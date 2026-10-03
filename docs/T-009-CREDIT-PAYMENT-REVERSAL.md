# T-009 — Credit Payment Reversal v1

Official identity: **T-009 CREDIT_PAYMENT_REVERSAL**. A confirmed T-008 payment is *compensated*, never edited or deleted:
the history "this payment happened, then it was reversed" stays observable forever.

## Economic model (append-only)

```
payment (immutable) + payment applications (immutable)
      + reversal (append-only, UNIQUE per payment) + reversal applications (append-only, one per original application)
net paid = applications - reversal applications          debt = contractual amounts - net paid
```

* `credit_payments` / `credit_payment_applications` (T-008) are **not modified**. `credit_payments.status` stays `confirmed`;
  "reversed" is **derived** from the existence of the reversal row (`reversed`, `reversal_id`, `reversal_number` on the reads).
* `credit_payment_reversals`: id, tenant, payment, loan, `reversal_number` (`REV-000001`, per tenant; technical reference,
  not a fiscal document), amount (= the payment's amount, never from the client), currency, origin, free-text `reason`
  (3–500 chars; no reason codes exist), `reversed_by`, `reversed_at`, `business_date` (contract timezone),
  `reversal_branch_id`, `cash_session_id` / `cash_movement_id` (counter only), idempotency key + digest.
* `credit_payment_reversal_applications`: one row per original application, repeating its obligation, component and amount.
* Alternatives rejected: negative applications in the same table (breaks `amount > 0`, the exact-sum trigger and the
  per-component uniqueness of T-008, i.e. would weaken proven invariants); reversal as a negative payment (same `amount > 0`
  CHECK, breaks `PAG` numbering); a status column (the payment table is immutable).

## What the database enforces (not only the service)

| Invariant | Mechanism |
|---|---|
| one reversal per payment | `UNIQUE(payment_id)` |
| **full** reversal only | composite FK binds `(amount, origin, currency, reversal_branch_id)` to the payment's own `(amount, origin, currency, receiving_branch_id)` |
| reversal branch = receiving branch | same FK |
| exact mirror | composite FK binds `(original_application_id, obligation, component, amount)` to the original application; `UNIQUE(original_application_id)` |
| sum of reversal applications = payment amount | deferred constraint triggers (COMMIT): an incomplete reversal cannot commit |
| counter reversal backed by cash | CHECK counter ⇔ session+movement, and a deferred trigger: movement kind `credit_payment_reversal`, amount `-amount`, same session, `reverses_id` = the original receipt |
| no over-application | the T-008 trigger now checks the **net** sum (`applications - reversal applications <= contractual amount`) |
| immutability | `origination_append_only` guard on both new tables |
| tenant safety | composite FKs through `tenant_id` |

## Net ledger (one helper)

`ledger.applied_by_obligation` is the **only** place where "paid" is computed (`applications - reversal applications`).
Balances, schedule, status projection, due-to-date and the payment command read it. `ledger.project` re-derives every
obligation status from the net amounts and the loan flag: all paid → `paid`; a `paid` loan whose debt reappeared → `active`
(the only new loan transition; `past_due`, `restructured`, `refinanced` stay out). A status can go `paid → partially_paid →
pending` only as a consequence of the net source of truth; a test rebuilds all statuses from scratch.

## Flows

Both flows run in ONE transaction and take the locks **loan → obligations (ascending sequence) → payment → cash box → cash
session** (a field reversal stops before Cash; every future debt-changing package must also take the loan first).

* **Counter** (cash leaves a drawer): loan lock → obligations → payment → branch/permission/session validation →
  `cash_port.withdraw(kind=credit_payment_reversal, reverses_id=<original receipt>, reference=REV-…)` → reversal row →
  mirror applications → projection → audit → one commit. Never the legacy kind `reversal`, never the legacy reversal.
* **Field**: loan → obligations → payment → reversal row → mirror applications → projection → audit. No Cash movement, no
  session. The physical return of the money by the collector is outside the current Cash Runtime (no rendition exists);
  future custody/rendition calculations must use field payments **net of reversals**.

## Cash session rules (D1, D2)

* The reversal branch is explicit in the request and must equal the payment's **receiving branch** (never origin, managing,
  disbursement or an arbitrary branch of the actor). It is persisted and audited.
* The cash session is explicit, must be **open**, belong to that branch's box, belong to the **executing actor**
  (`cash_sessions.cashier_id`), and hold enough cash (`insufficient_cash`). It can be a different session/cashier than the
  original one. The original session — open or closed — is **never touched or reopened**; the cash physically leaves the
  executing cashier's current drawer, representing the money returned to the customer.
* No suitable open session → rejected. Nothing is written.

## Other rules

* **Later payments are never reallocated** (D7). P1 covered installment 1, P2 covered installment 2; reversing P1 reopens
  installment 1 and leaves P2's rows byte-identical. The next payment applies oldest-effective-due-first, so it covers the
  reopened debt. Re-allocating history would be a separate package.
* **`external_reference` is not released** (D6): the reversed payment still exists and keeps its reference; the corrected
  payment needs a new reference or none. The T-008 uniqueness is unchanged.
* **Corrections** = reverse the original + create a new, correct payment. There is no adjustment operation, no partial
  reversal (no amount, no component selection; unknown request fields are a 422), no void (T-008 payments are born
  `confirmed`; there is no pending state).
* **Idempotency**: `(tenant, idempotency_key)` + digest of `(payment, reason, reversal branch, cash session)`. Same key and
  digest → HTTP 200 replay with zero writes (no new REV number, movement, applications or audit). Same key, other digest →
  409 `idempotency_conflict`. Another key on a reversed payment → 409 `payment_already_reversed`; `UNIQUE(payment_id)` is the
  database backstop.
* **Authorization**: `payments.reverse` (sensitive, separate from `payments.create` / `payments.read`), required within the
  scope of the payment's receiving branch (which is also the reversal branch). No maker-checker, no monetary limits in v1.
  GETs use `payments.read` and are pure (listener-verified).
* **Audit**: `payment.reversed` (tenant, actor, correlation id, payment/reversal ids and numbers, loan, amount, currency,
  business date, receiving and reversal branch, origin, reason, session and movements, component totals, rules and contract
  digests; no customer identity; none on replay). Cash keeps its own audit row.
* **Legacy isolation**: nothing reads or writes legacy `payments`, `loans`, `loan_installments`, `capital_movements`,
  `cash_deliveries`, `cash_allocations`, `cash_transfers`; the legacy cash reversal cannot reverse `credit_payment_receipt`,
  `credit_payment_reversal` or `credit_disbursement` (tested). `cash_service.py` is untouched. The Cash port only gained
  `reverses_id` and an optional session-ownership check on `withdraw`.

## API

`POST /api/v2/payments/{id}/reversals` (`idempotency_key`, `reason`, `reversal_branch_id`, `cash_session_id` for counter) ·
`GET /api/v2/payments/{id}/reversal` · payment reads expose `reversed`, `reversal_id`, `reversal_number`.

## Migration 0011 and downgrade

Creates the two tables, two unique FK targets on T-008 tables, the guards, the deferred triggers, the net version of the
over-application function and the permission. **Downgrade 0011 → 0010 fails explicitly when `credit_payment_reversals`
contains rows**: dropping them would make reversed payments look applied again while the compensating cash movements stay in
the Cash ledger (not owned by Alembic). On a database without reversals the downgrade/re-upgrade is clean (CI uses that).

## BLOCKED_BY_SPEC

Partial reversal; generic adjustments (and wrong business date, since backdated payments are undefined); void / pending
payments; maker-checker and monetary limits for reversals; reason-code catalogue; backdated payments/reversals; fiscal/legal
receipts; rendition and mature collector custody; prepayment; payoff; delinquency execution; accounting; methods beyond cash;
assignment/portfolio rules; re-allocation of later payments; releasing a reversed payment's external reference; unresolved
T-006/T-007 items.

## BLOCKED_BY_EVIDENCE

Bank/provider/transfer reversal; non-DOP cash; accounting and outbox; mature rendition infrastructure; offline flows.

## Future dependencies

* **Cash Runtime**: the compensating movement is a normal Cash outflow; a rebuilt Cash only has to honour the port.
* **Delinquency**: reads the net source of truth, so a reversal makes overdue base reappear without any special case.
* **Accounting**: the facts it can consume are the original payment, its applications, the reversal, the reversal
  applications, the movement pair linked by `reverses_id`, and the audit events — no GL entries are created here.

> **T-010 update**: the loan status after a reversal now comes from the common projection (`paid > past_due > active`), so a
> reopened debt that is already overdue (`business_date > effective due_date`) ends directly in `past_due`; otherwise
> `paid → active`. See `T-010-CREDIT-OVERDUE-PROJECTION.md`.
