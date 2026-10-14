# T-020 — Field cash refund

Official identity: **T-020 FIELD_CASH_REFUND**. It adds the PHYSICAL return to the customer of a reversed FIELD payment:
one table (`credit_field_refunds`, migration `0019`), one permission, one Cash port operation, one command and one list
read. No discrepancy incident, no shortage/overage write-off or suspense, no partial refund, no CashPoint rebuild, no
accounting, outbox or mobile work.

## Reversal is not refund

T-009 reverses the DEBT; it never proves the customer got cash back. A refund is its own durable command and history:

* It requires a **committed `CreditPaymentReversal`** of a **field** payment (the endpoint is anchored on the reversal).
  No reversal, no refund; there is no combined reverse + refund command.
* **Full only**: amount = payment = reversal = custody receipt (composite FKs); the request carries no amount.
* **Field only**: a counter reversal already withdrew its cash in T-009 — a refund of it is a 409
  `field_refund_not_applicable`, never a second withdrawal.
* **Pre-custody** field payments (recorded before T-019, no receipt): 409 `pre_custody_not_refundable`. Where that cash
  is cannot be known and is never guessed.

## The source is derived, never sent

From the receipt's custody history (the client never sends `source_kind`, amount, custodian, payment, branch or currency):

| Custody of the receipt | Source | What happens |
|---|---|---|
| live item in a **declared** rendition | — | 409 `receipt_in_declared_rendition`: cancel or reject it first (no hidden cancellation) |
| no live item (never rendered, rejected, cancelled) | `collector` | the custodian hands the cash back; no session, no Cash movement |
| live item in an **accepted** rendition | `branch_cash` | ONE negative movement `credit_field_refund` from the refunder's own current session |

* **Collector source**: the actor must be exactly the receipt's custodian, active, with `cash.field_custody.refund` on the
  receiving branch; `cash_session_id` must be absent (422 `refund_session_not_applicable`). The refund is the receipt's
  terminal exit from collector custody: outstanding custody no longer counts it, and it can **never be rendered** (service
  409 `receipt_refunded`, backed by the extended T-019 item insert trigger). The receipt itself is never updated.
* **Branch source**: the actor needs `cash.field_custody.refund` and an explicit `cash_session_id` (422
  `refund_session_required` without it) that is open, belongs to the box of the receiving branch, is the actor's own and has
  enough cash. It is the CURRENT session (T-009 precedent), never the original rendition's session (which may be closed),
  never "the latest". `cash.port.withdraw_field_refund` (box then session, fixed kind) writes the movement and CashAudit.
* **No `reverses_id`**: one accepted rendition deposit may aggregate many payments refunded independently, and
  `reverses_id` is one-to-one. The link is refund → `cash_movement_id`, and refund → payment / reversal / receipt; the
  accepted rendition is derived through the receipt's item and stays untouched.

**Exactly one way out of collector custody**: an accepted rendition XOR a collector refund (refund insert trigger + item insert
trigger + the shared receipt row lock). A branch refund after an accepted rendition is a second-stage outflow of branch cash; it
never reopens collector custody nor releases the accepted item.

## Authority (H10)

`cash.field_custody.refund` is a separate sensitive permission (not `payments.reverse`, `.accept` or `.render`). T-020 does
**not** forbid the reverser from also refunding: for collector-held cash the refunder must be the physical custodian, so a
hard inequality could make the refund impossible. Separation of duty comes from assigning the two permissions to different
roles; the audit event records both `reversed_by` and `refunded_by`.

## Rejected rendition = all cash returned (H2/H3)

Operating rule confirmed for T-019/T-020: when a cashier rejects a rendition, they physically hand ALL counted cash back to
the custodian before completing the rejection. Nothing enters the session, nothing stays with the cashier, the receipts
return to the collector's full custody, and `counted_amount` is only a historical observation. Mismatched counts are never
accepted; shortages and overages are never booked. There is no discrepancy state.

## Outstanding custody V2 and user deactivation

A receipt is outstanding iff it has no live item in an accepted rendition AND no collector refund. `has_open_custody` (user
disable guard, modern and legacy) uses the same equation plus declared renditions, in one query: a collector who refunded
their last receipt can be disabled. A branch refund does not change collector custody.

## Idempotency, locks and concurrency

`UNIQUE(tenant, idempotency_key)` + digest of (operation, reversal, derived source, session, reason): the same key and
content replays the stored answer (no second movement or audit); the same key with other content is 409; a new key after
the refund is 409 `already_refunded`. `UNIQUE(reversal_id)`, `UNIQUE(payment_id)`, `UNIQUE(receipt_id)` are the backstop.
Lock order: the reversal (read) → the RECEIPT row `FOR UPDATE` (the same row T-019 `declare` locks) → for branch source the
Cash port's box then session. Never a loan or obligation lock: a refund changes no debt. Refund vs declaration of the same
receipt serialise on the receipt row (exactly one wins); refund vs refund: one wins; refund vs session close: the port decides.

## Reads

* `GET /api/v2/payments/{id}/field-custody` adds `physical_state` (`collector_custody`, `rendition_declared`, `branch_cash`,
  `refunded_from_collector`, `refunded_from_branch`), `reversed`, `refund_pending` and a `refund` block (ids, source, amount,
  session, movement, time). Derived, never stored.
* `GET /api/v2/cash/field-refunds?status=pending|refunded&receiving_branch_id=` (keyset, one query per page, T-019 read
  scope): `pending` = committed field reversals with a receipt and no refund, with the physical state telling who must act.

No customer data anywhere. GETs never write or audit.

## Command

`POST /api/v2/payment-reversals/{reversal_id}/field-refund` {idempotency_key, reason (3..500), cash_session_id?}. The
`refund_number` (RFD-000001) is a technical reference, not a fiscal receipt. No proof, signature or fiscal document in v1.

## Database

`credit_field_refunds`: composite FK to the reversal (tenant, id, payment, loan, amount, origin, currency, branch — a new
unique target on `credit_payment_reversals`, constraint only) with `origin = 'field'`; composite FK to the receipt (tenant,
id, payment, branch, custodian, currency, amount); CHECKs (DOP, cent-exact, source domain, session/movement iff branch,
collector refunds own, reason length); insert trigger (source vs live item state); append-only guard; deferred check that a
branch refund is backed by its `credit_field_refund` movement (amount = -refund, same session). Migration `0019`; downgrade
refused while refunds exist.

## Not in T-020

Shortage responsibility, write-off, overage suspense, cashier retention of mismatched cash, partial refunds, refunds
without reversal, non-customer dispositions, pre-custody refunds, proof/signature/fiscal documents, collector handoff,
cross-branch custody, branch close policy, CashPoint runtime (the port still targets one legacy CashBox per branch),
accounting, outbox, mobile.
