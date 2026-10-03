# T-010 — Credit Overdue Projection v1

Official identity: **T-010 CREDIT_OVERDUE_PROJECTION**. It derives overdue obligations and overdue debt from the net ledger and
projects the stored loan status. It is **not** `CREDIT_DELINQUENCY_ACCRUAL`: it calculates no late fee, creates no money and
adds no scheduler.

## Overdue is not a delinquency charge

| | Overdue (this package) | Delinquency charge (NOT implemented) |
|---|---|---|
| meaning | the debt is past its effective due date | an economic late fee / accrual |
| depends on | `business_date`, effective `due_date`, net outstanding | base, rate, frequency, cap, rounding, grace (all BLOCKED_BY_SPEC) |
| money | none | would create facts |

An obligation can be overdue with a delinquency charge of 0, and a product with `delinquency.enabled = false` (so
`delinquency_starts_on = NULL`) still has overdue obligations and a `past_due` loan. The delinquency grace days do **not** move
the due date and do not delay overdue: they could only affect a future charge. `delinquency_starts_on` is never read.

## The rules (pure code in `allocation.py`)

* **Overdue obligation**: `business_date > effective due_date` **and** net outstanding > 0. The first overdue day is the
  calendar day after the effective due date. `due_date` is the effective date persisted by T-007 (the calendar A/B/C is never
  recomputed; the contractual date is never used).
* **days_overdue**: 0 while not overdue (including the due date itself and a settled obligation); otherwise
  `business_date - due_date` (1 the day after, 5 five days after). The grace is not subtracted: it measures the age of the due
  date, not chargeable days.
* **Overdue outstanding**: net outstanding of the overdue obligation (principal + interest + fees + the contractual
  delinquency component, which is 0), computed per component and clipped at 0.
* **Loan status** (the ONE function `allocation.loan_status`): 1. net outstanding of everything is 0 → `paid`;
  2. any overdue obligation → `past_due`; 3. otherwise `active`. The current status never takes part in the decision.

## Source of truth

`ledger.applied_by_obligation` (applications − reversal applications) over the contractual obligations, at the business date
of the **frozen contract timezone** (`ledger.business_date`; never `date.today()`, never the UTC date; the 23:30-local /
next-day-UTC edge is tested). Never `obligation.status`, `loan.status`, raw payment or application rows, legacy balances,
`LoanSettings` or live product rules. A retired / deactivated product changes nothing.

## Read model (derived, never stored)

* Schedule rows: `is_overdue`, `days_overdue`, `overdue_outstanding`, plus the schedule `business_date`.
* Balances (also inside the loan detail `balances`): `overdue_obligations`, `overdue_outstanding`, `max_days_overdue`
  (the largest `days_overdue` among overdue obligations) and `projected_status` (the status the rule gives today).
* The loan **list** carries only the stored status (it would need one ledger read per loan).

Every GET is pure: 0 INSERT/UPDATE/DELETE (listener-tested), even when the stored status lags the derived overdue state.

## Stored status and its staleness (limitation)

`credit_loans.status` is a stored **projection**, not the truth. It is updated only by domain operations that already hold the
loan lock: an explicit assessment, a payment and a payment reversal. Time passing does not write anything, so the stored status
can lag until one of those runs. Consumers that need the current overdue condition must use the derived fields
(`is_overdue`, `overdue_*`, `projected_status`) and not assume that `loan.status` alone reflects today's clock.

## Payments and reversals

* Payments are accepted on `active` **and** `past_due` loans (still oldest-effective-due-first and the frozen
  `allocation.order`; no delinquency is generated). After the payment the common projection runs:
  `past_due → past_due | active | paid` as the net ledger dictates.
* Reversals are accepted on `active`, `past_due` and `paid` loans and run the same projection in the same transaction. A
  reopened debt that is already overdue ends **directly** in `past_due` (no temporary `active`, no later assessment needed);
  one that is not yet due gives `paid → active`. T-009 tests were adapted to this rule (they used to expect `active`).
* Audit convention (option A, no duplicate facts): `payment.confirmed` and `payment.reversed` now carry
  `previous_loan_status` next to the existing `loan_status`; no separate status event is written for them.

## Explicit assessment command

`POST /api/v2/loans/{loan_id}/delinquency/assess` (the path is kept from the API proposal; it calculates **no** charge).
Steps: tenant/auth → loan row `FOR UPDATE` (tenant-filtered, foreign loan = 404) → permission on the **managing branch** →
frozen contract integrity → obligations `FOR UPDATE` by ascending sequence → business date (contract timezone) → net ledger
re-read after the lock → projection → write only if the stored status changes → audit only if it changes → commit.
Response: `loan_id, previous_status, status, changed, business_date, overdue_obligations, overdue_outstanding,
max_days_overdue`. Always HTTP 200. No idempotency key: it is a deterministic projection; re-running it on an unchanged state
writes nothing and audits nothing. It creates no fees, accruals, applications, payments or cash movements.

* **Permission** `loans.delinquency.assess` (sensitive), checked on `loan.managing_branch_id` only (never origin,
  disbursement or receiving branch). A loan with no managing branch needs a tenant-wide grant. It does not authorize charging
  mora, editing obligations, waivers or accounting.
* **Audit** `loan.delinquency_assessed` (only on a real change): tenant, actor, correlation id, loan id/number,
  previous/resulting status, business date, overdue count, overdue outstanding, max days overdue, managing branch, rules and
  contract digests; no PII.
* **Lock order**: loan → obligations (sequence ASC). Cash does not participate. Races with another assessment, a payment or a
  reversal are serialised by the loan lock (real PostgreSQL tests).
* **No batch, no scheduler, no worker, no cron, no background task** (no new dependency). A future scheduler can call
  `overdue.assess` loan by loan, one transaction per loan, loan lock first.

## Migration 0012

`0012_credit_overdue_projection`: no table, no column, no trigger: it only inserts the permission and grants it to the tenant
admin roles. Downgrade removes the permission (there is no economic history to protect; loans stored as `past_due` remain valid
values of the existing CHECK). `credit_loan_obligations.delinquency_due` stays contractual, immutable and 0.

## Legacy

`refresh_loan_state`, `LoanSettings` and the legacy late-fee fields are never called, read or written
(REBUILD → REMOVE_LATER; untouched). `cash_service.py` is untouched; no cash movement is created.

## BLOCKED_BY_SPEC (economic, D6–D13, still open)

Semantics of the delinquency start for charging (`delinquency_starts_on`), calculation base (gross vs net, components),
frequency (`once`, `per_day`, `per_week`, `per_month`), cap, delinquency rounding, economic recognition (accrual vs
assessment), backdating and catch-up, retroactive effect after a reversal, waivers/forgiveness, ageing-bucket policy,
delinquency accrual and its accounting treatment; plus everything pending from T-006 to T-009.

## BLOCKED_BY_EVIDENCE

Scheduler/worker, accounting ledger, outbox, mature tenant/branch calendar configuration, regulatory validation of mora
caps/policies.

## Future dependencies

`CREDIT_DELINQUENCY_ACCRUAL` (append-only accrual facts reading this same net ledger, with the T-008 component trigger and
`ledger.views` adapted to include them), batch assessment + scheduler, collections (consumes `days_overdue` /
`overdue_outstanding`), accounting/outbox.
