# T-021 — CashPoint-anchored cash core session lifecycle

Official identity: **T-021 CASHPOINT_SESSION_LIFECYCLE** (internal lineage **CASH-CORE-01**). It makes the CashPoint the
operational cash position and gives the cash session a real lifecycle: one active session per CashPoint, traced openings,
exact physical counts, differences as records, and a closing handover to capital with maker != receiver. Migration
`0020_cashpoint_session_lifecycle`, decisions D1–D12 of `CASH-CORE-DISCOVERY-RESULT.md`.

## Why T-021 and not "T-008"

The historical design document `T-008-CASH-RUNTIME-RECONCILIATION.md` (outside this repository) was never implemented:
the repository's T-008 is `docs/T-008-CREDIT-PAYMENT-RUNTIME.md` (credit payment runtime) and stays untouched. The cash
core is therefore a new package, T-021. The old document was read as prior design evidence only.

## Hybrid transition (adapt, not rebuild)

* **CashPoint** (T-003) is the operational position. It gains `origin` (`manual` | `legacy_box` | `legacy_session_split`)
  and `box_id` (the base CashPoint of a cash box). Several CashPoints per branch = several cashiers in parallel.
* **`cash_sessions`, `cash_movements`, `cash_custody_transfers` are adapted IN PLACE**: T-007..T-020 reference their ids
  (FKs and triggers), so they are never replaced. New columns: tenant / CashPoint / currency anchors, opening source and
  contract, close contract, denominations, idempotency anchors, handover provenance.
* **`cash_boxes`** stays as the legacy branch container. Every box gets exactly one base CashPoint **created with it by the
  database** (`trg_cash_boxes_base_cash_point`): the post-0005 "box without CashPoint" gap cannot reappear.
* The Credit Cash port (`app/modules/cash/port.py`) is **unchanged**: same signatures, same `box -> session` lock order.
  A `BEFORE INSERT` trigger gives each movement the tenant, CashPoint and currency of its session.

## Lifecycle

```
open ──(close, counted = 0)──────────────────────────────▶ closed   (terminal)
open ──(close, counted > 0)──▶ closing ──(handover accepted)──▶ closed
```

* **D1** — at most ONE `open|closing` session per CashPoint (partial UNIQUE `uq_cash_sessions_active_cash_point`).
  `closing` holds the slot (the cash is not in capital yet); `closed` never does, even with a difference pending review.
* `closed` is terminal: no reopening, no edit, no delete (trigger). `closing -> open` does not exist.
* Separate lifecycles: the **difference** is a record (`cash_session_differences`), the **handover** is the transfer row,
  **suspension** lives on the CashPoint. None of them is a session state.

| situation | next opening on the same CashPoint |
| --- | --- |
| `closed` + difference `pending_review` | allowed |
| `closing` + handover `pending` | refused (`cash_point_busy`) |
| CashPoint `suspended` / `inactive` | refused (`cash_point_not_active`) |

## Opening (D3, D4)

Sources implemented: **`zero`** and **`capital`**. Anonymous or untracked opening cash is impossible (service and DB).

* `zero`: expects, counts and declares nothing; no movement, no capital row.
* `capital`: exactly ONE `opening_capital_fund` movement (+fund) and ONE capital `to_cash` linked 1:1 to it, same
  transaction; refused if the capital reserve is short. The legacy dead `opening_fund` path is retired.
* A capital opening is counted by denomination; `sum(denominations) = opening_counted` (DB). A count that differs from
  the fund needs an observation and becomes an `opening` difference; the ledger is not adjusted.
* Self only: the actor opens their own session (no opening on behalf of another person). Idempotent (key + digest).

## Closing and handover (D2, D4, D5)

* The owner closes with an exact denomination count. `expected` = the session balance (the movements), frozen with the
  count and `difference = counted - expected`; a non-zero difference requires an `observation_note` (no causal
  `resolution_reason` yet) and creates a `closing` difference record. **No `closing_adjustment` is ever written** (a new
  one is refused by the database; historical ones stay untouched).
* `counted = 0` → `closed` directly (no handover, no zero-value movement or capital row).
* `counted > 0` → `closing` + ONE pending handover of the **counted** amount to capital, naming a receiver who must be an
  active user of the tenant, different from the cashier and holding `cash.handovers.accept` for the branch. The answer
  carries `next_action` (endpoint and legacy command) so closing never depends on a hidden step.
* Acceptance: the named receiver, authenticated, with `cash.handovers.accept` in scope, never the maker (also a DB
  backstop), no admin bypass. One transaction: one `closing_capital_transfer` (−counted), one capital `from_cash`
  (+counted, linked 1:1), handover `confirmed` (terminal), session `closed`. Replays return the same answer.
* After a short close the closed session's balance equals `−difference`: the shortage stays visible in the ledger until a
  later, explicit regularisation (review package).

## Suspension (D6)

Suspension blocks only NEW sessions (service + insert trigger). An already active session keeps operating: Credit port
movements, T-019 accepted renditions, T-020 branch refunds, close and handover to capital. No movement trigger looks at
the CashPoint status; `suspended` is not a hard freeze.

## Database enforcement

* `cash_sessions`: state/source/contract CHECKs, v2 close difference CHECK, insert trigger (born `open`, v2, owned,
  anchored to an active CashPoint of its box branch, traced opening), update trigger (allowed transitions, terminal
  `closed`, immutable identity/owner/opening, immutable close record while `closing`, exact denominations), delete guard,
  deferred consistency trigger (`balance = balance_base + Σ movements`, capital opening backed, differences recorded,
  `closing` has exactly one handover of the count).
* `cash_movements`: append-only (UPDATE/DELETE refused), inherits anchors, only `open` sessions (the closing transfer only
  in `closing`), retired kinds refused, one fund / one closing transfer per session (partial UNIQUEs).
* `cash_custody_transfers`: one closing handover per session, `pending -> confirmed` terminal, immutable parties and
  amount, maker != receiver, v2 accepted by the named receiver, confirmation backed by its movement and capital row.
* `cash_session_differences`: immutable (no review transition in T-021), figures consistent, unique per (session, phase).
* `capital_movements.cash_movement_id` UNIQUE (already in the baseline) = the 1:1 capital link.

## Permissions

`cash.sessions.read`, `cash.sessions.open`*, `cash.sessions.close`*, `cash.handovers.accept`*, `cash.differences.read`
(* sensitive). Migration 0020 grants them ONLY to the tenant system role ("Administrador de agencia", as 0005/0018/0019);
cashiers and receivers need explicit tenant assignments (branch or cash_point scope). No legacy role implies them.

## Legacy `/api/v1/cash` compatibility

* `setup`: the box comes with its base CashPoint; the answer includes `cash_point_id`.
* `open`: same legacy role gate (cashier) **plus** `cash.sessions.open`; self only; `cash_point_id` optional (default: the
  box's base CashPoint); `amount > 0` needs `capital=true` and the denomination count.
* `close`: same role gate **plus** `cash.sessions.close`; T-021 count/difference/handover; returns `next_action`.
* `confirm_closing_transfer`: same role gate **plus** the T-021 acceptance rules; idempotent.
* `resolve`: 409 `difference_review_not_available` (no reopening, no adjustment).
* `movement`: a `contribution` without `capital=true` is refused (no untracked injection).
* `workspace`/reports: states `open|closing`; idempotency keys and digests are never serialised; the finance report counts
  `closing` sessions by their physical count.

## Migration 0020

Order: preflight → CashPoint provenance → base CashPoint per box (adopt 0005 `CAJA-<box_id>`, create missing) → nullable
columns + differences table → D8 split → session/movement backfill → D7/D11 differences → D9/D11 handovers → state
normalisation → active-slot check → NOT NULL / FKs / CHECKs → indexes → functions/triggers → permissions.

* **D8/D12**: per box, the active legacy set (`open`, `closing_transfer_pending`, `closing_review` with counted > 0)
  ordered by `(opened_at, id)`; the first keeps the base CashPoint, each other gets `MIG-S<session_id>` (same tenant and
  branch, `origin = legacy_session_split`, **suspended**, reason "Posición generada por migración para sesión legacy
  concurrente"). Migration constructs, not verified physical positions; never activated automatically.
* **D9**: `closing_transfer_pending` → `closing`, its single pending transfer adopted as the handover (same row).
* **D7/D11**: `closing_review` → a `pending_review` difference from the stored expected (snapshot) / counted / difference;
  counted > 0 → `closing` + a migration handover of exactly the stored count (no movement or capital row until it is
  accepted); counted = 0 → `closed`.
* **D10**: legacy rows keep their evidence (`opening_contract`/`close_contract = legacy`); denominations are never
  synthesised; every NEW open/close is v2 and DB-checked. Ownerless legacy sessions get `opened_by` as owner (the legacy
  code already used that fallback). The legacy balance is frozen as `balance_base` (no recomputation).
* **Preflight** refuses atomically (nothing changes) on: unknown states, `opening_review`, `closing_transfer_pending`
  without exactly one pending transfer, orphan pending transfers, unknown transfer kind/state, several closing transfers
  per session, `closing_review` with a transfer, NULL/negative counts, inconsistent expected/counted/difference, negative
  amounts, transfer amount ≠ stored count, sessions without a valid owner, cross-tenant boxes, movements of another box,
  `CAJA-`/`MIG-S` code collisions, duplicated capital links; and, after the split, more than one active session per point.
* **Downgrade** refuses when T-021 history exists (v2 sessions, v2 differences, v2 or accepted handovers, T-021
  movements, administered MIG CashPoints); otherwise it restores the legacy states deterministically and removes only the
  migration constructs. Base CashPoints stay (the table existed at 0019).

## T-019 / T-020 compatibility

`deposit_field_rendition` and `withdraw_field_refund` keep their signatures and behaviour; no T-019/T-020 row is rewritten;
`credit_field_refund` stays negative with `reverses_id = NULL`; triggers 0018/0019 are unchanged. Their test fixtures now
build valid v2 sessions (`tests/cash_fixtures.py`) instead of inserting legacy-shaped rows.

## Out of scope (later packages)

Difference review/resolution and its accounting, opening from a handover, session-to-session or point-to-point handover,
cash left in a point for the next session, multi-currency runtime (DOP only), Web and Mobile changes, workspace
pagination/N+1, discrepancy/write-off/suspense, FE-UX-CASH-001 execution.

## Known limitations

* Existing cashiers/receivers need explicit permission assignments before using the adapted legacy commands (403 until
  then). Web must send `capital=true` and the denomination count to open with cash (explicit 422 until it does).
* The capital reserve check is not serialised against concurrent capital writers (pre-existing `capital_service`).
* `/cash/workspace` keeps its unpaginated, N+1 shape (documented debt; not touched).
* The legacy collector delivery flow (`CashDelivery`) remains as is; T-019 renditions are the modern path.
