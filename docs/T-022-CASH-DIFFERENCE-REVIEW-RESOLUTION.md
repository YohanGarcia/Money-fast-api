# T-022A — Cash difference review and resolution

Official identity: **T-022A CASH_DIFFERENCE_REVIEW_RESOLUTION** (parent package **T-022**, internal lineage **CASH-CORE-02**).
It lets an independent, authorised reviewer record the **decision** about a physical cash difference that T-021 registered,
without ever erasing or rewriting it. Migration `0021_cash_difference_resolution`. Backend only.

## Scope

* A review decision for each `cash_session_differences` row of a **closed** session: one immutable
  `cash_difference_resolutions` row and the difference status `pending_review -> resolved`.
* Reads: the difference list (new `phase` / `provenance` filters, the resolution embedded) and the difference detail.
* **No** accounting posting, **no** cash movement, **no** capital movement, **no** employee receivable, **no** liability or
  suspense. Those belong to T-022B and the accounting packages (see "T-022B boundary").
* Not in this package: inter-session handover, opening from handover, leave-cash-in-point, Web, Mobile, FX, T-023.

## Frozen decisions (D1–D7, approved)

| id | decision |
| --- | --- |
| D1 | `accepted_loss` / `accepted_surplus` are **review decisions** only: no `CashMovement`, no `CapitalMovement`. `resolved` means "review decision complete", **not** "accounting posted". They are valid only for `phase = closing`. An opening difference never carries a pending economic consequence. |
| D2 | Strict maker-checker. The resolver is none of `session.cashier_id`, `session.opened_by`, `session.closed_by`, `difference.detected_by`; holds the explicit `cash.differences.resolve` permission with a valid scope; no admin bypass. The handover receiver is **not** excluded. |
| D3 | No amount thresholds and no double approval (needs a future policy). |
| D4 | Lifecycle `pending_review -> resolved` only. `under_review` and `dismissed` stay reserved in the CHECK for compatibility but no transition or endpoint reaches them. |
| D5 | `employee_receivable`, `unidentified_cash_liability` and the real accounting posting of a loss/surplus are deferred (T-022B / accounting packages). Never simulated with a `CashMovement`, `CapitalMovement`, `Loan` or free-text note. |
| D6 | Opening/closing overlap: the **closing** difference is the only one that can own a future economic consequence. An opening difference is kept as independent historical evidence, can be reviewed and only resolves as `no_further_action` (reason required). No netting, no double posting, no overlap FK; the API derives `opening_difference` / `closing_difference` by `session_id`. No terminal resolution while `session.state <> 'closed'` (opening differences included). |
| D7 | `legacy_migration` differences use the closing taxonomy with the same maker-checker, a new mandatory reason, visible provenance, and no reconstruction of missing evidence. A legacy difference whose session is still `closing` (pending migration handover, T-021 D11) cannot resolve until the handover is accepted and the session is `closed`; a `counted = 0` legacy session 0020 left `closed` resolves at once. |

## Lifecycle

```
difference:  pending_review ──(resolve, session closed)──▶ resolved      (terminal)
session:     open ─▶ closing ─▶ closed                                    (T-021, unchanged, never reopened)
```

Resolving never reopens a session, never blocks or unblocks a CashPoint opening, and never suspends a CashPoint
(suspension stays an explicit administrative act with `cash.points.suspend`).

## Resolution taxonomy

| type | phase | difference | reason | reference | `accounting_disposition` |
| --- | --- | --- | --- | --- | --- |
| `no_further_action` | opening or closing | either sign | >= 10 chars | optional | `none` |
| `accepted_loss` | closing only | `< 0` (shortage) | >= 10 chars | required (>= 3) | `posting_required` |
| `accepted_surplus` | closing only | `> 0` (overage) | >= 10 chars | required (>= 3) | `posting_required` |

`accounting_disposition` (`none` | `posting_required`) is part of the **historical fact**: fixed at INSERT by the type and
**never changes**. T-022A does not represent whether a future posting is pending, posted, failed or reversed, and the API never
exposes `posted`, `failed`, `reversed` or `settled`. A session has at most **one** `posting_required` resolution (only its
closing difference can carry it).

## Physical and capital effect: none

A resolution creates no `CashMovement` and no `CapitalMovement`, rewrites no session, count, denomination or balance. The
closed session keeps its ledger residual (`-difference`) as visible evidence. Capital already reflects the **counted** cash
(the T-021 closing handover moves `counted`, not `expected`), so compensating capital would count the effect twice.

## API (`/api/v2/cash`, backend only)

| method | path | permission |
| --- | --- | --- |
| GET | `/differences?status=&branch_id=&phase=&provenance=&limit=&before_id=` | `cash.differences.read` (tenant, branch or cash_point scope) |
| GET | `/differences/{id}` | `cash.differences.read` or `cash.differences.resolve` on the CashPoint |
| POST | `/differences/{id}/resolve` | `cash.differences.resolve` on the CashPoint |

`POST` body (`extra` forbidden): `idempotency_key` (12–120), `resolution_type`, `reason`, `reference` (required for
`accepted_*`). Response: `difference`, `resolution` (immutable, without key/digest), `accounting_disposition`, `session`,
`opening_difference`, `closing_difference`, `next_action`, `replayed`. There is no `/review` endpoint.

| status | code | when |
| --- | --- | --- |
| 404 | `cash_difference_not_found` | unknown id or another tenant |
| 403 | `permission_denied` | no permission/scope |
| 403 | `maker_checker_violation` | resolver is the cashier, opener, closer or detector |
| 409 | `difference_not_pending` | already resolved (new key) |
| 409 | `session_not_closed` | session `open` or `closing` |
| 409 | `idempotency_conflict` | same key, different request or actor, or another difference |
| 422 | `resolution_type_not_applicable` | type vs phase vs sign mismatch |
| 422 | `resolution_reason_required` | reason shorter than 10 characters once trimmed |
| 422 | `resolution_reference_required` | `accepted_*` without a reference |

The list used to ignore `cash_point`-scoped grants (`_branch_ids`): a CashPoint-scoped reader saw nothing. It is replaced by
`_visible` / `_scoped` in `app/modules/cash/sessions.py` (also used by `list_handovers`), with regression tests. Behaviour for
tenant and branch scopes is unchanged.

## Idempotency

The key and a canonical digest (`operation`, `difference_id`, `resolution_type`, `reason`, `reference`) are stored on the
resolution; `UNIQUE (tenant_id, idempotency_key)`. Same key + same digest + same actor replays the original result
(`replayed = true`, no new row, no new SecurityEvent), also after the difference became terminal. Same key with another
request, actor or difference is `idempotency_conflict`. The replay is checked again after the locks, so two concurrent
identical requests leave one resolution.

## Audit

The resolution row is the authoritative truth (who, when, type, reason, reference). One `SecurityEvent`
`cash.difference.resolved` is written in the same transaction with ids only (`difference_id`, `resolution_id`, `session_id`,
`cash_point_id`, `branch_id`, `phase`, `provenance`, `resolution_type`, `accounting_disposition`); the free-text reason is
**not** copied into it. The legacy `CashAudit` is not used.

## Lock order

cash box (legacy container) -> difference (`FOR UPDATE`) -> insert resolution -> status update. The session is only read:
`closed` is terminal, so it needs no write lock and no new lock cycle appears.

## Database enforcement (migration 0021)

* `cash_session_differences`: `UNIQUE (id, phase)` (target of the composite FK). The 0020 blanket guard
  (`BEFORE UPDATE OR DELETE -> cash_history_guard`) becomes a DELETE-only guard plus `cash_session_differences_update_check`:
  every original column stays immutable, only `status pending_review -> resolved`, and only once its resolution exists.
* `cash_difference_resolutions` (new, **INSERT only**): `UPDATE` and `DELETE` refused (`cash_history_guard`), `TRUNCATE`
  refused (`BEFORE TRUNCATE` statement trigger, also when reached with `CASCADE` from the differences table).
* CHECKs: type and disposition domains; `disposition matches type`; `opening` only `no_further_action`; reason >= 10 and
  reference >= 3 for `accepted_*`. Composite FK `(difference_id, phase)` ties the resolution to the exact difference phase.
  `UNIQUE (difference_id)`, `UNIQUE (tenant_id, idempotency_key)` and the partial unique index
  `(session_id) WHERE accounting_disposition = 'posting_required'` (one economic consequence per session).
* Insert trigger: tenant/session/phase of the difference, difference `pending_review`, session `closed`, `accepted_loss`
  needs `difference < 0`, `accepted_surplus` needs `difference > 0`, resolver != cashier / opener / closer / detector.
* Deferred constraint triggers: `status = 'resolved'` holds **exactly** when the resolution exists (both land in one
  transaction).
* Service only: permission and scope, reason/reference text rules, error mapping.

## Permission

`cash.differences.resolve` (tenant scope kind, **sensitive**). The migration grants it **only** to the tenant system role
(`WHERE r.system_defined AND r.tenant_id IS NOT NULL`, as 0018/0019/0020); nobody else receives it automatically and holding
it never bypasses the maker-checker.

## Migration 0021 and its downgrade

Additive over 0020: it rewrites no 0020 row and no 0020 migration. Downgrade is **lossless only**: it takes
`ACCESS EXCLUSIVE` locks on both tables, then runs a preflight and **refuses before any DROP/ALTER** with
`cannot downgrade 0021: cash difference resolution history exists` when (1) `cash_difference_resolutions` has rows, (2) any
difference is not `pending_review`, or (3) a `cash.difference.resolved` SecurityEvent exists. It never converts
`resolved -> pending_review`, deletes a resolution, synthesises an observation or repairs anything. With no history it
removes the 0021 triggers/functions, the empty table and indexes, the unique constraint and the permission, and restores the
exact 0020 guard (`tests/test_t022a_cash_difference_resolution.py` compares the full trigger/function/constraint/index
snapshot against 0020).

## T-019 / T-020 / T-021 compatibility

Unchanged and not reinterpreted: field custody and renditions (T-019, `credit_field_rendition`), field refunds (T-020,
`credit_field_refund`, `reverses_id = NULL`), CashPoint as the operational anchor, one active session per CashPoint, `closed`
terminal, exact denomination evidence, no `closing_adjustment`, the capital handover and suspension semantics. The only
existing code touched is `sessions.py` (list scope helper, difference filters) and the catalogue; no earlier assertion
changed.

## T-022B boundary

T-022B (after the accounting packages T-011/T-012/T-013 and an approved labour/accounting policy) creates its **own**
immutable truth referencing `resolution_id` with appropriate uniqueness: employee receivable, unidentified-cash liability or
suspense, loss/surplus postings. It **never updates** a T-022A resolution; the current accounting state is derived from the
resolution's `accounting_disposition` plus the existence/state of the later primitive.

## Known limitations

* A legacy `closing` session whose migration handover is never accepted keeps its difference unresolved by design.
* No attachment storage exists: evidence is the `reason` and the `reference` text.
* No SLA/timeout, assignment or investigation state (no `under_review`): the queue is `status = pending_review`.
* The finance report is unchanged: loss/surplus decisions do not reach it until the accounting packages exist.
