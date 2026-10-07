# T-017 — Collection worklist `promise_status` filter

Official identity: **T-017 CREDIT_COLLECTION_WORKLIST_PROMISE_STATUS_FILTER**. It adds ONE membership filter to
`GET /api/v2/collections/overdue-loans`: `promise_status`. No Activity filter, no sort, no endpoint, no permission, no migration,
no index, no read model, no scheduler and no write.

## The parameter

`promise_status=open|fulfilled|broken|none`, a single value. Anything else (unknown value, `cancelled`, `superseded`, case
variants, comma or pipe lists, empty string) is a 422. Without it the behaviour is exactly the one before T-017. There is no
`has_current_promise` (the enum already separates `none` from the rest).

| Value | Exact meaning (the T-015 projection of the CURRENT promise, `closed_at IS NULL`) |
|---|---|
| `open` | `qualifying_paid_amount < promised_amount` and the loan's business date today `<= promise_date` |
| `fulfilled` | `qualifying_paid_amount >= promised_amount` (it does not mean the loan is paid: the worklist only lists overdue loans) |
| `broken` | `qualifying_paid_amount < promised_amount` and the loan's business date today `> promise_date` |
| `none` | there is NO current promise: never had one, cancelled-only or superseded-only history all count |

Closed history (cancelled / superseded) never satisfies `open`, `fulfilled` or `broken`; there are no historical filters. The
worklist stays an OVERDUE list: a broken promise on a loan without overdue debt does not enter, and a fulfilled promise on an
overdue loan can (`promise_status=fulfilled`). The filter INTERSECTS with `collections.read` scope, `branch_id`, `currency`,
`min_days_overdue`, `assignment` and `assignee_id`; it never widens visibility and needs no permission (`collections.read` only,
tenant-level for loans without a managing branch).

## Where it is applied (membership, never post-page)

Pipeline: tenant / RBAC → managing-branch scope → `branch_id` → `currency` → assignment filter → cheap "obligation due" candidate
condition → **promise SQL stage** → **exact promise projection** → discard non-matching candidates → `ledger.views_many` ONLY for the
survivors → exact overdue → `min_days_overdue` → sort → cursor → page → T-016 enrichment. A post-page filter is incorrect (short and
wrong pages) and is covered by tests (pages of exactly 2, 2, 1 and the unpaged list in the same order).

**Hybrid strategy.** The SQL stage (in `promises.restrict_candidates`, called from the candidate query) is exact for existence: `none` is
`NOT EXISTS` a current promise; the other values join the current promise (the partial UNIQUE `uq_credit_collection_promises_current`
guarantees at most one per loan, so the join never multiplies rows). For `broken` and `open` it adds a date SUPERSET prefilter that only
reduces and never decides: in any timezone a loan's business date is at most one day ahead of / behind the UTC date, so `broken` needs
`promise_date <= UTC date` and `open` needs `promise_date >= UTC date - 1`. A property test over every IANA timezone and thousands of
instants, and a test with real loans in UTC-4, UTC+14 and UTC-11 around every local midnight, show it never drops a legitimate loan.
The exact stage (`promises.filter_candidates`) projects the remaining promises with the ONE shared definition — `qualifying_paid_amounts`
plus `projected_status`, each in its own loan's business date — so no T-015 rule is duplicated in SQL or in `worklist.py`.

**Shared helper.** `promises.qualifying_paid_amounts` (confirmed payments of the same tenant and loan, `received_at >= promise.created_at`,
`business_date <= promise_date`, no reversal, `sum(amount)`; counter and field both count; late and previous payments do not; a reversed
payment contributes 0) is still the single definition used by T-015, T-016 and T-017. It now takes ids and sends them as ONE PostgreSQL
array (`id = ANY(...)`), so a candidate set of tens of thousands of promises is one cheap parameter, not tens of thousands of binds.

**Reuse.** The mini-view computed by the filter stage (`promises.mini_view`, the same one the T-016 page enrichment uses) becomes
`current_promise` of the surviving rows: the status shown is always the status filtered by (`promise_status=broken` ⇒ every row says
`broken`; `none` ⇒ `current_promise` is null) and the page does not repeat any promise query. The unfiltered request keeps the T-016
page-only enrichment unchanged. `last_collection_activity` is untouched.

## Cursor

Format unchanged: `{s, o, v, i, f}`. The fingerprint is the SHA-256 (16 hex) of the canonical JSON of the membership filters; **`p` enters
only when `promise_status` was requested** (`{"a","b","c","m"}` or `{"a","b","c","m","p"}`), so a cursor issued without the filter (any
cursor of T-013 / T-016) stays valid for the same request, and a filtered cursor is a 422 `invalid_cursor` if reused without the filter,
with another value, or with another filter. Cursor position semantics and the sorts (`days_overdue`, `overdue_outstanding`,
`oldest_overdue_date`) do not change.

## Consistency and races

READ COMMITTED, no snapshot, no lock, no `REPEATABLE READ` / `SERIALIZABLE`. The promise projection that decides membership is the one shown
in the row. Residual drift is accepted and documented: the promise stage and the ledger stage are separate queries, so a payment committed
between them can be visible to one and not to the other in that response; the next request reflects it. A reversal can turn `fulfilled` into
`open` / `broken` on a later read; a replaced or cancelled promise changes membership between pages. No crash, no write, no cursor snapshot.

## Cost

Query count is constant (independent of the page length; measured for each value with every filter non-empty): no filter 13, `none` 11,
`open` / `fulfilled` / `broken` 12 (promise rows come from the candidate SQL, one payment aggregation, no page promise lookup; `none` needs no
aggregation).

Benchmark of the real final code (`worklist.overdue_loans`) on a scratch PostgreSQL 16 database migrated to head, bulk-loaded into the real
tables: 50,000 loans in 3 timezones, 600,000 obligations, 500,000 payments, 25,000 reversals, 30,000 current promises (+10,000 loans with only
closed promises), 20,000 open assignments, page of 50. Medians of 7 runs (3 for the unfiltered baseline), local machine, noisy (min–max ranges
are wide); this is regression evidence, not a production SLO:

| broken-eligible share | no filter | none | open | fulfilled | broken |
|---|---|---|---|---|---|
| 1% | 47.9 s (ledger 37.7 s, 50k ids) | 21.6 s | 8.1 s | 19.1 s | **0.23 s** (300 survivors) |
| 20% | 69.0 s | 40.8 s | 7.1 s | 24.0 s | 4.6 s (6,000 survivors) |
| 80% | 57.0 s | 27.3 s | 1.1 s | 6.0 s | 18.6 s (24,000 survivors) |

The promise stage itself (candidate SQL + exact projection) costs 16 ms – 2.1 s; the rest is `ledger.views_many` over the survivors. The
filter never costs more than it saves: survivors are a subset of the unfiltered candidates, so the ledger — which is what dominates the
worklist (≈38–49 s for 50,000 candidates, a pre-existing T-011 cost not changed here) — only gets cheaper. EXPLAIN of the broken path (20%):
candidate SQL 69 ms (30,046 buffers), qualifying aggregation 483 ms (no spill; index on `ix_credit_payments_loan` and
`uq_credit_payment_reversals_payment`); at 80% the aggregation spills a hash aggregate to disk at the default `work_mem` (482 ms). No index was
added (the discovery's covering index showed no consistent gain) and no migration was needed. No production SLO is claimed.

## Not in T-017

Activity filters (`has_activity`, `no_activity`, `activity_type`, `no_activity_since`, "contacted recently") — T-018 discovery; new sorts and
null-order / state-ranking policies; historical promise filters; a persisted `broken` / status marker, scheduler or read model (the decision is
open until production provides tenant sizes, p95/p99 and an agreed SLO); permissions; scoring. GET stays at 0 INSERT/UPDATE/DELETE and 0 audit.

**BLOCKED_BY_SPEC**: activity type semantics (latest vs any), recency window, "contacted recently", promise / activity sorts, null ordering, state
ranking, score / priority, `requires_review`, notes / outcomes / free text, retention / regulation. **BLOCKED_BY_EVIDENCE**: production worklist
SLO, real tenant sizes, combined production ledger timings, need of a read model, real payments-per-loan distribution.

## Known limitations

* The filter is evaluated over the candidate universe (loans with a current promise and an obligation due), so its cost grows with them, not with
  the page; the unfiltered worklist already pays a ledger cost that dominates at 50,000 candidates.
* Residual READ COMMITTED drift between the promise stage and the ledger stage (documented above).
* The date prefilters are supersets; their safety rests on the UTC-offset bound (−12h … +14h) and is covered by tests.
* The benchmark is local, synthetic and noisy.
