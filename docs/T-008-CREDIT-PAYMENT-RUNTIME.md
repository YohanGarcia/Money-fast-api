# T-008 — Credit Payment Runtime v1

Official identity: **T-008 CREDIT_PAYMENT_RUNTIME** (Payment Runtime v1). The earlier plan name for T-008, "Cash Runtime &
Reconciliation", is superseded in numbering; its new number is decided later. Nothing of Cash Runtime is built here.

Flow: `money received → confirmed payment → contractual allocation → payment applications → derived balances → obligation
projection → loan paid when fully settled`, all in ONE transaction, without touching the legacy `payments` / `loans` /
`loan_installments` tables.

## Source of truth

1. `credit_payments` (immutable, confirmed)
2. `credit_payment_applications` (append-only: payment → obligation → component → amount)
3. `credit_loan_obligations` (contractual amounts; immutable except the projected `status`)
4. the frozen contract (T-005 snapshot inside the T-006 formalization) for the allocation order

There is **no** stored `principal_balance` / `interest_balance` / `late_fee_balance` or any other balance. Every figure
is `contractual due − sum(applications)` (`ledger.py`, `allocation.py`). `obligation.status` and `loan.status = paid` are
**projections**, rebuilt from obligations + applications only (a test rebuilds all statuses from scratch).

## Allocation (pure code in `allocation.py`)

* Payable set: obligations whose **effective due date ≤ business date** (business date = the contract's frozen calendar
  timezone, never the UTC date). Order: effective due date ascending, ties by sequence. Never the contractual date, never a
  future obligation.
* Inside an obligation, components follow exactly the frozen `allocation.order` (a contract whose `apply_by` is
  `component_then_installment` is refused with 409 `allocation_policy_blocked_by_spec`: only the confirmed
  installment-by-installment reading is defined).
* **Excess rule (v1):** the maximum payment is the outstanding amount of the payable obligations. More is refused with 422
  `payment_exceeds_due_amount`: no advance, prepayment, principal reduction, payoff or future-interest handling.
* A partial payment is allowed and consumed deterministically and **exactly**; the obligation moves
  `pending → partially_paid → paid`; the loan moves `active → paid` only when every obligation is paid.
  `delinquency_due` takes part if it exists (today it is 0): nothing computes or accrues it, and `past_due` is not set.

## Counter vs field

| | counter | field |
|---|---|---|
| cash | `cash/port.py::deposit` into an open session of the receiving branch's box (kind `credit_payment_receipt`) | none |
| confirmation | immediate | immediate (DR-002) |
| custody | cash session | stays with the collector (the future cash package derives it: field payments minus rendition) |

`credit_payment_receipt` is deliberately not `counter_payment`: the legacy cash reversal accepts `counter_payment` and would
reverse only the cash, leaving the debt applied. A test calls the legacy reversal on the new movement and it is refused.

## Rules

* `receiving_branch_id` is mandatory and explicit (same tenant, active, in the actor's `payments.create` scope); never
  inferred from origin / managing / disbursement branch. A loan of branch A can be paid at branch B; the origin branch
  gets no movement.
* Currency = the loan's currency (composite FK `(tenant, loan, currency)`); cash is RD$ only (the legacy cash ledger has no
  currency) → anything else is rejected.
* Technical receipt: `PAG-000001` (per-tenant sequence). It is **not** a fiscal/legal receipt, invoice, NCF or tax
  document; the legal form is BLOCKED_BY_SPEC.
* `payments.create` / `payments.read` are separate from approve / formalize / disburse. Collector assignment is not
  enforced (permission + branch scope only; that belongs to Collections).
* Visibility of a payment: from its **receiving branch** and from the loan's origin, managing and disbursement branches.
  The list endpoint applies the same rule.

## Idempotency / concurrency / atomicity

* Boundary `(tenant, idempotency_key)` + a digest of loan, amount, currency, method, origin, receiving branch, cash session
  and external reference. Same key + digest → 200 replay (nothing new: payment, applications, movement, number, audit).
  Same key + other digest → 409 `idempotency_conflict`. Another key with an equivalent body is a legitimate additional
  payment (no arbitrary de-duplication); `external_reference`, when present, is unique per tenant.
* **Lock order (every later package that changes debt must follow it, loan first):** loan row `FOR UPDATE` → obligations in
  ascending sequence → cash box → cash session. A `credit_loans` lock serialises payment vs payment and will serialise
  payment vs reversal / payoff / delinquency job.
* Database invariants (deferred constraint triggers, checked at COMMIT): the applications of a payment add up EXACTLY to its
  amount; no component of an obligation can be applied beyond its contractual amount (the trigger locks the obligation row
  first, so two transactions serialise and the second re-reads the first's rows). Plus the service validation and the loan lock.
  Limit: the over-application guarantee relies on the trigger's own obligation-row lock and the loan lock; a hand-written
  SQL session that bypasses both and commits concurrently with an application transaction is the one case not proven.
* Counter payment: cash inflow + payment + applications + projection + audit commit together; the Cash port never commits.

## Audit and purity

`payment.confirmed` (actor, tenant, correlation id, payment/loan ids and numbers, amount, currency, business date, receiving
branch, origin, method, session and movement ids, component totals, rules and contract digests; no customer identity;
no replay events) + the cash side's own audit row. GET endpoints never write (SQL listener test).

## Legacy classification

Legacy `payments`, `loans`, `loan_installments`, `loan_service`, `payment_service`, `refresh_loan_state` (writes on GET),
the legacy cash payment/transfer paths: **REBUILD → REMOVE_LATER**, untouched, never read nor written by this package.
`cash_service.py` is not modified. Migration 0010 downgrade drops the new tables; the cash movements of counter payments
stay in the cash ledger (not owned by the migration) and no economic reversal is invented.

## BLOCKED_BY_SPEC

Advance / early principal reduction / prepayment / payoff / discounts, backdated payments, fiscal or legal receipt format,
payment reversal and adjustments, delinquency execution, payment-method configuration beyond v1 (cash), collector/customer/
portfolio assignment, accounting rules, offline collection. T-006/T-007 open items stay open.

## BLOCKED_BY_EVIDENCE

Bank / provider / transfer collection (registry only, no ledger or provider), non-DOP cash, offline collection infrastructure,
accounting, outbox/event bus.

## Future dependencies

Payment reversal (cash compensation + application reversal semantics), delinquency runtime (reads applications, takes the loan
lock first), cash runtime and collector rendition (rebuild on `credit_payments`), prepayment/payoff, collections, accounting/outbox.
