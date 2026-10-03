# T-013 — Collection worklist assignment filters

Official identity: **T-013 CREDIT_COLLECTION_WORKLIST_ASSIGNMENT_FILTERS**. It integrates the T-012 current assignment into the
T-011 read-only worklist `GET /api/v2/collections/overdue-loans` as an extra **restriction** and a per-row summary. It adds no
write, no permission and no financial rule.

## API

| Parameter | Values | Meaning (always over the loans the actor already sees) |
|---|---|---|
| `assignment=mine` | | current assignee = the caller (resolved to `assignee:<actor.user_id>`) |
| `assignment=unassigned` | | no OPEN assignment row exists |
| `assignment=assigned` | | any OPEN assignment row exists (exact complement of `unassigned`) |
| `assignee_id=<int > 0>` | | the OPEN assignment's assignee equals the id |

`assignment` and `assignee_id` are **mutually exclusive** (422 `conflicting_assignment_filters`, no silent precedence). An
unknown `assignment` value, `assignee_id` ≤ 0 or non-integer is a 422. There are no `assigned_to_me` / `unassigned` booleans
(an unknown query parameter is ignored by FastAPI, as for any endpoint). Without either parameter the population is exactly
the T-011 one. `branch_id`, `min_days_overdue`, `currency`, `sort`, `order`, `limit` and `cursor` keep their T-011 meaning; no new sort.

## Semantics

* **Current = open row only** (`ended_at IS NULL` of `credit_collection_assignments`). A loan with any number of closed rows is
  `unassigned`. Latest-history, `customers.assigned_collector_id`, `routes.assigned_collector_id` and the `collector` role are
  never read: a modern loan without an open row is unassigned, with no fallback.
* **Assignment never grants access.** Order: tenant → `collections.read` / managing-branch scope (T-011) → candidate overdue
  universe → T-011 filters → assignment restriction → exact overdue derivation → sort → cursor/page. The filter is an
  intersection with the visible set, never a union: a user that is the assignee of a loan it cannot read does not see it; a
  branch-scoped user never sees a loan without managing branch even with `assignment=mine`.
* **Authorization**: `collections.read` with exactly the T-011 scope, for every filter. `collections.assign` is a WRITE permission
  and is **not** a read gate: T-012 already shows `assignee_user_id` of the current/history row to any `collections.read` holder
  of that loan, so a second gate on the same information would be inconsistent. No new permission (no `collections.work`).
* **Unknown / foreign / inactive / unassigned `assignee_id`** → `200` with zero rows, the same body for every kind of id. No
  `users` lookup is made (the assignee id is never validated), so there is no user-existence oracle; the query is tenant-scoped,
  so another tenant's assignments cannot leak.
* **Stale assignments stay current.** An open assignment whose assignee was disabled or lost `collections.read` still filters as
  current; reading never closes, repairs or audits anything. For the stale user itself `assignment=mine` shows a loan only while
  RBAC still lets it read it (otherwise 403/empty): assignment never substitutes RBAC.
* **Not a list of "my loans"**: `mine`/`assigned` mean *overdue loans of the visible worklist whose current assignment matches*.
  A paid or not-yet-overdue loan keeps its open assignment (T-012) but is not listed, because the overdue truth (T-010/T-011:
  `business_date > effective due_date` and net outstanding > 0, net ledger, frozen timezone) is untouched.
* **Row**: the T-011 row plus `current_assignment: {assignment_id, assignee_user_id, assigned_at} | null`. No names, e-mail, phone,
  role, branch snapshot, `assigned_by`, ended fields, history, idempotency keys or digests; the assignee is not looked up and no
  "eligible/valid" flag is computed.

## Cursor

`{"s": sort, "o": order, "v": value, "i": loan_id, "f": fingerprint}` (base64 JSON). The fingerprint is the first 16 hex of the
SHA-256 of the canonical JSON `{"a": <none|unassigned|assigned|assignee:<id>>, "b": branch_id, "c": currency, "m": min_days_overdue}`
(deterministic, never Python `hash()`; not a signature). `mine` is resolved to `assignee:<actor.user_id>` **before** hashing, so a
`mine` cursor of one user is `invalid_cursor` for another, and `mine` of A is interchangeable with `assignee_id=A` for A. A change
of any of sort, order, branch, min days, currency or the resolved assignment filter is a 422 `invalid_cursor`; `limit` is not
bound. Cursors of T-011 (no `f`) are intentionally invalid (they are short-lived). Before T-013 the cursor was bound only to sort
and order; branch/min-days/currency are now bound too. The cursor never carries scope: RBAC is recomputed on every request, so
it cannot widen what is visible.

## Live pagination (known limitation)

There is no snapshot across requests: a reassignment, an end, a new assignment, a payment, a reversal or a date change between
two pages can change the membership of the next page (never the scope). No snapshot table, token or server-side cursor state.

## Query strategy and cost

* The assignment restriction is an `EXISTS` / `NOT EXISTS` over the open rows (tenant + loan [+ assignee]) in the **candidate
  query**, so the ledger is loaded only for loans that pass it (`ledger.views_many` receives only those; tested with a spy).
* The page's `current_assignment` is ONE batched query (`tenant_id AND loan_id IN (page) AND ended_at IS NULL`); the partial UNIQUE
  of T-012 guarantees at most one row per loan. The number of SELECTs does not grow with the page (tested: 1 vs 100 items, and per
  filter).

### Index experiment (PostgreSQL 16, 20 000 loans, 240 000 obligations, 15 000 open + 20 000 closed assignments; median of 7 runs,
`EXPLAIN (ANALYZE, BUFFERS)` of the candidate query on UNLOGGED tables with the same columns and indexes)

Candidate: `(tenant_id, assignee_user_id, loan_id) WHERE ended_at IS NULL`.

| Case | without | with | plan |
|---|---|---|---|
| assignee, 20 loans | 3.68 ms | **0.14 ms** | bitmap scan of the open index → index-only scan of the candidate index |
| assignee, ~75 loans | 3.54 ms | **0.47 ms** | same |
| assignee, 5 000 loans | 41.3 ms | **23.6 ms** | candidate index used |
| assigned | 100 ms | 85.6 ms | candidate index not used (no change) |
| unassigned | 30.8 ms | 23.9 ms | not used (no change) |
| no assignment filter | 87.7 ms | 92.8 ms | not used (run-to-run noise) |

PostgreSQL uses the index for the selective and the non-selective assignee cases, the gain is reproducible (7 runs, fewer buffers) and
the other paths do not degrade. **Decision: migration 0015 adds only this partial index** (write cost is negligible: assignments are
low-volume). Caveat: the benchmark is local and synthetic; absolute gains are milliseconds at this size, and the end-to-end cost of
the worklist is still dominated by the candidate/ledger work (T-011).

## Migration 0015

`0015_credit_collection_worklist_assignment_index` creates only `ix_credit_collection_assignments_open_assignee`; its downgrade only
drops it (no history guard needed: no data is touched). `alembic check` is clean. Head: **0015**.

## Not in T-013

No assignment write, history change, automatic/bulk assignment, team/provider/supervisor, sort by assignment, history or date-range
filter, legacy import or fallback, field-payment coupling, custody/rendition, CRM/activity/promise, accrual, scheduler or persisted
worklist. GET stays at 0 INSERT/UPDATE/DELETE, 0 audit, no assessment.

**BLOCKED_BY_SPEC**: hiding/limiting the assignee identity as a new policy (it would also affect the T-012 GETs), assignment sort,
history filters, multi-assignee, teams, providers, supervisors, `collections.work`, field payment ↔ assignment, activities, promises,
escalation, custody/rendition, delinquency accrual, legacy import, earlier pending items. **BLOCKED_BY_EVIDENCE**: modern routes,
team/provider models, supervisor hierarchy, scheduler/worker, accounting, outbox, production-scale index evidence.
