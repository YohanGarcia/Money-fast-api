# T-019 — Field cash custody and rendition

Official identity: **T-019 FIELD_CASH_CUSTODY_AND_RENDITION**. It records who physically holds cash collected in the field
and how that cash enters a cashier's drawer. It adds three tables (migration `0018`), three permissions, one Cash port
operation, four commands and five reads. No refund, no shortage/overage resolution, no partial rendition, no legacy Cash
merge, no accounting, outbox, GPS or mobile work.

## Three truths, kept apart

| Truth | Where it lives | Changes when |
|---|---|---|
| Economic (debt applied) | `credit_payments` + applications (T-008), reversals (T-009) | a payment / reversal is recorded |
| Physical custody | `credit_field_custody_receipts` + renditions (T-019) | a field payment is born; a rendition is accepted |
| Branch cash session | legacy `cash_sessions` / `cash_movements` through the Cash port | a counter payment; an accepted rendition |

A payment applied to debt does not prove the cash is in a drawer. A reversal of the debt does not prove the customer got the
money back. Neither ever changes custody.

## Custody birth and the custodian

* A `CreditPayment(origin="field")` creates exactly ONE `CreditFieldCustodyReceipt` **in the same transaction**
  (`payments.pay` → `field_custody.receipts.create_receipt`). If the receipt fails, the payment rolls back entirely. A
  counter payment creates none (its cash entered a session when collected).
* **Custodian = `payment.collected_by` = the authenticated actor.** There is no proxy collection: no request field names
  another collector (unknown fields are a 422). Whoever records a field payment — a supervisor or an admin included — is its
  physical custodian. This is the v1 operating rule.
* Recording a field payment needs `payments.create` **and** `cash.field_custody.render` on the receiving branch (nobody can
  hold cash they cannot hand in). Counter payments still need only `payments.create`. The custodian must be `active`
  (the user row is read `FOR SHARE`).
* The receipt snapshots the payment exactly (tenant, loan, amount, DOP, receiving branch, custodian, received_at); a
  database trigger rejects any receipt that does not match a FIELD payment. Receipts are immutable and never deleted.
* **No backfill.** Field payments recorded before `0018` have no receipt and stay untracked: a payment does not prove its
  cash is still with the collector, so inventing receipts would create false cash assets. Their custody read answers
  `tracking_status: "pre_custody"` and makes no custodian, outstanding or rendered claim.

## Rendition

* Indivisible: one receipt = one payment's full amount. No partial rendition, no FIFO, no client amount: items repeat the
  receipt's amount through a composite FK, and `declared_amount` = SUM(items) (server-derived, checked at commit).
* Lifecycle: `declared` → exactly one of `accepted` | `rejected` | `cancelled` (terminal). No draft, reopen or discrepant.
* **Maker-checker.** Declare and cancel: the custodian only (every selected receipt must be theirs, same branch, DOP, not
  claimed). Accept and reject: a different user with `cash.field_custody.accept` on the branch. No admin override.
* **Exact acceptance only.** `counted_amount` must equal `declared_amount`; otherwise the accept is a 409 and nothing
  moves. Short or over: the cashier rejects (mandatory reason; `counted_amount` may be recorded as an observation and books
  nothing). Rejected and cancelled renditions release their items (false → true, once); the receipts are outstanding again
  and can be re-declared. Missing cash simply stays as the custodian's outstanding custody; excess cash is never booked.
* **Session.** The accept body names `cash_session_id` explicitly (never the latest open one). It must be open, belong to
  the box of the rendition's branch and to the acceptor (`cashier_id`). Cross-branch rendition does not exist.
* **Cash port.** `cash.port.deposit_field_rendition` (box then session `FOR UPDATE`, DOP, cent-exact, open, owned by the
  acceptor, fixed kind `credit_field_rendition`, never commits) writes the ONE positive movement of an accepted rendition.
  The custody module never writes a cash session. The legacy cash reversal does not list this kind, so it refuses it.
* A deferred trigger checks at commit that an accepted rendition is backed by its movement (kind, amount, session), that
  declared/accepted renditions release nothing and that rejected/cancelled ones release everything. Guards allow only the
  single declared → terminal transition and the single item release.

## Outstanding custody

* Per receipt: outstanding = its full amount unless it has a live item in an **accepted** rendition. A receipt in a
  `declared` rendition is still the custodian's cash, shown as `pending_rendition_id`; it is not branch cash.
* Per custodian: SUM of outstanding receipts. Reversals are never subtracted.
* Accepted branch cash: one `credit_field_rendition` movement per accepted rendition, amount = declared = counted.

## Reversal (T-009) and refund

Before rendition the debt is restored and the cash stays with the custodian (receipt outstanding, still renderable). While
declared, the rendition stays valid. After acceptance, the cash stays in the session: no withdrawal, no release, no refund.
A field reversal never calls the Cash port. Customer refund/disposition is out of scope (T-020 candidate). The T-009 note
"custody = field payments net of reversals" is superseded; the T-009 runtime is unchanged.

## Idempotency and concurrency

* Declare: `(tenant, create_idempotency_key)` + digest of (operation, custodian, branch, sorted payment ids). Decisions: a
  SEPARATE `(tenant, decision_idempotency_key)` (partial UNIQUE) + digest of (rendition, operation, payload). Same key and
  content replays the stored answer (never a second movement); same key with other content, or any new key after the
  terminal transition, is a 409.
* Lock order. Field payment: the existing loan → obligations order, then the receipt (and the custodian row `FOR SHARE`).
  Declare: the selected receipts `FOR UPDATE`, ascending id — never a loan or obligation. Decisions: the rendition row,
  then (accept) the Cash port's box and session. Two declarations of one receipt: one wins (row lock + partial UNIQUE on live
  items). Two decisions: one terminal transition, at most one movement. A session closed before the accept fails in the port.
  Reversals share no lock with custody (independent truths). READ COMMITTED, no isolation change.

## Authorization and scope

`cash.field_custody.read` (branch reads), `cash.field_custody.render` (field collection, declare / cancel own custody),
`cash.field_custody.accept` (accept / reject; accepting also needs the explicit owned session). All tenant-scope definitions,
branch-scoped grants; render and accept are sensitive. The receiving branch is the boundary: collection assignment, routes and
the managing branch are irrelevant. A custodian with render reads their OWN custody only; ownership never widens branch access.
Foreign tenants get 404.

## User deactivation

Disabling a user (modern `POST /api/v2/users/{id}/disable`, and the legacy v1 user update) is refused with 409
`outstanding_field_custody` while the user has an outstanding receipt or a declared rendition (one query,
`field_custody.service.has_open_custody`). The user row is locked first, so a concurrent field payment or declaration
serialises with it. Custody is never transferred silently. The legacy `user_has_pending` check is untouched; this one is
additive. Reads never hide custody because its custodian later became inactive.

## API

Commands: `POST /api/v2/cash/field-renditions` {idempotency_key, receiving_branch_id, payment_ids[]} ·
`POST /api/v2/cash/field-renditions/{id}/accept` {idempotency_key, cash_session_id, counted_amount} ·
`.../{id}/reject` {idempotency_key, reason, counted_amount?} · `.../{id}/cancel` {idempotency_key}. No PATCH.

Reads (pure, no PII, constant query count): `GET /api/v2/cash/field-custody` (outstanding, keyset, filters branch /
custodian) · `GET /api/v2/cash/field-custody/summary?receiving_branch_id=` (per custodian) · `GET /api/v2/cash/field-renditions`
(keyset, filters branch / state) · `GET /api/v2/cash/field-renditions/{id}` · `GET /api/v2/payments/{id}/field-custody`
(`outstanding` | `pending_rendition` | `rendered` | `pre_custody` | `not_applicable` for counter).

## Audit

`record_event`: `field_custody.rendition_declared` / `_accepted` / `_rejected` / `_cancelled` with ids, amounts, currency,
branch, custodian, actor, session and movement; never on replays; no customer data. Receipt birth is evidenced by the
receipt row itself and the existing `payment.confirmed` event (origin `field`); no extra event. The Cash port writes its own
`CashAudit` row on acceptance.

## Legacy coexistence

Modern custody lives only in `credit_field_*`. Nothing is written to `CashDelivery`, `CashAllocation` or legacy payments,
and the legacy `pending()` is never a source of modern custody. Limitation: legacy closing snapshots / reports and the
legacy `user_has_pending` remain blind to modern outstanding custody; accepted renditions are visible in the session through
their movement, outstanding custody through the T-019 reads.

## Evidence

Read plans were checked on a throwaway PostgreSQL 17 with the final schema (see the delivery report): collector page and
total, branch summary, declared queue, rendition detail and payment custody detail are served by the four indexes above.
No production SLO is claimed; no read model is needed.

## Not in T-019 (BLOCKED_BY_SPEC)

Customer refund / disposition; shortage / overage resolution and write-off; collector-to-collector handoff; cross-branch
custody transfer; branch closure with outstanding custody; partial rendition; historical opening balances; legacy Cash merge;
multi-currency cash; the CashBox → CashPoint runtime; accounting; outbox; GPS / routes; mobile.
