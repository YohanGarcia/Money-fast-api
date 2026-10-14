# T-012 — Credit Collection Assignment v1

Official identity: **T-012 CREDIT_COLLECTION_ASSIGNMENT**. It records WHO is responsible for collecting a loan, as
effective-dated history. It is metadata: it moves no money, changes no debt or loan status, grants no access and does not
change the T-011 worklist.

## Model

* **Target = the loan** (`credit_loans.id`). Two loans of the same customer may have different assignees. Customer,
  obligation, route, portfolio and branch targets do not exist here.
* **Assignee = a `users` row** (modern identity). Never the legacy `users.role = 'collector'`,
  `customers.assigned_collector_id` or `routes.assigned_collector_id`.
* **One open assignment per loan.** Table `credit_collection_assignments`: `id, tenant_id, loan_id, assignee_user_id,
  managing_branch_id (snapshot), assigned_by, assigned_at, idempotency_key, request_digest, ended_by, ended_at,
  end_idempotency_key, end_request_digest`. No status column: **open ⇔ `ended_at IS NULL`**.
* **History is never rewritten.** A reassignment closes the open row and inserts a new one in the same transaction; `end`
  only closes. Nothing is deleted, reopened or edited.
* **`managing_branch_id` is a snapshot** of the loan's managing branch at creation (may be NULL), never sent by the client and
  checked by the database to equal the loan's own.
* Timestamps are server generated (`assigned_at`, `ended_at`): no backdated, future or scheduled assignment, no client date,
  and **no reason field** in v1 (no catalogue, no free text).

## What the database enforces

| Invariant | Mechanism |
|---|---|
| at most one open row per loan | partial UNIQUE `(tenant_id, loan_id) WHERE ended_at IS NULL` (the backstop of the loan lock) |
| tenant-safe references | composite FKs for loan `(tenant, loan)`, assignee, `assigned_by`, `ended_by` (→ `users(company_id, id)`) and managing branch |
| idempotency namespaces | UNIQUE `(tenant_id, idempotency_key)` and partial UNIQUE `(tenant_id, end_idempotency_key) WHERE NOT NULL` |
| the end transition is all-or-nothing | CHECK: `ended_at`, `ended_by`, `end_idempotency_key`, `end_request_digest` appear together; `ended_at >= assigned_at` |
| born open, under the loan's managing branch | BEFORE INSERT trigger |
| immutability | BEFORE UPDATE/DELETE guard: DELETE always refused; a closed row accepts NO update (no reopen); an open row may only gain its end fields; assignee, loan, branch snapshot, `assigned_by/at` and the idempotency pair never change |

`users(company_id, id)` had no UNIQUE target, so migration 0014 adds **only** `uq_users_tenant_id` (`id` is already unique:
it changes no identity semantics) to support the tenant-safe FKs.

## Commands

`POST /api/v2/loans/{loan_id}/collection-assignment` `{assignee_user_id, idempotency_key}` — initial assignment **or**
reassignment, depending on whether a different open assignment exists. `POST …/collection-assignment/end`
`{idempotency_key}`. Unknown fields (tenant, reason, dates, branch) are a 422. Both return 200 (also on replay, with
`replayed`).

Assign / reassign (one transaction, lock order **loan `FOR UPDATE` → open assignment row → assignee lookup**; no Cash, no
obligations): tenant + `collections.assign` on the managing branch → idempotency → lifecycle → assignee eligibility → close the
old row (if any) → insert the new row → audit → commit.

* **Lifecycle gate**: only a loan stored `active` or `past_due` can receive a NEW assignment or reassignment
  (`409 loan_not_assignable` otherwise: paid, restructured, refinanced, …). The stored status is a lifecycle gate, **not** an
  overdue test: an `active` loan that is not overdue can be assigned.
* **Same assignee**: a NEW key asking for the current assignee → `409 already_assigned` (no row, no audit: a no-op whose key
  could not be persisted would break durable idempotency). The retry of the key that created it is a replay.
* **End** is allowed in any loan status (closing responsibility must always be possible). With no open assignment:
  `409 no_active_assignment` unless it is the exact replay of a previous end.

### Idempotency

Assign/reassign: `(tenant, idempotency_key)` + digest of `(operation=assign, loan_id, assignee_user_id)` stored on the NEW row.
Same key + digest → 200 replay of that row (0 writes, 0 audit, even if it was later closed); same key + other digest (or another
loan) → 409 `idempotency_conflict`. End: separate namespace `(tenant, end_idempotency_key)` + digest of `(operation=end_assignment,
loan_id, assignment_id)`; same → 200 replay, anything else → 409. The old row closed by a reassignment records the reassignment's
key and digest as its end pair.

## Eligibility of the assignee (checked at command time only)

Same tenant (another tenant's user → 404), `status = active` (pending / locked / disabled → `422 assignee_not_eligible`), and a
modern grant `collections.read` that covers the loan: tenant-level, or a branch grant on its managing branch; for a loan
**without** managing branch only a tenant-level grant. `user.branch_id` is irrelevant. Assignment creates no permission.

**Limitation**: eligibility is the state read by the command. If the assignee is later disabled, loses the permission or the
branch scope, the row is neither closed nor modified (it can be operationally stale); there is no global lock over RBAC, so a
concurrent revoke is not serialised against the command.

## Authorization

* **Writes** (`collections.assign`, sensitive): on the loan's **managing branch**; a loan without one needs a **tenant-level**
  grant. Origin, disbursement and receiving branches never substitute it; the legacy role never authorizes.
* **Reads** (`collections.read`, same boundary as T-011): `GET …/collection-assignment` (current, `200` with
  `assignment = null` when unassigned) and `GET …/collection-assignment/history` (all rows, `assigned_at ASC, id ASC`).
* **Being assigned grants nothing**: no `collections.read`, `loans.read`, customer, PII, payment or Cash permission (tested: an
  assignee that lost its grant gets 403 while its row stays open).
* Responses carry IDs and timestamps only (no names, contact, role strings, keys or digests). Cross-tenant loan → safe 404;
  the tenant always comes from the session.

## No automation

Payments, reversals and the delinquency assessment never touch assignments. A loan that becomes `active` again, overdue again or
`paid` keeps its open assignment until an explicit `end`; a reversal never revives a closed row nor creates one (a manually ended
loan stays unassigned until a new explicit assignment). Concurrency with a payment or a reversal is serialised by the loan lock:
if the payment settles the loan first, a new assignment is refused; otherwise the assignment is created and stays open.

## Audit vs history

The table is the business history (who, which loan, from/until when, who assigned/closed). The audit adds context:
`loan.collection_assigned`, `loan.collection_reassigned`, `loan.collection_assignment_ended` (assignment id, loan, assignee,
previous assignee/assignment, managing branch; no PII; none on replay or on a failed command; reads are not audited).

## Migration 0014 and downgrade

Creates `uq_users_tenant_id`, the table with its constraints and indexes, the guard and insert-check triggers, and the permission
`collections.assign` (granted to the tenant admin roles; not to the legacy collector role). **Downgrade 0014 → 0013 refuses to
run before any DDL when the table has rows** (it would erase operational history); with an empty table it is clean, and
re-upgrade works.

## Not in T-012 (still deferred / blocked)

* **T-013**: worklist filters by assignment (`assigned_to_me`, `unassigned`, `assignee_id`). T-011 is unchanged.
* **BLOCKED_BY_SPEC**: multi-assignee, teams, external providers, supervisors, automatic and bulk assignment, assignment reasons,
  alternatives to a loan target (customer/route/portfolio), whether field payments must match the assigned user, a future "work"
  permission, activity / promises / escalation, custody and rendition, delinquency accrual, legacy import policy, and everything
  pending from T-006 to T-011.
* **BLOCKED_BY_EVIDENCE**: modern routes, team and provider entities, supervisor hierarchy, scheduler/worker, accounting, outbox.
* **Legacy**: `customers.assigned_collector_id` and `routes.assigned_collector_id` are neither imported, compared nor written;
  the legacy screens keep using them.
