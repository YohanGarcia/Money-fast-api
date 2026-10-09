"""Cash core session lifecycle (T-021): CashPoint-anchored sessions, ``open -> closing -> closed``.

* A CashPoint is the operational cash position; at most ONE session per CashPoint is ``open`` or ``closing`` (D1).
* Opening (D3): ``zero`` (nothing enters) or ``capital`` (one ``opening_capital_fund`` movement backed by one capital
  ``to_cash``, same transaction). Cash without a traced origin never enters. A non-zero opening is counted by
  denomination (D4); a counted amount that differs from the fund is recorded as an opening difference.
* Close (D2/D4): exact denomination count; ``expected`` is the session balance (the movements); a difference needs an
  observation and is recorded apart (never a balancing movement). ``counted = 0`` closes directly; otherwise the session
  goes ``closing`` with ONE pending handover of the counted cash to capital, to a named receiver.
* Handover acceptance (D5): the named receiver (v2), authenticated, holding ``cash.handovers.accept`` for the branch,
  never the maker. One ``closing_capital_transfer`` movement + one capital ``from_cash`` + confirmation + ``closed``,
  one transaction. Confirmed is terminal; a closed session never reopens.
* Suspension (D6): an inactive or suspended CashPoint refuses a NEW session only.

Lock order: cash box (legacy container) -> cash point (open/close) -> session -> handover. The Credit Cash port keeps
box -> session. Functions here never commit: the HTTP layer (or the legacy command) commits the unit of work.
"""

import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflict
from app.core.time import business_date, now_utc
from app.models.cash import (
    ACTIVE_SESSION_STATES,
    CashBox,
    CashConfig,
    CashCustodyTransfer,
    CashMovement,
    CashSession,
    CashSessionDifference,
)
from app.modules.cash.ddl import DENOMINATIONS
from app.modules.cash.errors import (
    CashNotEnabled,
    CashPointBusy,
    CashPointNotActive,
    CashPointNotFound,
    CashSessionNotFound,
    HandoverNotFound,
    HandoverNotPending,
    InsufficientCapital,
    InvalidCount,
    InvalidOpening,
    InvalidReceiver,
    MakerCannotAccept,
    NotNamedReceiver,
    NotSessionOwner,
    ObservationRequired,
    ReceiverRequired,
    SessionNotOpen,
)
from app.modules.credit.rules import canonical_json
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, build_principal, require
from app.modules.identity.errors import PermissionDenied, TenantMismatch
from app.modules.identity.models import UserAccount
from app.modules.organization.models import CashPoint
from app.modules.organization.service import ensure_cash_point_usable
from app.services import capital_service

READ, OPEN, CLOSE = "cash.sessions.read", "cash.sessions.open", "cash.sessions.close"
ACCEPT, DIFF_READ = "cash.handovers.accept", "cash.differences.read"
CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("9999999999.99")
OPENING_FUND_KIND = "opening_capital_fund"
CLOSING_TRANSFER_KIND = "closing_capital_transfer"


# --- helpers --------------------------------------------------------------------------------------------
def _digest(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _amt(value: Decimal | None) -> str | None:
    return None if value is None else format(Decimal(value).quantize(CENT), "f")


def _money(raw, *, allow_zero: bool) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise InvalidOpening() from None
    if value < 0 or (value == 0 and not allow_zero) or value != value.quantize(CENT) or value > MAX_AMOUNT:
        raise InvalidOpening()
    return value


def count_denominations(denominations: dict | None) -> tuple[dict[str, int], Decimal]:
    """Validate a denomination count and return (canonical count, total). Zero quantities are dropped."""
    if denominations is None:
        return {}, Decimal(0)
    if not isinstance(denominations, dict):
        raise InvalidCount()
    canonical: dict[str, int] = {}
    total = Decimal(0)
    for key, qty in denominations.items():
        if key not in DENOMINATIONS or isinstance(qty, bool) or not isinstance(qty, int) or not 0 <= qty <= 1_000_000:
            raise InvalidCount()
        if qty:
            canonical[key] = qty
            total += Decimal(key) * qty
    if total > MAX_AMOUNT:
        raise InvalidCount()
    return canonical, total


def _gate(actor: Principal) -> int:
    if actor.tenant_id is None:
        raise TenantMismatch()
    return actor.tenant_id


def _cash_point(db: Session, tenant_id: int, cash_point_id: int, *, lock: bool = False) -> CashPoint:
    stmt = select(CashPoint).where(CashPoint.id == cash_point_id, CashPoint.tenant_id == tenant_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    cp = db.scalar(stmt)
    if cp is None:
        raise CashPointNotFound()
    return cp


def _box(db: Session, tenant_id: int, branch_id: int) -> CashBox:
    """The legacy cash box of the branch, locked FIRST (the order every cash writer follows)."""
    if db.get(CashConfig, tenant_id) is None:
        raise CashNotEnabled()
    box = db.scalar(
        select(CashBox).where(CashBox.company_id == tenant_id, CashBox.branch_id == branch_id).with_for_update()
    )
    if box is None:
        raise CashNotEnabled()
    return box


def _session(db: Session, tenant_id: int, session_id: int, *, lock: bool = False) -> CashSession:
    stmt = select(CashSession).where(CashSession.id == session_id, CashSession.tenant_id == tenant_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    s = db.scalar(stmt)
    if s is None:
        raise CashSessionNotFound()
    return s


def _scope(cp: CashPoint) -> dict:
    return {"tenant_id": cp.tenant_id, "branch_id": cp.branch_id, "cash_point_id": cp.id}


def _audit(db: Session, event: str, actor: Principal, client_ip: str | None, subject_id: int | None, **details) -> None:
    record_event(
        db,
        event,
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        subject_id=subject_id,
        client_ip=client_ip,
        details=details,
    )


def _handover(db: Session, session_id: int) -> CashCustodyTransfer | None:
    return db.scalar(
        select(CashCustodyTransfer).where(
            CashCustodyTransfer.session_id == session_id, CashCustodyTransfer.kind == "closing_capital"
        )
    )


def handover_out(h: CashCustodyTransfer) -> dict:
    return {
        "id": h.id,
        "session_id": h.session_id,
        "state": h.state,
        "provenance": h.provenance,
        "amount": _amt(h.amount),
        "currency_code": h.currency_code,
        "from_user_id": h.from_user_id,
        "to_user_id": h.to_user_id,
        "accepted_by": h.accepted_by,
        "accepted_at": h.accepted_at,
        "cash_movement_id": h.cash_movement_id,
        "capital_movement_id": h.capital_movement_id,
    }


def difference_out(d: CashSessionDifference) -> dict:
    return {
        "id": d.id,
        "session_id": d.session_id,
        "cash_point_id": d.cash_point_id,
        "phase": d.phase,
        "status": d.status,
        "provenance": d.provenance,
        "currency_code": d.currency_code,
        "expected": _amt(d.expected),
        "counted": _amt(d.counted),
        "difference": _amt(d.difference),
        "observation_note": d.observation_note,
        "detected_by": d.detected_by,
        "detected_at": d.detected_at,
    }


def session_out(db: Session, s: CashSession, *, replayed: bool | None = None) -> dict:
    """Ids, amounts, states, timestamps. No idempotency keys or digests. ``next_action`` says what completes a close."""
    h = _handover(db, s.id)
    differences = db.scalars(
        select(CashSessionDifference).where(CashSessionDifference.session_id == s.id).order_by(CashSessionDifference.id)
    ).all()
    next_action = None
    if s.state == "closing" and h is not None and h.state == "pending":
        next_action = {
            "action": "accept_closing_handover",
            "handover_id": h.id,
            "receiver_user_id": h.to_user_id,
            "endpoint": f"/api/v2/cash/handovers/{h.id}/accept",
            "legacy_command": "confirm_closing_transfer",
        }
    out = {
        "id": s.id,
        "tenant_id": s.tenant_id,
        "cash_point_id": s.cash_point_id,
        "box_id": s.box_id,
        "state": s.state,
        "currency_code": s.currency_code,
        "cashier_user_id": s.cashier_id,
        "opened_by": s.opened_by,
        "opened_at": s.opened_at,
        "business_date": s.business_date,
        "opening_source": s.opening_source,
        "opening_contract": s.opening_contract,
        "opening_expected": _amt(s.opening_expected),
        "opening_counted": _amt(s.opening_counted),
        "opening_denominations": s.opening_denominations,
        "balance": _amt(s.balance),
        "close_contract": s.close_contract,
        "closing_expected": _amt(s.closing_expected),
        "counted": _amt(s.counted),
        "difference": _amt(s.difference),
        "closed_by": s.closed_by,
        "closed_at": s.closed_at,
        "handover": handover_out(h) if h is not None else None,
        "differences": [difference_out(d) for d in differences],
        "next_action": next_action,
    }
    if replayed is not None:
        out["replayed"] = replayed
    return out


def _record_difference(
    db: Session,
    s: CashSession,
    *,
    phase: str,
    expected: Decimal,
    counted: Decimal,
    note: str,
    actor_id: int,
    at: datetime,
) -> CashSessionDifference:
    row = CashSessionDifference(
        tenant_id=s.tenant_id,
        session_id=s.id,
        cash_point_id=s.cash_point_id,
        phase=phase,
        currency_code=s.currency_code,
        expected=expected,
        counted=counted,
        difference=counted - expected,
        observation_note=note,
        status="pending_review",
        provenance="v2",
        detected_by=actor_id,
        detected_at=at,
    )
    db.add(row)
    db.flush()
    return row


def _note(raw: str | None) -> str:
    return (raw or "").strip()


# --- open ------------------------------------------------------------------------------------------------
def open_session(
    db: Session,
    actor: Principal,
    *,
    cash_point_id: int,
    source: str,
    amount,
    denominations: dict | None,
    observation_note: str | None,
    idempotency_key: str,
    client_ip: str | None = None,
) -> dict:
    """Open the actor's OWN session on a CashPoint (never on behalf of someone else)."""
    tenant_id = _gate(actor)
    note = _note(observation_note)
    counted_map, counted = count_denominations(denominations)
    fund = _money(amount, allow_zero=True)
    digest = _digest(
        {
            "operation": "open_cash_session",
            "cash_point_id": cash_point_id,
            "source": source,
            "amount": format(fund, "f"),
            "denominations": counted_map,
            "observation_note": note,
        }
    )
    prior = db.scalar(
        select(CashSession).where(
            CashSession.tenant_id == tenant_id, CashSession.open_idempotency_key == idempotency_key
        )
    )
    if prior is not None:
        if prior.open_request_digest == digest and prior.cashier_id == actor.user_id:
            return session_out(db, prior, replayed=True)
        raise IdempotencyConflict()
    cp = _cash_point(db, tenant_id, cash_point_id)
    require(actor, OPEN, **_scope(cp))
    if source == "zero":
        if fund != 0 or counted != 0:
            raise InvalidOpening("Una apertura en cero no recibe ni cuenta efectivo.")
    elif source == "capital":
        if fund <= 0 or not counted_map:
            raise InvalidOpening(
                "Una apertura desde capital necesita un fondo positivo y su conteo por denominaciones."
            )
        if counted != fund and len(note) < 3:
            raise ObservationRequired()
    else:
        raise InvalidOpening("El origen de la apertura es cero o capital: no se admite efectivo sin origen.")
    # locks: box -> cash point (the active-slot check serialises here; the partial UNIQUE index is the backstop)
    box = _box(db, tenant_id, cp.branch_id)
    cp = _cash_point(db, tenant_id, cash_point_id, lock=True)
    if cp.status != "active":
        raise CashPointNotActive()  # D6: suspended (or inactive) blocks a NEW session only
    ensure_cash_point_usable(db, tenant_id, cp.id, "DOP")  # T-003 gate: tenant and branch active, currency admitted
    if db.scalar(
        select(CashSession.id).where(CashSession.cash_point_id == cp.id, CashSession.state.in_(ACTIVE_SESSION_STATES))
    ):
        raise CashPointBusy()
    if source == "capital" and capital_service.balance(db, tenant_id) < fund:
        raise InsufficientCapital()
    at = now_utc()
    s = CashSession(
        box_id=box.id,
        tenant_id=tenant_id,
        cash_point_id=cp.id,
        currency_code="DOP",
        business_date=business_date(at),
        state="open",
        opening_source=source,
        opening_contract="v2",
        opening_expected=fund,
        opening_counted=counted,
        opening_denominations=counted_map,
        balance=Decimal(0),
        balance_base=Decimal(0),
        notes=note,
        opened_by=actor.user_id,
        cashier_id=actor.user_id,
        opened_at=at,
        open_idempotency_key=idempotency_key,
        open_request_digest=digest,
    )
    db.add(s)
    db.flush()
    if source == "capital":
        movement = CashMovement(
            box_id=box.id,
            session_id=s.id,
            kind=OPENING_FUND_KIND,
            amount=fund,
            actor_id=actor.user_id,
            notes="Fondo de apertura desde capital",
            reference=f"APE-{s.id}",
        )
        db.add(movement)
        db.flush()
        capital_service.record(
            db,
            tenant_id,
            actor.user_id,
            "to_cash",
            fund,
            f"Fondo de apertura jornada {s.id}",
            cash_movement_id=movement.id,
        )
        s.balance = s.balance + fund
        s.version += 1
    if counted != fund:
        _record_difference(
            db, s, phase="opening", expected=fund, counted=counted, note=note, actor_id=actor.user_id, at=at
        )
    db.flush()
    _audit(
        db,
        "cash.session.opened",
        actor,
        client_ip,
        actor.user_id,
        session_id=s.id,
        cash_point_id=cp.id,
        branch_id=cp.branch_id,
        opening_source=source,
        opening_expected=_amt(fund),
        opening_counted=_amt(counted),
    )
    if counted != fund:
        _audit(
            db,
            "cash.difference.detected",
            actor,
            client_ip,
            actor.user_id,
            session_id=s.id,
            phase="opening",
            expected=_amt(fund),
            counted=_amt(counted),
        )
    return session_out(db, s, replayed=False)


# --- close -----------------------------------------------------------------------------------------------
def _close_snapshot(db: Session, s: CashSession, expected: Decimal, counted: Decimal, difference: Decimal) -> dict:
    """Frozen at close (legacy report keys kept): opening + incoming - outgoing = expected. The opening is the cash the
    session started with (v2: its capital fund; legacy: its frozen base), never a balancing figure."""
    rows = db.execute(
        select(CashMovement.id, CashMovement.kind, CashMovement.amount).where(CashMovement.session_id == s.id)
    ).all()
    flows = [r for r in rows if r.kind != OPENING_FUND_KIND]
    incoming = sum((r.amount for r in flows if r.amount > 0), Decimal(0))
    outgoing = -sum((r.amount for r in flows if r.amount < 0), Decimal(0))
    return {
        "opening": _amt(expected - incoming + outgoing),
        "incoming": _amt(incoming),
        "outgoing": _amt(outgoing),
        "expected": _amt(expected),
        "counted": _amt(counted),
        "difference": _amt(difference),
        "movement_ids": [r.id for r in flows],
    }


def _valid_receiver(db: Session, tenant_id: int, cp: CashPoint, receiver_user_id: int, maker_id: int) -> None:
    user = db.get(UserAccount, receiver_user_id)
    if user is None or user.company_id != tenant_id or not user.is_active or user.id == maker_id:
        raise InvalidReceiver()
    if not build_principal(db, user, 0).allows(ACCEPT, **_scope(cp)):
        raise InvalidReceiver()


def close_session(
    db: Session,
    actor: Principal,
    session_id: int,
    *,
    denominations: dict | None,
    observation_note: str | None,
    receiver_user_id: int | None,
    idempotency_key: str,
    client_ip: str | None = None,
    snapshot_extra: dict | None = None,
) -> dict:
    """Operational close by the session owner. The difference (if any) is a record; it never blocks the next session."""
    tenant_id = _gate(actor)
    note = _note(observation_note)
    if not denominations:
        raise InvalidCount("El cierre exige el conteo fisico por denominaciones.")
    counted_map, counted = count_denominations(denominations)
    digest = _digest(
        {
            "operation": "close_cash_session",
            "session_id": session_id,
            "denominations": counted_map,
            "observation_note": note,
            "receiver_user_id": receiver_user_id if counted > 0 else None,
        }
    )
    s = _session(db, tenant_id, session_id)
    if s.close_idempotency_key == idempotency_key:
        if s.close_request_digest == digest:
            return session_out(db, s, replayed=True)
        raise IdempotencyConflict()
    if db.scalar(
        select(CashSession.id).where(
            CashSession.tenant_id == tenant_id, CashSession.close_idempotency_key == idempotency_key
        )
    ):
        raise IdempotencyConflict()
    cp = _cash_point(db, tenant_id, s.cash_point_id)
    require(actor, CLOSE, **_scope(cp))
    if s.cashier_id != actor.user_id:
        raise NotSessionOwner()
    # locks: box -> cash point -> session
    _box(db, tenant_id, cp.branch_id)
    cp = _cash_point(db, tenant_id, s.cash_point_id, lock=True)
    s = _session(db, tenant_id, session_id, lock=True)
    if s.state != "open":
        raise SessionNotOpen()
    expected = Decimal(s.balance)
    difference = counted - expected
    if difference != 0 and len(note) < 3:
        raise ObservationRequired()
    if counted > 0:
        if receiver_user_id is None:
            raise ReceiverRequired()
        _valid_receiver(db, tenant_id, cp, receiver_user_id, s.cashier_id)
    at = now_utc()
    s.counted, s.closing_expected, s.difference = counted, expected, difference
    s.denominations = counted_map
    s.close_contract = "v2"
    s.closed_by, s.closed_at = actor.user_id, at
    s.close_idempotency_key, s.close_request_digest = idempotency_key, digest
    s.snapshot = _close_snapshot(db, s, expected, counted, difference) | (snapshot_extra or {})
    if note:
        s.notes = note
    s.state = "closing" if counted > 0 else "closed"
    s.version += 1
    db.flush()
    if difference != 0:
        _record_difference(
            db, s, phase="closing", expected=expected, counted=counted, note=note, actor_id=actor.user_id, at=at
        )
    handover = None
    if counted > 0:
        handover = CashCustodyTransfer(
            company_id=tenant_id,
            box_id=s.box_id,
            session_id=s.id,
            kind="closing_capital",
            from_user_id=s.cashier_id,
            to_user_id=receiver_user_id,
            amount=counted,
            currency_code="DOP",
            state="pending",
            provenance="v2",
            notes=note,
        )
        db.add(handover)
        db.flush()
    _audit(
        db,
        "cash.session.closing_counted" if counted > 0 else "cash.session.closed",
        actor,
        client_ip,
        s.cashier_id,
        session_id=s.id,
        cash_point_id=cp.id,
        branch_id=cp.branch_id,
        expected=_amt(expected),
        counted=_amt(counted),
        difference=_amt(difference),
        handover_id=handover.id if handover is not None else None,
    )
    if difference != 0:
        _audit(
            db,
            "cash.difference.detected",
            actor,
            client_ip,
            s.cashier_id,
            session_id=s.id,
            phase="closing",
            expected=_amt(expected),
            counted=_amt(counted),
        )
    return session_out(db, s, replayed=False)


# --- closing handover acceptance -------------------------------------------------------------------------
def accept_handover(
    db: Session,
    actor: Principal,
    handover_id: int,
    *,
    idempotency_key: str,
    acceptance_id: str | None = None,
    notes: str | None = None,
    client_ip: str | None = None,
) -> dict:
    """The receiver confirms the physical cash: one transfer out of the session + one capital entry, then ``closed``."""
    tenant_id = _gate(actor)
    digest = _digest({"operation": "accept_closing_handover", "handover_id": handover_id})
    h = db.scalar(
        select(CashCustodyTransfer).where(
            CashCustodyTransfer.id == handover_id,
            CashCustodyTransfer.company_id == tenant_id,
            CashCustodyTransfer.kind == "closing_capital",
        )
    )
    if h is None:
        raise HandoverNotFound()
    if h.accept_idempotency_key == idempotency_key:
        if h.accept_request_digest == digest and h.accepted_by == actor.user_id:
            return session_out(db, _session(db, tenant_id, h.session_id), replayed=True)
        raise IdempotencyConflict()
    if db.scalar(
        select(CashCustodyTransfer.id).where(
            CashCustodyTransfer.company_id == tenant_id, CashCustodyTransfer.accept_idempotency_key == idempotency_key
        )
    ):
        raise IdempotencyConflict()
    s = _session(db, tenant_id, h.session_id)
    cp = _cash_point(db, tenant_id, s.cash_point_id)
    require(actor, ACCEPT, **_scope(cp))
    # locks: box -> session -> handover
    _box(db, tenant_id, cp.branch_id)
    s = _session(db, tenant_id, h.session_id, lock=True)
    h = db.scalar(
        select(CashCustodyTransfer)
        .where(CashCustodyTransfer.id == handover_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if h.state != "pending" or s.state != "closing":
        raise HandoverNotPending()
    if actor.user_id == h.from_user_id:
        raise MakerCannotAccept()
    if h.provenance == "v2" and actor.user_id != h.to_user_id:
        raise NotNamedReceiver()
    at = now_utc()
    if h.amount > 0:
        movement = CashMovement(
            box_id=s.box_id,
            session_id=s.id,
            kind=CLOSING_TRANSFER_KIND,
            amount=-h.amount,
            actor_id=actor.user_id,
            notes="Entrega de cierre a capital",
            reference=f"CUST-{h.id}",
            custody_transfer_id=h.id,
        )
        db.add(movement)
        db.flush()
        capital = capital_service.record(
            db,
            tenant_id,
            actor.user_id,
            "from_cash",
            h.amount,
            f"Entrega de cierre jornada {s.id}",
            cash_movement_id=movement.id,
        )
        s.balance = s.balance - h.amount
        h.cash_movement_id, h.capital_movement_id = movement.id, capital.id
    h.state = "confirmed"
    h.accepted_by, h.accepted_at = actor.user_id, at
    h.acceptance_id = acceptance_id or f"T021-{tenant_id}-{idempotency_key}"[:80]
    h.acceptance_method = "authenticated_confirmation"
    h.accept_idempotency_key, h.accept_request_digest = idempotency_key, digest
    if notes:
        h.notes = notes
    h.version += 1
    db.flush()
    s.state = "closed"
    s.version += 1
    db.flush()
    _audit(
        db,
        "cash.handover.accepted",
        actor,
        client_ip,
        h.from_user_id,
        handover_id=h.id,
        session_id=s.id,
        cash_point_id=cp.id,
        branch_id=cp.branch_id,
        amount=_amt(h.amount),
        provenance=h.provenance,
        cash_movement_id=h.cash_movement_id,
        capital_movement_id=h.capital_movement_id,
    )
    return session_out(db, s, replayed=False)


# --- reads (pure) ----------------------------------------------------------------------------------------
def _can_read_session(actor: Principal, cp: CashPoint, s: CashSession) -> bool:
    return s.cashier_id == actor.user_id or actor.allows(READ, **_scope(cp))


def get_session(db: Session, actor: Principal, session_id: int) -> dict:
    tenant_id = _gate(actor)
    s = _session(db, tenant_id, session_id)
    cp = _cash_point(db, tenant_id, s.cash_point_id)
    if not _can_read_session(actor, cp, s):
        raise PermissionDenied()
    return session_out(db, s)


def current_session(db: Session, actor: Principal, cash_point_id: int) -> dict | None:
    tenant_id = _gate(actor)
    cp = _cash_point(db, tenant_id, cash_point_id)
    s = db.scalar(
        select(CashSession).where(CashSession.cash_point_id == cp.id, CashSession.state.in_(ACTIVE_SESSION_STATES))
    )
    if s is None:
        if not (actor.allows(READ, **_scope(cp)) or actor.allows(OPEN, **_scope(cp))):
            raise PermissionDenied()
        return None
    if not _can_read_session(actor, cp, s):
        raise PermissionDenied()
    return session_out(db, s)


def _branch_ids(db: Session, actor: Principal, permission: str, branch_id: int | None) -> list[int] | None:
    """None = every branch (tenant scope); otherwise the branches the actor may see for ``permission``."""
    if branch_id is not None:
        if not actor.allows(permission, tenant_id=actor.tenant_id, branch_id=branch_id):
            raise PermissionDenied()
        return [branch_id]
    if actor.holds_at_tenant_scope(permission):
        return None
    ids = sorted({g.branch_id for g in actor.grants if g.permission == permission and g.scope_kind == "branch"})
    if not ids:
        raise PermissionDenied()
    return ids


def list_handovers(
    db: Session, actor: Principal, *, state: str, branch_id: int | None, limit: int, before_id: int | None
) -> dict:
    tenant_id = _gate(actor)
    branches = _branch_ids(db, actor, ACCEPT, branch_id)
    stmt = (
        select(CashCustodyTransfer)
        .join(CashSession, CashSession.id == CashCustodyTransfer.session_id)
        .join(CashPoint, CashPoint.id == CashSession.cash_point_id)
        .where(
            CashCustodyTransfer.company_id == tenant_id,
            CashCustodyTransfer.kind == "closing_capital",
            CashCustodyTransfer.state == state,
        )
        .order_by(CashCustodyTransfer.id.desc())
        .limit(limit)
    )
    if branches is not None:
        stmt = stmt.where(CashPoint.branch_id.in_(branches))
    if before_id is not None:
        stmt = stmt.where(CashCustodyTransfer.id < before_id)
    items = [handover_out(h) for h in db.scalars(stmt).all()]
    return {"items": items, "next_before_id": items[-1]["id"] if len(items) == limit else None}


def list_differences(
    db: Session, actor: Principal, *, status: str, branch_id: int | None, limit: int, before_id: int | None
) -> dict:
    tenant_id = _gate(actor)
    branches = _branch_ids(db, actor, DIFF_READ, branch_id)
    stmt = (
        select(CashSessionDifference)
        .join(CashPoint, CashPoint.id == CashSessionDifference.cash_point_id)
        .where(CashSessionDifference.tenant_id == tenant_id, CashSessionDifference.status == status)
        .order_by(CashSessionDifference.id.desc())
        .limit(limit)
    )
    if branches is not None:
        stmt = stmt.where(CashPoint.branch_id.in_(branches))
    if before_id is not None:
        stmt = stmt.where(CashSessionDifference.id < before_id)
    items = [difference_out(d) for d in db.scalars(stmt).all()]
    return {"items": items, "next_before_id": items[-1]["id"] if len(items) == limit else None}


def base_cash_point_id(db: Session, box_id: int) -> int | None:
    """The CashPoint created with a cash box (the legacy command's default position)."""
    return db.scalar(select(CashPoint.id).where(CashPoint.box_id == box_id))
