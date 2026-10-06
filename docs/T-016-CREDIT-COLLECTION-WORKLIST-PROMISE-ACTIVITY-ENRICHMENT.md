# T-016 — Collection worklist promise / activity enrichment

Official identity: **T-016 CREDIT_COLLECTION_WORKLIST_PROMISE_ACTIVITY_ENRICHMENT**. It adds exactly two fields to every row of
`GET /api/v2/collections/overdue-loans`: `current_promise` (T-015) and `last_collection_activity` (T-014). It is **enrichment
only**: no filter, no sort, no change of membership, order, cursor, fingerprint, scope or permission, no migration, no index, no
write.

## Boundary

* Membership, order, `next_cursor`, scope and authorization are exactly T-013's. With the same parameters the list of `loan_id`
  (and its order, sort values and cursor) is identical with or without promises and activities (tested for no filter, `branch_id`,
  `currency`, `min_days_overdue`, `assignment=mine|assigned|unassigned`, `assignee_id` and the other sorts).
* A promise or an activity never includes, hides, orders, prioritizes or grants access to a loan, never pauses overdue, creates no
  grace period and changes no overdue fact. There is no score, no `last_promise`, no `contacted recently`.
* Nothing is removed or renamed in the row (`loan_id, loan_number, customer_id, managing_branch_id, currency, projected_status,
  overdue_obligations, days_overdue, overdue_outstanding, oldest_overdue_date, next_due_date, last_net_payment,
  current_assignment`). The row-level `projected_status` is still the economic state of the LOAN; the promise's own state is
  `current_promise.projected_status`.

## `current_promise`

`null` or exactly: `promise_id, promised_amount, currency_code, promise_date, projected_status, qualifying_paid_amount, created_at`.

* **Current = `closed_at IS NULL`** (T-015), never "projects open". A current promise may therefore project `open`, `fulfilled` or
  `broken`; a fulfilled or broken one is still shown. A loan whose promises are all `cancelled` / `superseded` has
  `current_promise = null` (no historical fallback).
* The projection is T-015's, with no second formula: `qualifying_paid_amount` comes from the ONE shared helper
  `promises.qualifying_paid_amounts` (confirmed payments of the loan with `received_at >= promise.created_at`,
  `business_date <= promise_date`, not reversed, summed; any origin) and `projected_status` from the same `projected_status`
  function (`fulfilled` if qualifying ≥ promised, `broken` if today > promise date, else `open`). Nothing is persisted, so a
  reversal re-projects on the next read.
* "Today" is each loan's own business date in its FROZEN contract timezone, using the timezone the worklist already loads per
  candidate (`business_date_in(c.tz, now)`): no UTC date, no global date and no extra query.
* Not exposed: `created_by`, `assignment_id`, `managing_branch_id`, `supersedes_promise_id`, closure fields, keys, digests (the T-015
  detail endpoint has them).

## `last_collection_activity`

`null` or exactly: `activity_id, activity_type, created_at`. It is the T-014 activity with the **greatest `id`** of the loan (the
canonical `id DESC` of the history, not the timestamp), whatever the assignment at the time or now, and whoever recorded it (no
user is looked up; no name, contact or PII).

## Query strategy (page only)

Order: candidates → ledger → T-011/T-013 filters → sort → cursor → page → last payment → current assignment → **current promises →
last activities**. The new helpers receive ONLY the `loan_id` of the page (never the candidate universe; spied in the tests with 6
candidates and a page of 2).

| Query | Shape | Index |
|---|---|---|
| current promises | `tenant_id = … AND loan_id IN (page) AND closed_at IS NULL` | `uq_credit_collection_promises_current` |
| qualifying payments | one aggregation for the page's current promises (skipped when the page has none) | `ix_credit_payments_loan`, `uq_credit_payment_reversals_payment` |
| last activities | `LATERAL … ORDER BY id DESC LIMIT 1` per loan | `(tenant_id, loan_id, id)` read backwards |

Every query filters `tenant_id` explicitly. Query count: the worklist did 10 SELECT before T-016; now **12** when no page loan has a
current promise and **13** when some has (the aggregation only runs then), identical for a page of 1 and of more rows (tested):
constant, no N+1. The cost of the candidate set (ledger over every candidate) does not change.

**Why LATERAL.** Local synthetic benchmark (PostgreSQL 16; 240,000 payments, 12,000 reversals, 30,000 promises of which 10,000 current,
300,000 activities including 20 loans with 5,000 each; page of 100 loans; median of 9 runs): current promises 0.31 ms; qualifying
payments 1.79 ms; last activity with `LATERAL` 1.00 ms (index scan backward, no sort) versus 511 ms with a global `DISTINCT ON
(loan_id) … ORDER BY loan_id, id DESC` (it sorts every activity of the busy loans, spilling to disk). No index was added or needed;
the benchmark is local and synthetic.

## Cursor, fingerprint, authorization

The cursor stays `{s, o, v, i, f}` and the fingerprint stays the SHA-256 (16 hex) of `{branch, min_days, currency, resolved
assignment}`: enrichment is not part of either, so cursors issued before T-016 stay valid (tested, including a hand-built one and
a recomputed fingerprint). Authorization is only `collections.read` on the loan's CURRENT managing branch (tenant-level for a loan
without one); `collections.promises.create`, `collections.actions.create` and `collections.assign` neither are required nor grant
anything, and the historical branch / assignment snapshots of promises and activities are never used for access.

## Consistency, purity

GET stays live: no snapshot across requests, no lock, no `REPEATABLE READ` / `SERIALIZABLE`. The queries of one request read what is
committed when each runs, so a promise or activity created (or a reversal applied) meanwhile may appear in that very response or in
the next page; membership never depends on them. GET does 0 INSERT/UPDATE/DELETE, 0 audit, no repair of promises, activities or
assignments, no loan status or delinquency write. Alembic head stays **0017**; no permission was added.

## Not in T-016

Filters (`promise=…`, broken-promise, `has/no_activity`, `activity_type`, `no_activity_since`) and sorts (`promise_date`,
`promise_status`, `last_activity_at`): T-017, which must settle membership semantics, cursor fingerprint, the cost over every
candidate (a promise filter needs the current promise, the payments and each loan's date before pagination) and production-scale
evidence. Also out: score / priority, read model or materialized queue, scheduler, reminders, customer / user enrichment, T-014
outcomes / text, T-015 `requires_review`, retention / regulatory policy, Cash, accrual, accounting, outbox.

## Known risks

* One request does several reads without a common snapshot, so a row may mix instants a few milliseconds apart.
* The promise's derived state depends on the loan's business date and on the shared helper staying the single definition.
* The last payment still uses a `DISTINCT ON` (cheap with few payments per loan; not measured with very many).
* Local, synthetic benchmark only.
