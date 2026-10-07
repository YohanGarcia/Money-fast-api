# T-018 — Collection worklist `activity` existence filter

Official identity: **T-018 CREDIT_COLLECTION_WORKLIST_ACTIVITY_EXISTENCE_FILTER**. It adds ONE membership filter to
`GET /api/v2/collections/overdue-loans`: `activity`. No Activity type filter, no recency, no sort, no endpoint, no permission, no
migration, no index, no read model and no write.

## The parameter

`activity=has_activity|no_activity`, a single value. Anything else (`has`, `none`, an activity type, case variants, comma or pipe
lists, empty string) is a 422. Without it the behaviour is exactly the one before T-018.

| Value | Exact meaning (T-014 history of the loan, same tenant) |
|---|---|
| `has_activity` | at least one `credit_collection_activities` row exists for the loan |
| `no_activity` | no row has EVER existed for the loan |

Existence is historical and total: the type (`no_contact` and `other` count like any other), the recorder, the date, the number of
rows (1 or 5,000 give the same answer) and the snapshots (`assignment_id`, `managing_branch_id`) do not matter. An activity recorded
under an assignment that is closed today, or by a user who is no longer the assignee, still counts. `no_activity` does NOT mean "no
recent activity", "no contact" or "no activity under the current assignment".

The filter INTERSECTS with `collections.read` scope, `branch_id`, `currency`, `min_days_overdue`, `assignment`, `assignee_id` and
`promise_status` (`promise_status=broken&activity=no_activity` = visible overdue loans AND current promise broken AND no activity ever).
It never widens visibility: scope is still the loan's CURRENT managing branch (tenant-level `collections.read` for loans without one),
never the activity snapshot; `collections.actions.create` is neither required nor sufficient. The worklist stays an overdue list.

## Where it is applied (membership, never post-page)

Pipeline: tenant / RBAC → managing-branch scope → `branch_id` → `currency` → assignment filter → cheap "obligation due" candidate
condition → **activity `EXISTS` / `NOT EXISTS`** → promise SQL stage → exact promise projection → `ledger.views_many` ONLY for the
survivors → exact overdue → `min_days_overdue` → sort → cursor → page → enrichment. All the SQL conditions live in ONE candidate
statement, so PostgreSQL may reorder them; semantically both filters are resolved before the ledger. Tests spy the ledger (it receives
exactly the survivors of the activity filter, and of the promise + activity intersection) and walk the pages (full, no holes, same
order as the unpaged list).

**Query shape.** `EXISTS (SELECT * FROM credit_collection_activities WHERE tenant_id = :tenant AND loan_id = credit_loans.id)`, negated
for `no_activity`, built by the shared T-014 helper `activities.restrict_candidates` and added to the candidate statement (the worklist
never touches the Activity model, as T-016 requires). Existence is exact in SQL, so there is no Python stage and no second definition. No `count(*)`, `GROUP BY`,
`DISTINCT ON`, window, sort of the history or query per loan. The existing index `(tenant_id, loan_id, id)` serves it.

## Response and consistency

The response shape is unchanged (`last_collection_activity` already exists since T-016).

* `no_activity`: the membership already proved there is no activity, so every row returns `last_collection_activity = null` and the
  page lookup (`activities.latest_by_loan`) is skipped. An activity inserted concurrently after the candidate statement cannot make the
  same response contradict its filter; the next request sees it.
* `has_activity`: the T-016 page-only lookup runs as before (the greatest `id`). Activities are append-only, so existence never goes
  back to false; a newer activity inserted between the stages may be shown as the latest, which is correct.
* READ COMMITTED, no REPEATABLE READ / SERIALIZABLE / locks. The list is live keyset pagination, not a snapshot: an activity created
  between pages can move a loan from `no_activity` to `has_activity`.

## Cursor

Format unchanged: `{s, o, v, i, f}`. **`ae` enters the canonical fingerprint only when `activity` was requested**
(`"ae":"has_activity"` or `"ae":"no_activity"`, never null or a default), so without it the fingerprint is byte-for-byte the T-017 one
and every older cursor stays valid. A filtered cursor reused without the filter, with the other value, or with any other filter
change is a 422 `invalid_cursor`; so is an unfiltered cursor used with the filter. With both filters, `p` and `ae` bind the cursor.

## Cost

Query count is constant (independent of the page length): with the filter `has_activity` adds no SELECT (the `EXISTS` is inside the
candidate statement) and `no_activity` saves one (no latest-activity lookup). Measured in the test suite: no filter 13,
`has_activity` 13, `no_activity` 12, `promise_status=broken` + `has_activity` 12, + `no_activity` 11.

Evidence from the T-018 discovery (local throwaway PostgreSQL 17, synthetic and noisy, not a production SLO): 50,000 loans,
600,000 obligations, 275,000 payments, 10,000 reversals, 20,000 assignments, 30,000 current promises, activities on 10 / 50 / 90 % of the
loans plus 20 hot loans with 5,000 activities each. The candidate statement with the filter took 0.04–0.29 s with no spill; with large
candidate sets PostgreSQL uses a hash semi / anti join, with small ones (e.g. after the promise restriction) a nested loop over an
index-only scan of `(tenant_id, loan_id, id)`; on a hot loan `EXISTS` is one index probe. The ledger (13–14 s for 50,000 candidates, a
pre-existing T-011 cost) still dominates; the filter only shrinks it (e.g. `has_activity` with 10 % active loans: 18.1 s → 1.7 s).
The final code was checked again with `EXPLAIN (ANALYZE, BUFFERS)` (see the delivery report). No index and no migration were needed;
Alembic head stays `0017`.

## Not in T-018

* Activity type filters: "the latest activity is X" and "some activity was ever X" select materially different loans; a business
  decision is needed first.
* Recency (`no_activity_since`, inactive days, contacted recently): no window, timezone or outcome semantics are defined.
* Sorts by activity (`last_activity_at`, type, count): null ordering and business order are undefined.
* Any persisted activity state (`has_activity`, `last_activity_*` on the loan), read model, cache or index.
