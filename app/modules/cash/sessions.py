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

Idempotency (T-021H): the open / close / accept keys are TENANT-global but the locks are per branch. Each command
(1) replays before the locks, (2) replays AGAIN right after them, before any state check, so an identical concurrent
retry gets the original answer instead of busy / not-open / not-pending, and (3) claims its key inside a savepoint as
its first write, so a collision with another branch's key is classified (only by the named unique index) as
``IdempotencyConflict`` while the outer transaction and everything else stay intact.

Direct handover (T-023A): ``close`` with ``destination = next_session`` leaves the counted cash in ONE pending
``cash_session_handovers`` row for a named cashier of the SAME CashPoint (``app.modules.cash.session_handovers`` owns
accept / decline / redirect). The capital close keeps its historical digest byte for byte (no ``destination`` key).
"""

import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflict
from app.core.time import business_date, now_utc
from app.models.cash import (
    ACTIVE_SESSION_STATES,
    CashBox,
    CashConfig,
    CashCustodyTransfer,
    CashDelivery,
    CashDifferenceResolution,
    CashMovement,
    CashSession,
    CashSessionDifference,
    CashSessionHandover,
)
from app.modules.cash.ddl import DENOMINATIONS
from app.modules.cash.errors import (
    CashNotEnabled,
    CashPointBusy,
    CashPointNotActive,
    CashPointNotFound,
    CashSessionNotFound,
    PendingSurplusReview,
    InvalidCount,
    InvalidHandoverDestination,
    InvalidOpening,
    InvalidReceiver,
    NextSessionRequiresCash,
    NotSessionOwner,
    ObservationRequired,
    ReceiverRequired,
    SessionNotOpen,
)
from app.modules.credit.rules import canonical_json
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, build_principal, require
from app.modules.identity.catalog import CATALOG_CODES
from app.modules.identity.errors import PermissionDenied, TenantMismatch
from app.modules.identity.models import UserAccount
from app.modules.organization.models import CashPoint
from app.modules.organization.service import ensure_cash_point_usable

READ, OPEN, CLOSE = "cash.sessions.read", "cash.sessions.open", "cash.sessions.close"
ACCEPT, DIFF_READ = "cash.handovers.accept", "cash.differences.read"
RECEIVE, REDIRECT = "cash.handovers.receive", "cash.handovers.redirect"  # T-023A (direct handover)
CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("9999999999.99")
OPENING_FUND_KIND = "opening_capital_fund"
# T-021H: the tenant-global idempotency anchors (their PostgreSQL names are the only collisions that are classified)
OPEN_KEY_CONSTRAINT = "uq_cash_sessions_open_key"
CLOSE_KEY_CONSTRAINT = "uq_cash_sessions_close_key"
ACCEPT_KEY_CONSTRAINT = "uq_cash_custody_transfers_accept_key"
CLOSING_TRANSFER_KIND = "closing_capital_transfer"
# T-023A: opening cash of a handover-opened session, and the source's matching exit (exactly one of each per handover)
OPENING_HANDOVER_FUND_KIND = "opening_handover_fund"
HANDOVER_OUT_KIND = "session_handover_out"
# FM-SIMPLE-CASH-010: the declared initial amount of a simple session (never backed by a capital movement)
INITIAL_FUND_KIND = "opening_initial_fund"
SIMPLE_CLOSE_NO_NOTE = "Diferencia de cierre registrada sin observacion de la cajera."
OPENING_KINDS = (OPENING_FUND_KIND, OPENING_HANDOVER_FUND_KIND, INITIAL_FUND_KIND)  # opening cash, never operating incoming cash
DESTINATIONS = ("next_session",)


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


def _lock_user_share(db: Session, user_id: int) -> None:
    """T-019 pattern: the user row FOR SHARE, so ``disable_user`` (FOR UPDATE) serialises with whoever relies on it."""
    db.execute(select(UserAccount.id).where(UserAccount.id == user_id).with_for_update(read=True))


def _live_session_handover(db: Session, session_id: int) -> CashSessionHandover | None:
    """The non-cancelled direct handover of a source session (at most one exists)."""
    return db.scalar(
        select(CashSessionHandover).where(
            CashSessionHandover.source_session_id == session_id, CashSessionHandover.state != "cancelled"
        )
    )


def session_handover_out(h: CashSessionHandover) -> dict:
    """The direct handover as business history. No idempotency keys or digests."""
    return {
        "id": h.id,
        "source_session_id": h.source_session_id,
        "cash_point_id": h.cash_point_id,
        "state": h.state,
        "amount": _amt(h.amount),
        "currency_code": h.currency_code,
        "from_user_id": h.from_user_id,
        "to_user_id": h.to_user_id,
        "created_at": h.created_at,
        "declined": h.declined_at is not None,
        "declined_by": h.declined_by,
        "declined_at": h.declined_at,
        "decline_reason": h.decline_reason,
        "accepted_by": h.accepted_by,
        "accepted_at": h.accepted_at,
        "redirected_by": h.redirected_by,
        "redirected_at": h.redirected_at,
        "redirect_reason": h.redirect_reason,
        "redirected_to_session_handover_id": h.redirected_to_session_handover_id,
        "redirected_to_capital_handover_id": h.redirected_to_capital_handover_id,
    }


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


def resolution_out(r: CashDifferenceResolution) -> dict:
    """The immutable decision. No idempotency key or digest."""
    return {
        "id": r.id,
        "difference_id": r.difference_id,
        "session_id": r.session_id,
        "phase": r.phase,
        "resolution_type": r.resolution_type,
        "accounting_disposition": r.accounting_disposition,
        "reason": r.reason,
        "reference": r.reference,
        "resolved_by": r.resolved_by,
        "resolved_at": r.resolved_at,
    }


def session_out(db: Session, s: CashSession, *, replayed: bool | None = None) -> dict:
    """Ids, amounts, states, timestamps. No idempotency keys or digests. ``next_action`` says what completes a close."""
    h = _handover(db, s.id)
    differences = db.scalars(
        select(CashSessionDifference).where(CashSessionDifference.session_id == s.id).order_by(CashSessionDifference.id)
    ).all()
    dh = None if s.state == "open" else _live_session_handover(db, s.id)  # only a closing / closed source can have one
    next_action = None
    # (a pending handover TO CAPITAL can only be historical now: nothing completes it, so no next action is offered)
    if s.state == "closing" and dh is not None and dh.state == "pending":
        declined = dh.declined_at is not None
        next_action = {
            "action": "redirect_session_handover" if declined else "accept_session_handover",
            "session_handover_id": dh.id,
            "receiver_user_id": dh.to_user_id,
            "endpoint": f"/api/v2/cash/session-handovers/{dh.id}/" + ("redirect" if declined else "accept"),
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
        "opening_handover_id": s.opening_handover_id,
        "balance": _amt(s.balance),
        "close_contract": s.close_contract,
        "closing_expected": _amt(s.closing_expected),
        "counted": _amt(s.counted),
        "difference": _amt(s.difference),
        "closed_by": s.closed_by,
        "closed_at": s.closed_at,
        "handover": handover_out(h) if h is not None else None,
        "session_handover": session_handover_out(dh) if dh is not None else None,
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


def _violates(exc: IntegrityError, constraint: str) -> bool:
    """True only when PostgreSQL names ``constraint`` (a unique index name) as the one violated."""
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == constraint


def _open_replay(db: Session, tenant_id: int, actor: Principal, idempotency_key: str, digest: str) -> dict | None:
    """The answer to a repeated open (same key, digest and owner); a different request under the key conflicts."""
    prior = db.scalar(
        select(CashSession).where(
            CashSession.tenant_id == tenant_id, CashSession.open_idempotency_key == idempotency_key
        )
    )
    if prior is None:
        return None
    if prior.open_request_digest == digest and prior.cashier_id == actor.user_id:
        return session_out(db, prior, replayed=True)
    raise IdempotencyConflict()


def _close_replay(db: Session, tenant_id: int, s: CashSession, idempotency_key: str, digest: str) -> dict | None:
    """The answer to a repeated close of THIS session; the key on any other session conflicts."""
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
    return None



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
    if source == "initial":  # a declared amount: no denomination count, expected and counted are the same figure
        counted_map, counted = {}, fund
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
    replay = _open_replay(db, tenant_id, actor, idempotency_key, digest)
    if replay is not None:
        return replay
    cp = _cash_point(db, tenant_id, cash_point_id)
    require(actor, OPEN, **_scope(cp))
    if source == "zero":
        if fund != 0 or counted != 0:
            raise InvalidOpening("Una apertura en cero no recibe ni cuenta efectivo.")
    elif source == "initial":
        pass  # any non-negative amount (zero allowed); the amount is declared, so it is never a difference
    else:
        raise InvalidOpening("El origen de la apertura es cero o monto inicial: no se admite efectivo sin origen.")
    # locks: box -> cash point (the active-slot check serialises here; the partial UNIQUE index is the backstop)
    box = _box(db, tenant_id, cp.branch_id)
    cp = _cash_point(db, tenant_id, cash_point_id, lock=True)
    replay = _open_replay(db, tenant_id, actor, idempotency_key, digest)  # an identical request committed meanwhile
    if replay is not None:
        return replay
    if cp.status != "active":
        raise CashPointNotActive()  # D6: suspended (or inactive) blocks a NEW session only
    ensure_cash_point_usable(db, tenant_id, cp.id, "DOP")  # T-003 gate: tenant and branch active, currency admitted
    if db.scalar(
        select(CashSession.id).where(CashSession.cash_point_id == cp.id, CashSession.state.in_(ACTIVE_SESSION_STATES))
    ):
        raise CashPointBusy()
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
    try:
        with db.begin_nested():  # savepoint: only the session INSERT is undone if the tenant-global key collides
            db.add(s)
            db.flush()
    except IntegrityError as exc:
        if not _violates(exc, OPEN_KEY_CONSTRAINT):
            raise  # any other integrity failure keeps its own behaviour
        replay = _open_replay(db, tenant_id, actor, idempotency_key, digest)  # the winner: another branch or request
        if replay is None:
            raise
        return replay
    if source == "initial" and fund > 0:
        db.add(
            CashMovement(
                box_id=box.id,
                session_id=s.id,
                kind=INITIAL_FUND_KIND,
                amount=fund,
                actor_id=actor.user_id,
                notes="Monto inicial de apertura",
                reference=f"APE-{s.id}",
            )
        )
        db.flush()
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
    flows = [r for r in rows if r.kind not in OPENING_KINDS]
    incoming = sum((r.amount for r in flows if r.amount > 0), Decimal(0))
    outgoing = -sum((r.amount for r in flows if r.amount < 0), Decimal(0))
    additions = sum((r.amount for r in flows if r.kind == "cash_addition"), Decimal(0))
    return {
        "opening": _amt(expected - incoming + outgoing),
        "additions": _amt(additions),  # FM-FINANCIAL-CLOSURE-012: already part of ``incoming``; shown apart, never added twice
        "incoming": _amt(incoming),
        "outgoing": _amt(outgoing),
        "expected": _amt(expected),
        "counted": _amt(counted),
        "difference": _amt(difference),
        "movement_ids": [r.id for r in flows],
    }


def _valid_direct_receiver(db: Session, tenant_id: int, cp: CashPoint, receiver_user_id: int, maker_id: int) -> None:
    """A named next cashier: an active user of the tenant, never the maker, who may receive AND open HERE."""
    user = db.get(UserAccount, receiver_user_id)
    if user is None or user.company_id != tenant_id or not user.is_active or user.id == maker_id:
        raise InvalidReceiver()
    principal = build_principal(db, user, 0)
    if not (principal.allows(RECEIVE, **_scope(cp)) and principal.allows(OPEN, **_scope(cp))):
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
    destination: str = "next_session",
) -> dict:
    """Operational close by the session owner. The difference (if any) is a record; it never blocks the next session.

    The counted cash is left in a pending DIRECT handover (``next_session``) to a named cashier of the same CashPoint.
    Handing the cash over to Capital no longer exists.
    """
    tenant_id = _gate(actor)
    note = _note(observation_note)
    if not denominations:
        raise InvalidCount("El cierre exige el conteo fisico por denominaciones.")
    counted_map, counted = count_denominations(denominations)
    if destination not in DESTINATIONS:
        raise InvalidHandoverDestination()
    if destination == "next_session" and counted <= 0:
        raise NextSessionRequiresCash()
    payload = {
        "operation": "close_cash_session",
        "session_id": session_id,
        "denominations": counted_map,
        "observation_note": note,
        "receiver_user_id": receiver_user_id if counted > 0 else None,
    }
    payload["destination"] = "next_session"
    digest = _digest(payload)
    s = _session(db, tenant_id, session_id)
    replay = _close_replay(db, tenant_id, s, idempotency_key, digest)
    if replay is not None:
        return replay
    cp = _cash_point(db, tenant_id, s.cash_point_id)
    require(actor, CLOSE, **_scope(cp))
    if s.cashier_id != actor.user_id:
        raise NotSessionOwner()
    if db.scalar(select(CashDelivery.id).where(CashDelivery.box_id == s.box_id, CashDelivery.state == "surplus_review")):
        raise PendingSurplusReview()
    # locks: receiver user (FOR SHARE) -> box -> cash point -> session
    if counted > 0 and receiver_user_id is not None:
        _lock_user_share(db, receiver_user_id)
    _box(db, tenant_id, cp.branch_id)
    cp = _cash_point(db, tenant_id, s.cash_point_id, lock=True)
    s = _session(db, tenant_id, session_id, lock=True)
    replay = _close_replay(db, tenant_id, s, idempotency_key, digest)  # an identical request closed it meanwhile
    if replay is not None:
        return replay
    if s.state != "open":
        raise SessionNotOpen()
    expected = Decimal(s.balance)
    difference = counted - expected
    if difference != 0 and len(note) < 3:
        raise ObservationRequired()
    if counted > 0:
        if receiver_user_id is None:
            raise ReceiverRequired()
        _valid_direct_receiver(db, tenant_id, cp, receiver_user_id, s.cashier_id)
    at = now_utc()
    snapshot = _close_snapshot(db, s, expected, counted, difference) | (snapshot_extra or {})  # reads only
    try:
        with db.begin_nested():  # savepoint: only this UPDATE (which claims the tenant-global key) is undone
            s.counted, s.closing_expected, s.difference = counted, expected, difference
            s.denominations = counted_map
            s.close_contract = "v2"
            s.closed_by, s.closed_at = actor.user_id, at
            s.close_idempotency_key, s.close_request_digest = idempotency_key, digest
            s.snapshot = snapshot
            if note:
                s.notes = note
            s.state = "closing" if counted > 0 else "closed"
            s.version += 1
            db.flush()
    except IntegrityError as exc:
        if not _violates(exc, CLOSE_KEY_CONSTRAINT):
            raise
        replay = _close_replay(db, tenant_id, s, idempotency_key, digest)  # the session is restored: open, no key
        if replay is None:
            raise
        return replay
    if difference != 0:
        _record_difference(
            db, s, phase="closing", expected=expected, counted=counted, note=note, actor_id=actor.user_id, at=at
        )
    handover = direct = None
    if counted > 0 and destination == "next_session":
        direct = CashSessionHandover(
            tenant_id=tenant_id,
            box_id=s.box_id,
            cash_point_id=s.cash_point_id,
            source_session_id=s.id,
            from_user_id=s.cashier_id,
            to_user_id=receiver_user_id,
            amount=counted,
            currency_code="DOP",
            state="pending",
        )
        db.add(direct)
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
        **({"session_handover_id": direct.id} if direct is not None else {}),
    )
    if direct is not None:
        _audit(
            db,
            "cash.session_handover.declared",
            actor,
            client_ip,
            direct.to_user_id,
            session_handover_id=direct.id,
            session_id=s.id,
            cash_point_id=cp.id,
            branch_id=cp.branch_id,
            amount=_amt(direct.amount),
            from_user_id=direct.from_user_id,
            to_user_id=direct.to_user_id,
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


# --- simple close (FM-SIMPLE-CASH-010) -------------------------------------------------------------------
def close_simple_session(
    db: Session,
    actor: Principal,
    session_id: int,
    *,
    counted_amount,
    observation_note: str | None,
    idempotency_key: str,
    client_ip: str | None = None,
    snapshot_extra: dict | None = None,
) -> dict:
    """Close the actor's OWN ``initial`` session: ``open -> closed`` with the counted cash, whatever the difference.

    ``difference = counted - expected`` (SOBRANTE > 0, FALTANTE < 0, EXACTO = 0) is a recorded difference, never a
    balancing movement; no handover, receiver or denomination count exists in this flow. The counted cash stays with
    the cashier: the next session declares its own initial amount.
    """
    tenant_id = _gate(actor)
    note = _note(observation_note)
    try:
        counted = _money(counted_amount, allow_zero=True)
    except InvalidOpening:
        raise InvalidCount("El dinero contado debe ser un importe valido, no negativo y con centavos como maximo.") from None
    digest = _digest(
        {
            "operation": "close_cash_session_simple",
            "session_id": session_id,
            "counted": format(counted, "f"),
            "observation_note": note,
        }
    )
    s = _session(db, tenant_id, session_id)
    replay = _close_replay(db, tenant_id, s, idempotency_key, digest)
    if replay is not None:
        return replay
    if s.opening_source != "initial":
        raise InvalidOpening("Esta jornada no usa el cierre simple.")
    cp = _cash_point(db, tenant_id, s.cash_point_id)
    require(actor, CLOSE, **_scope(cp))
    if s.cashier_id != actor.user_id:
        raise NotSessionOwner()
    if db.scalar(select(CashDelivery.id).where(CashDelivery.box_id == s.box_id, CashDelivery.state == "surplus_review")):
        raise PendingSurplusReview()
    _box(db, tenant_id, cp.branch_id)
    cp = _cash_point(db, tenant_id, s.cash_point_id, lock=True)
    s = _session(db, tenant_id, session_id, lock=True)
    replay = _close_replay(db, tenant_id, s, idempotency_key, digest)  # an identical request closed it meanwhile
    if replay is not None:
        return replay
    if s.state != "open":
        raise SessionNotOpen()
    expected = Decimal(s.balance)
    difference = counted - expected
    at = now_utc()
    snapshot = _close_snapshot(db, s, expected, counted, difference) | (snapshot_extra or {})  # reads only
    try:
        with db.begin_nested():  # savepoint: only this UPDATE (which claims the tenant-global key) is undone
            s.counted, s.closing_expected, s.difference = counted, expected, difference
            s.denominations = {}
            s.close_contract = "v2"
            s.closed_by, s.closed_at = actor.user_id, at
            s.close_idempotency_key, s.close_request_digest = idempotency_key, digest
            s.snapshot = snapshot
            if note:
                s.notes = note
            s.state = "closed"
            s.version += 1
            db.flush()
    except IntegrityError as exc:
        if not _violates(exc, CLOSE_KEY_CONSTRAINT):
            raise
        replay = _close_replay(db, tenant_id, s, idempotency_key, digest)
        if replay is None:
            raise
        return replay
    if difference != 0:
        # the difference record always carries an observation (database backstop); when the cashier wrote none it carries
        # this explicit system marker, never an invented explanation, and the session's own notes stay untouched
        _record_difference(
            db,
            s,
            phase="closing",
            expected=expected,
            counted=counted,
            note=note or SIMPLE_CLOSE_NO_NOTE,
            actor_id=actor.user_id,
            at=at,
        )
    _audit(
        db,
        "cash.session.closed",
        actor,
        client_ip,
        s.cashier_id,
        session_id=s.id,
        cash_point_id=cp.id,
        branch_id=cp.branch_id,
        expected=_amt(expected),
        counted=_amt(counted),
        difference=_amt(difference),
        handover_id=None,
        mode="simple",
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


def _visible(actor: Principal, permission: str, branch_id: int | None) -> tuple[list[int] | None, list[int]]:
    """(branches, cash_points) the actor may see for ``permission``; branches None = every branch (tenant scope).

    T-022A: a cash_point-scoped grant counts too (it used to be ignored, so a CashPoint-scoped reviewer saw nothing)."""
    if branch_id is not None and actor.allows(permission, tenant_id=actor.tenant_id, branch_id=branch_id):
        return [branch_id], []
    if branch_id is None and actor.holds_at_tenant_scope(permission):
        return None, []
    grants = [g for g in actor.grants if g.permission == permission and permission in CATALOG_CODES]
    branches = sorted({g.branch_id for g in grants if g.scope_kind == "branch"})
    points = sorted({g.cash_point_id for g in grants if g.scope_kind == "cash_point"})
    if not points and (branch_id is not None or not branches):
        raise PermissionDenied()
    return ([] if branch_id is not None else branches), points


def _scoped(stmt, branches: list[int] | None, points: list[int], branch_id: int | None):
    """Restrict a CashPoint-joined query to what ``_visible`` allowed (and to ``branch_id`` when asked)."""
    if branches is not None:
        stmt = stmt.where(or_(CashPoint.branch_id.in_(branches), CashPoint.id.in_(points)))
    if branch_id is not None:
        stmt = stmt.where(CashPoint.branch_id == branch_id)
    return stmt


def list_differences(
    db: Session,
    actor: Principal,
    *,
    status: str,
    branch_id: int | None,
    limit: int,
    before_id: int | None,
    phase: str | None = None,
    provenance: str | None = None,
) -> dict:
    tenant_id = _gate(actor)
    branches, points = _visible(actor, DIFF_READ, branch_id)
    stmt = (
        select(CashSessionDifference)
        .join(CashPoint, CashPoint.id == CashSessionDifference.cash_point_id)
        .where(CashSessionDifference.tenant_id == tenant_id, CashSessionDifference.status == status)
        .order_by(CashSessionDifference.id.desc())
        .limit(limit)
    )
    stmt = _scoped(stmt, branches, points, branch_id)
    if phase is not None:
        stmt = stmt.where(CashSessionDifference.phase == phase)
    if provenance is not None:
        stmt = stmt.where(CashSessionDifference.provenance == provenance)
    if before_id is not None:
        stmt = stmt.where(CashSessionDifference.id < before_id)
    rows = db.scalars(stmt).all()
    resolutions = {
        r.difference_id: r
        for r in db.scalars(
            select(CashDifferenceResolution).where(CashDifferenceResolution.difference_id.in_([d.id for d in rows]))
        )
    }
    items = [
        {**difference_out(d), "resolution": resolution_out(resolutions[d.id]) if d.id in resolutions else None}
        for d in rows
    ]
    return {"items": items, "next_before_id": items[-1]["id"] if len(items) == limit else None}


def base_cash_point_id(db: Session, box_id: int) -> int | None:
    """The CashPoint created with a cash box (the legacy command's default position)."""
    return db.scalar(select(CashPoint.id).where(CashPoint.box_id == box_id))


# --- user lifecycle guard (T-023A, D20) ---------------------------------------------------------------------
def has_cash_responsibility(db: Session, tenant_id: int, user_id: int) -> bool:
    """The user owns an open / closing session, or is the named receiver of a pending,
    NOT declined direct handover (a receiver who declined has discharged that responsibility). ONE query."""
    owns_session = exists().where(
        CashSession.tenant_id == tenant_id,
        CashSession.cashier_id == user_id,
        CashSession.state.in_(ACTIVE_SESSION_STATES),
    )
    direct_receiver = exists().where(
        CashSessionHandover.tenant_id == tenant_id,
        CashSessionHandover.to_user_id == user_id,
        CashSessionHandover.state == "pending",
        CashSessionHandover.declined_at.is_(None),
    )
    return bool(db.scalar(select(or_(owns_session, direct_receiver))))
