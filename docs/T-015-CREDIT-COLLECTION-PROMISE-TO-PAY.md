# T-015 — Credit Collection Promise-to-Pay v1

Official identity: **T-015 CREDIT_COLLECTION_PROMISE_TO_PAY**. It records a customer's declared commitment to pay an amount on a
loan by a date. **A promise is a commitment, not a payment**: it moves no money, creates no payment, reduces no debt, changes
neither overdue, delinquency, loan status, worklist nor activity, and never stops collection.

## Decisions D1–D29 (closed)

| # | Decision |
|---|---|
| D1 | Loan-level (`credit_loans.id`); one promise may economically be met by payments applied to several obligations. No customer / obligation / case / route / portfolio target. |
| D2–D4 | `promised_amount` mandatory, **> 0**, in cents (the precision a payment can actually be made in); partial promises are valid. |
| D5 | Cap at CREATE / REPLACE: `promised_amount <= due_to_date_outstanding` of the NET ledger (applications minus reversal applications, the same source as payments) evaluated **at the promise date**. Above it → 422 `promise_amount_exceeds_due_by_date`; nothing due by that date → 422 `promise_not_applicable`. No clamp, no new formula, no delinquency accrual. The stored `loan.status` is never the gate. Reason: T-008 never lets a payment exceed what is due on its date, so a bigger promise would be impossible under the current runtime. |
| D6 | `promise_date` is a calendar date in the contract's FROZEN timezone (`ledger.business_date`), never the UTC date. |
| D7–D8 | Today allowed; the past is 422 `promise_date_in_the_past`; no future horizon limit. |
| D9 | At most ONE promise not closed per loan (partial UNIQUE `(tenant_id, loan_id) WHERE closed_at IS NULL`). "Current" = not closed, whatever its financial result (open, fulfilled or broken). |
| D10 | A normal create with a current promise is 409 `already_has_open_promise` (never a hidden supersede). Renegotiation = explicit `replace`. |
| D11 | New sensitive tenant permission `collections.promises.create` governs create, replace and cancel (not `collections.assign`, `collections.actions.create`, `payments.create`, `loans.read`). Not granted to the legacy `collector` role. |
| D12 | Explicit cancel, no reason, no free text, same permission; only a current promise that projects `open`. |
| D13 | Nullable snapshot of the open T-012 assignment; it neither authorizes, restricts nor cancels; reassign / end afterwards do nothing to the promise. |
| D14 | Active non-overdue loans may receive a promise if something is due by the promise date (the next instalment can be promised). |
| D15 | Economically settled (nothing due by the date) → 422, whatever the stored status says. |
| D16 | A payment qualifies only if `payment.received_at >= promise.created_at` (inclusive); earlier payments never count. |
| D17 | Qualifying payments are summed (400 + 600 meets 1,000). |
| D18 | Counter and field payments count the same; no custody / rendition coupling. |
| D19 | Deadline: `payment.business_date <= promise_date` (the deadline day itself counts). |
| D20 | A late payment never counts; a broken promise is not repaired retroactively (the loan itself may still be paid). |
| D21–D22 | Reversals reproject: a reversal of the qualifying payment returns the promise to `open` before the date, to `broken` after it. |
| D23–D25 | `fulfilled` / `broken` are DERIVED on every read from the net ledger; nothing is stored (no `fulfilled_at`, `broken_at`, status or amount column); the projected status may change between reads because of reversals. |
| D26 | No scheduler, no worker, no write on read. |
| D27 | Independent of T-014: no activity FK, no synthetic activity, T-014 unchanged. |
| D28 | The worklist is unchanged (promise enrichment and filters belong to T-016). |
| D29 | No `requires_review` state: no objective rule exists. |

## Model

Table `credit_collection_promises` (exactly): `id, tenant_id, loan_id, managing_branch_id, assignment_id, created_by, created_at,
currency_code, promised_amount, promise_date, supersedes_promise_id, idempotency_key, request_digest, closed_at, closed_by,
closed_kind, close_idempotency_key, close_request_digest`. `currency_code` is the loan's (server-derived); `created_by` is the
authenticated actor; `managing_branch_id` / `assignment_id` are server snapshots; `created_at` is the server's.

**Closure** (`closed_*` appear together or all NULL, `closed_kind` ∈ {`cancelled`, `superseded`}) is the ONLY change a row ever
gets, once. `fulfilled` / `broken` can never be a closure kind. A `replace` stores the new promise with
`supersedes_promise_id = old.id` and closes the old one as `superseded` in ONE transaction.

### What the database enforces (migration 0017)

| Invariant | Mechanism |
|---|---|
| one current promise per loan | partial UNIQUE `uq_credit_collection_promises_current` |
| terms immutable, single closing transition, no DELETE | BEFORE UPDATE/DELETE guard trigger |
| born open, with the loan's branch snapshot, currency and open assignment (or NULL), valid supersede relation | BEFORE INSERT trigger |
| amount > 0, closure all-or-nothing, closure kind, no self-supersede, `closed_at >= created_at` | CHECK constraints |
| tenant safety | composite FKs: loan, creator, closer, managing branch, assignment snapshot `(tenant, assignment, loan)` and superseded promise `(tenant, promise, loan)` |
| idempotency | UNIQUE `(tenant_id, idempotency_key)` and partial UNIQUE `(tenant_id, close_idempotency_key)` |
| history reads | index `(tenant_id, loan_id, id)` |

Only supporting UNIQUE added: `(tenant_id, id, loan_id)` on the new table itself (target of the supersede FK); the assignment
UNIQUE already exists from 0016. Downgrade 0017 → 0016 **refuses before any DDL when rows exist**; with the table empty it is clean.

## The derived outcome

`projected_status`:

1. closed → `cancelled` / `superseded` (terminal: later payments or reversals never reopen it);
2. else `fulfilled` if `qualifying_paid_amount >= promised_amount`;
3. else `broken` if the contract's business date today `> promise_date`;
4. else `open`.

`qualifying_paid_amount` = sum of `amount` of the loan's payments that are `confirmed`, `received_at >= promise.created_at`,
`business_date <= promise_date`, and have no reversal (reversals are full, so a reversed payment contributes 0). Any origin. It is
computed in one batched query for any number of promises and is also returned for closed promises (information only).

Examples (promise 1,000 for Oct 10): one payment of 1,000 on Oct 9 → fulfilled; 400 + 600 → fulfilled; only 600 → open until the
10th, `broken` from the 11th; the remaining 400 on the 12th does not repair it; the 1,000 reversed on the 9th → open again, on the
11th → broken.

## Commands (all loan-first)

`POST /api/v2/loans/{id}/collection-promises` `{promised_amount, promise_date, idempotency_key}` (unknown fields, including currency,
status, actor, branch, assignment, note, activity: 422) · `POST …/collection-promises/replace` (same body; the key is the durable
replace key) · `POST …/collection-promises/{promise_id}/cancel` `{idempotency_key}`.

Order: tenant → loan `FOR UPDATE` → permission → amount / date → idempotency replay → current promise → business date → ledger at the
promise date → snapshots → INSERT / close → audit → commit. **Authorization always runs before any replay.**

* Create / replace key: `(tenant, idempotency_key)` + digest of `(operation, loan, amount, date)`; exact replay → 200, 0 writes, 0
  audit, and it keeps working after the promise is later cancelled or superseded; same key with other terms, operation or loan → 409.
* Cancel (and the replace-closure of the old row) use a separate `(tenant, close_idempotency_key)` namespace; exact replay → 200; a new
  key on a closed promise → 409 `promise_already_closed`; on a fulfilled or broken promise → 409 `promise_not_cancellable`.
* Replace works on ANY current promise (open, fulfilled or broken), so a broken promise never blocks a renegotiation; with no current
  promise it is 409 `no_open_promise`.

## Reads

`GET …/collection-promises/current` (200 with `promise: null` when there is none; otherwise the promise that is not closed, with any
projected status), `GET …/collection-promises` (newest first, keyset on `id`, default 50, max 100, no OFFSET, strict cursor `{"i": n}`),
`GET …/collection-promises/{id}` (another loan's, another tenant's or a missing id: the same safe 404). Fields: ids, snapshots,
creator, `created_at`, `currency_code`, `promised_amount`, `promise_date`, `supersedes_promise_id`, closure fields,
`projected_status`, `qualifying_paid_amount`. Never keys, digests or PII. GETs do 0 writes and 0 audit.

## Authorization

Write: `collections.promises.create` on the loan's MANAGING branch (tenant-level without one); no `collections.read` needed. Read:
`collections.read` on the same boundary. Creator, assignee, activity and the snapshots grant nothing.

## Concurrency

Create, replace, cancel, payments, reversals and assignment commands all take the loan row first, so they serialize without a global
lock; the promise takes no obligation, Cash or customer lock (reading the ledger takes no row locks). If a payment wins, the cap sees
the ledger after it; if the promise wins, the payment counts toward it. Cancel vs payment: either order is valid (a promise already
fulfilled is not cancellable). After a command, reversals and payments only change what the next read projects. The partial UNIQUE is
the backstop for concurrent creates.

## Audit

`loan.collection_promise_created`, `loan.collection_promise_replaced` (with `superseded_promise_id`) and
`loan.collection_promise_cancelled`: ids, loan, amount, currency, date, creator, snapshots, closure kind. No PII, key, digest or text;
none on replay, failure or read. Audit is not an activity.

## Not in T-015

Worklist fields / filters, follow-up, reminders, scheduler, notifications, activity linkage, notes / reasons, `requires_review`,
multiple simultaneous promises, per-obligation or customer promises, formal payment agreements, accrual or grace, Cash, accounting,
outbox, legacy import. **BLOCKED_BY_SPEC**: those items plus a sticky historical outcome, interaction with future adjustments /
write-offs, cancellation reasons, retention and regulatory policy, earlier pending items. **BLOCKED_BY_EVIDENCE**: scheduler / worker,
notifications, messaging, modern routes, team / provider / supervisor models, accounting, outbox, a future adjustment engine.

## Known limitations

* The outcome is dynamic: a reversal can turn `fulfilled` into `open` / `broken` on a later read; there is no sticky history of it.
* A promise whose cap was met at creation can become unfulfillable if the debt later falls by adjustments the system does not model yet.
* A fulfilled or broken promise stays "current" until it is explicitly replaced (it blocks a plain create by design).
* Reading computes the ledger per request; reads are live, with no snapshot across pages.
* Direction for T-016: read-only worklist enrichment (`current_promise_*`, projected status, broken filter, `last_activity_*`).
