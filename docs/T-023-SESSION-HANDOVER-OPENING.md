# T-023A — Same-CashPoint session handover / opening from handover

Official identity: **T-023A SESSION_HANDOVER_OPENING** (parent package **T-023**, internal lineage **CASH-CORE-03**).
A closing session may leave its **exact counted cash** with a **named next cashier of the same CashPoint**, who recounts it and
opens their own session **in the same transaction**. Migration `0022_session_handover_opening`. Backend v2 only.

## Scope

Supported, and only this:

```
source session on CashPoint X ──close(destination = next_session)──▶ pending direct handover (named next cashier)
        └─ the named receiver recounts ─▶ accept ─▶ source CLOSED + receiving session OPEN (same CashPoint X)
```

Not supported (frozen): another CashPoint or branch, cross-branch transit, arbitrary point-to-point transfers, open-to-open
transfer, partial transfer, anonymous receiver, receiver discrepancy accounting, T-022B accounting, Web, Mobile.

## Frozen decisions

| id | decision |
| --- | --- |
| D1 | The destination CashPoint equals the source CashPoint (service, composite FK and trigger). The `closing` source itself reserves the CashPoint through the existing active-slot index: no separate reservation exists. |
| D2 | `close` takes `destination: "capital" \| "next_session"` (default `capital`). `next_session` needs `counted > 0` and creates ONE pending direct handover of the counted cash (`next_session_requires_cash` otherwise). The source difference is a record and is never netted: expected 31,000 / counted 30,000 hands over 30,000 and keeps the -1,000 difference. |
| D3 | No receiving session exists while pending. Accept and open are ONE transaction. `opening_source = handover` exists only through accept (service, `CHECK`s, insert trigger); `POST /sessions/open` still accepts `zero` / `capital` only. The receiving session is `open`, same tenant / branch / CashPoint / DOP, `cashier = opened_by = accepting actor`, `opening_expected = opening_counted = amount`, no open key or digest, and a unique immutable `opening_handover_id` (one receiving session per confirmed handover). |
| D4 | The receiver submits a **fresh, full** denomination map; its total must equal the handover exactly (`receiver_count_mismatch`, 422, zero side effects). A different composition is fine. A handover never creates an opening difference (T-022A: an opening difference can never own `posting_required`). |
| D5 | Confirmation creates exactly one `session_handover_out` (negative, source) and one `opening_handover_fund` (positive, receiving), both linked by `cash_movements.session_handover_id`, each unique per handover. No `CapitalMovement`. Aggregate cash effect 0. |
| D6 | `_close_snapshot` excludes BOTH opening kinds (`opening_capital_fund`, `opening_handover_fund`) from operating incoming cash. |
| D7 | `cash_custody_transfers` stays capital-only. History lives in `cash_session_handovers` (`pending` / `confirmed` / `cancelled`); at most one non-cancelled row per source session; confirmed is terminal; no UPDATE of history, no DELETE, no TRUNCATE. |
| D8 | Accept owns `accept_idempotency_key` / `accept_request_digest` / `accepted_by` / `accepted_at`. The receiver's count is stored ONCE: the receiving session's immutable `opening_denominations` (exposed as `receiver_denominations`). |
| D9 | The named receiver may **decline**: an immutable annotation (`declined_*`, `decline_reason`, own key), the row stays `pending`, never a fourth state. A declined row refuses `/accept` with `handover_declined` (409) and can never be confirmed; its only next step is redirect. Declining needs no permission (refusing custody is always safe). |
| D10 | **Redirect** has its own key/digest/actor/reason and atomically: cancels the old row, creates the replacement, fills exactly one immutable forward pointer. No bare cancel. |
| D11 | Direct -> direct: another named cashier of the SAME CashPoint (a new row; the same receiver is allowed after a decline). Pointer `redirected_to_session_handover_id`; the predecessor is derived from the unique forward pointers. |
| D12 | Direct -> capital: a NEW ordinary T-021 row (`kind = closing_capital`, same session, same frozen amount, named capital receiver, T-021 maker-checker). Pointer `redirected_to_capital_handover_id`. Exactly one pointer per cancelled row at COMMIT (deferred); none otherwise. |
| D13 | No way back from capital. T-021 capital rules apply from then on (named receiver, no recount, no cancel/reassign). Capital does not solve every no-show or dispute. |
| D14 | A persistent count dispute (source 30,000, receiver counts 29,500 and declines) is NOT an opening difference, a source rewrite, a cash/capital adjustment or a posting. After a decline only `cash.handovers.redirect` may redirect it; the source may stay `closing`. A receiver-discrepancy primitive is out of scope. |
| D15 | New sensitive permissions `cash.handovers.receive` (named receiver, together with `cash.sessions.open` for the CashPoint, validated at declaration and again at acceptance) and `cash.handovers.redirect` (supervision). Granted only to the tenant system role (migration pattern). No admin bypass. |
| D16 | The source cashier cannot receive their own handover (service + `receiver_not_maker` CHECK + insert trigger). The accepting actor must be the named receiver; the session is opened by them (self-opening intact). |
| D17 | Redirect authority: before a decline the maker OR a holder of `cash.handovers.redirect`; after a decline ONLY a holder of `cash.handovers.redirect`. A new direct receiver is validated against current membership, receive + open permission and maker separation; a capital receiver by the current T-021 rules. |
| D18 | A suspended / inactive CashPoint blocks the acceptance (it opens a NEW session): `cash_point_not_active`. Redirect is recovery and does not need an active CashPoint. |
| D19 | No user-wide single-session rule is invented. |
| D20 | `disable_user` (and the legacy v1 user update) is refused with `user_has_cash_responsibility` while the user owns an `open` / `closing` session, is the named receiver of a pending capital handover, or of a pending **non-declined** direct handover. A receiver who declined may be disabled. The T-019 field-custody guard is unchanged. Lock order: user row first, then the responsibility checks; commands that name a receiver take that user row `FOR SHARE`. |
| D21 | A source `pending_review` difference blocks neither the acceptance nor the receiving opening; it becomes resolvable once the source is `closed`. T-022A tables and taxonomy are untouched. |
| D22 | The `close` digest for `capital` / omitted destination is byte-for-byte the T-021/T-021H formula (no `destination` key); `next_session` adds `"destination": "next_session"`. No backfill. |
| D23 | Accept, decline and redirect each own a tenant-global partial UNIQUE key index and follow the T-021H pattern: pre-lock replay, locks, post-lock replay before state checks, savepoint claim, classification of the exact `diag.constraint_name`, re-read of the winner, anything else propagates. |
| D24 | Lock order: receiver user `FOR SHARE` -> cash box -> CashPoint `FOR UPDATE` -> source session `FOR UPDATE` -> handover `FOR UPDATE`. Accept: claim key -> source out movement -> handover confirmed -> source `closed` (before the new session, the active-slot index is not deferrable) -> receiving session -> receiving movement -> balance -> audit; commit-time deferred triggers validate the whole graph. |
| D25 | At COMMIT a `closing` source has exactly ONE live (pending) destination across `cash_custody_transfers` and `cash_session_handovers`. |

## Lifecycle

```
direct handover:  pending ──accept──▶ confirmed (terminal)      source: closing ─▶ closed, receiving session: open
                  pending (declined, immutable) ──redirect──▶ cancelled ─▶ pointer ─▶ direct row | capital row
                  pending ──redirect (maker or supervisor)──▶ cancelled ─▶ pointer ─▶ direct row | capital row
```

## API (v2)

| method | path | notes |
| --- | --- | --- |
| POST | `/api/v2/cash/sessions/{id}/close` | new optional `destination` (`capital` default, `next_session`). A direct close returns `session_handover` and `next_action = accept_session_handover`. `handover` keeps meaning the T-021 capital handover. |
| GET | `/api/v2/cash/session-handovers` | `state` (`pending` default, `confirmed`, `cancelled`), `branch_id`, `limit`, `before_id`. Participants, or holders of receive / redirect / read for the scope. |
| GET | `/api/v2/cash/session-handovers/{id}` | handover, `replacement`, `source_session`, `receiving_session`, `receiver_denominations`. |
| POST | `/api/v2/cash/session-handovers/{id}/accept` | `idempotency_key`, `denominations` (fresh full map). |
| POST | `/api/v2/cash/session-handovers/{id}/decline` | `idempotency_key`, `reason` (>= 10 chars). |
| POST | `/api/v2/cash/session-handovers/{id}/redirect` | `idempotency_key`, `destination` (`next_session` / `capital`), `receiver_user_id`, `reason` (>= 10 chars). |

`extra="forbid"` everywhere. `/api/v2/cash/handovers` stays capital-only; legacy v1 behaviour stays capital-only. Idempotency
keys and digests are never exposed. Stable errors: `session_handover_not_found` (404), `session_handover_not_pending`,
`handover_declined`, `user_has_cash_responsibility` (409), `receiver_count_mismatch`, `next_session_requires_cash`,
`invalid_handover_destination`, `handover_reason_required`, `invalid_receiver` (422), `redirect_not_authorized` (403), plus the
existing `not_named_receiver`, `maker_checker_violation`, `cash_point_not_active`, `idempotency_conflict`.

## Security events

Rows are the truth; events carry ids and structured facts only (never the free-text reasons):
`cash.session_handover.declared`, `.accepted`, `.declined`, `.redirected`. The receiving session also gets the normal
`cash.session.opened` with `opening_source = handover` and `session_handover_id`.

## Migration 0022

One migration, no historical rewrite, no synthetic handover, no backfill. It adds `cash_session_handovers`, `cash_sessions.
opening_handover_id`, the `handover` opening source, `cash_movements.session_handover_id` with its CHECKs and unique indexes,
the three key indexes, the permissions (system role only), replaces four 0020 trigger functions (`cash_sessions_insert_check`,
`cash_sessions_update_check`, `cash_session_consistency_check`, `cash_movements_insert_check`; the 0020 definitions are kept in
the migration for the downgrade) and installs the handover triggers, including the deferred consistency graph. The SQL lives
once in `app/modules/cash/handover_ddl.py`; the migration carries a verbatim copy and a test compares both.

**Downgrade is lossless only.** It takes `ACCESS EXCLUSIVE` locks on `cash_session_handovers`, `cash_sessions` and
`cash_movements`, and refuses with `cannot downgrade 0022: session handover history exists` if ANY of these exists: a
`cash_session_handovers` row in any state; a session with `opening_source = 'handover'` or an `opening_handover_id`; a
`session_handover_out` / `opening_handover_fund` movement or any `session_handover_id`; a `cash.session_handover.*`
SecurityEvent; a `cash.session.opened` event whose `opening_source` is `handover`. It never deletes, rewrites or synthesises
anything (no fake repair, no synthetic capital movement). With no T-023 history it removes only 0022 artifacts and restores
the exact 0021 schema; head returns to `0021`.

## Package boundary

T-023A does not change T-019 (field custody), T-020 (refunds, `credit_field_refund` stays negative, `reverses_id = NULL`),
T-021 (CashPoint active-slot model, capital closing flow, closed terminal), T-021H (idempotency hardening) or T-022A
(differences and resolutions, no accounting effect). Out of scope: other CashPoints / branches, cross-branch transit,
open-to-open transfer, partial transfer, receiver discrepancy accounting, T-022B, Web, Mobile.
