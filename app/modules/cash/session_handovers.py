"""Same-CashPoint direct session handover (T-023A, CASH-CORE-03): accept / decline / redirect and their reads.

``close`` with ``destination = next_session`` (``sessions.close_session``) leaves the closing session's exact
counted cash in ONE pending ``cash_session_handovers`` row for a named cashier of the SAME CashPoint. Here the
lifecycle continues:

* ``accept`` (the named receiver only, never the maker, holding ``cash.handovers.receive`` and
  ``cash.sessions.open`` for the CashPoint, re-validated at acceptance): the receiver recounts the cash by
  denomination with a FRESH, full map whose total must equal the handover exactly (a different composition is
  fine). In ONE transaction: claim the accept key -> source ``session_handover_out`` movement -> handover
  ``confirmed`` -> source ``closed`` (the active-slot index is non-deferrable, so the source leaves it BEFORE
  the new session enters) -> receiving ``open`` session (``opening_source = handover``, owned by the receiver)
  -> ``opening_handover_fund`` movement. Cash moves between two sessions: no capital, no opening difference,
  aggregate cash effect 0. A suspended CashPoint refuses the acceptance (it opens a NEW session). The
  receiver's count is the receiving session's ``opening_denominations`` (single source of truth);
* ``decline`` (the named receiver; refusing custody needs no permission): an immutable annotation, the row
  stays ``pending`` but can never be accepted; its only next step is ``redirect``;
* ``redirect`` (the maker before a decline, or an actor holding ``cash.handovers.redirect``; ONLY the latter
  after a decline): atomically cancels the row, creates the replacement (another direct handover of the same
  CashPoint, or a T-021 capital handover) and fills the old row's single immutable forward pointer. A bare
  cancel does not exist, and there is no way back from capital.

Lock order: receiver user (FOR SHARE) -> cash box -> cash point -> source session -> handover. Every command
owns a TENANT-global key: replay before the locks, replay again right after them (before any state check),
claim as the first write inside a savepoint and classify ONLY the named unique index (any other
``IntegrityError`` propagates). Functions never commit.
"""

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflict
from app.core.time import business_date, now_utc
from app.models.cash import CashCustodyTransfer, CashMovement, CashSession, CashSessionHandover
from app.modules.cash.errors import (
    CashPointNotActive,
    HandoverDeclined,
    HandoverReasonRequired,
    InvalidCount,
    InvalidHandoverDestination,
    InvalidReceiver,
    MakerCannotAccept,
    NotNamedReceiver,
    ReceiverCountMismatch,
    RedirectNotAuthorized,
    SessionHandoverNotFound,
    SessionHandoverNotPending,
)
from app.modules.cash.sessions import (
    HANDOVER_OUT_KIND,
    OPEN,
    OPENING_HANDOVER_FUND_KIND,
    READ,
    RECEIVE,
    REDIRECT,
    _amt,
    _audit,
    _box,
    _cash_point,
    _digest,
    _gate,
    _scope,
    _session,
    _valid_direct_receiver,
    _valid_receiver,
    _violates,
    _visible,
    count_denominations,
    handover_out,
    session_handover_out,
    session_out,
)
from app.modules.identity.authorization import Principal, build_principal, require
from app.modules.identity.errors import PermissionDenied
from app.modules.identity.models import UserAccount
from app.modules.organization.models import CashPoint
from app.modules.organization.service import ensure_cash_point_usable

MIN_REASON = 10
# the tenant-global anchors of the three commands (their PostgreSQL names are the only collisions that are classified)
ACCEPT_KEY_CONSTRAINT = "uq_cash_session_handovers_accept_key"
DECLINE_KEY_CONSTRAINT = "uq_cash_session_handovers_decline_key"
REDIRECT_KEY_CONSTRAINT = "uq_cash_session_handovers_redirect_key"


# --- helpers --------------------------------------------------------------------------------------------
def _handover(db: Session, tenant_id: int, handover_id: int, *, lock: bool = False) -> CashSessionHandover:
    stmt = select(CashSessionHandover).where(
        CashSessionHandover.id == handover_id, CashSessionHandover.tenant_id == tenant_id
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    h = db.scalar(stmt)
    if h is None:
        raise SessionHandoverNotFound()
    return h


def _reason(raw: str | None) -> str:
    reason = (raw or "").strip()
    if len(reason) < MIN_REASON:
        raise HandoverReasonRequired()
    return reason


def _lock_receiver(db: Session, user_id: int) -> UserAccount | None:
    """The user row FOR SHARE, freshly read (a concurrent ``disable_user`` takes it FOR UPDATE)."""
    return db.scalar(
        select(UserAccount)
        .where(UserAccount.id == user_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )


def handover_detail(db: Session, h: CashSessionHandover, *, replayed: bool | None = None) -> dict:
    """The direct handover, its source session, the receiving session it opened (if confirmed) and its replacement."""
    receiving = None
    if h.state == "confirmed":
        receiving = db.scalar(select(CashSession).where(CashSession.opening_handover_id == h.id))
    replacement = None
    if h.redirected_to_session_handover_id is not None:
        replacement = {
            "type": "session_handover",
            "handover": session_handover_out(db.get(CashSessionHandover, h.redirected_to_session_handover_id)),
        }
    elif h.redirected_to_capital_handover_id is not None:
        replacement = {
            "type": "capital_handover",
            "handover": handover_out(db.get(CashCustodyTransfer, h.redirected_to_capital_handover_id)),
        }
    source = _session(db, h.tenant_id, h.source_session_id)
    out = {
        "session_handover": {
            **session_handover_out(h),
            "receiving_session_id": receiving.id if receiving is not None else None,
            "receiver_denominations": receiving.opening_denominations if receiving is not None else None,
        },
        "replacement": replacement,
        "source_session": session_out(db, source),
        "receiving_session": session_out(db, receiving) if receiving is not None else None,
    }
    if replayed is not None:
        out["replayed"] = replayed
    return out


def _accept_replay(
    db: Session, tenant_id: int, actor: Principal, h: CashSessionHandover, key: str, digest: str
) -> dict | None:
    if h.accept_idempotency_key == key:
        if h.accept_request_digest == digest and h.accepted_by == actor.user_id:
            return handover_detail(db, h, replayed=True)
        raise IdempotencyConflict()
    if db.scalar(
        select(CashSessionHandover.id).where(
            CashSessionHandover.tenant_id == tenant_id, CashSessionHandover.accept_idempotency_key == key
        )
    ):
        raise IdempotencyConflict()
    return None


def _decline_replay(
    db: Session, tenant_id: int, actor: Principal, h: CashSessionHandover, key: str, digest: str
) -> dict | None:
    if h.decline_idempotency_key == key:
        if h.decline_request_digest == digest and h.declined_by == actor.user_id:
            return handover_detail(db, h, replayed=True)
        raise IdempotencyConflict()
    if db.scalar(
        select(CashSessionHandover.id).where(
            CashSessionHandover.tenant_id == tenant_id, CashSessionHandover.decline_idempotency_key == key
        )
    ):
        raise IdempotencyConflict()
    return None


def _redirect_replay(
    db: Session, tenant_id: int, actor: Principal, h: CashSessionHandover, key: str, digest: str
) -> dict | None:
    if h.redirect_idempotency_key == key:
        if h.redirect_request_digest == digest and h.redirected_by == actor.user_id:
            return handover_detail(db, h, replayed=True)
        raise IdempotencyConflict()
    if db.scalar(
        select(CashSessionHandover.id).where(
            CashSessionHandover.tenant_id == tenant_id, CashSessionHandover.redirect_idempotency_key == key
        )
    ):
        raise IdempotencyConflict()
    return None


# --- accept (the atomic accept-and-open) -----------------------------------------------------------------
def accept_session_handover(
    db: Session,
    actor: Principal,
    handover_id: int,
    *,
    idempotency_key: str,
    denominations: dict | None,
    client_ip: str | None = None,
) -> dict:
    """The named receiver recounts the cash, the source closes and the receiver's own session opens: one transaction."""
    tenant_id = _gate(actor)
    if not denominations:
        raise InvalidCount("El receptor debe contar el efectivo recibido por denominaciones.")
    counted_map, counted = count_denominations(denominations)
    digest = _digest({"operation": "accept_session_handover", "handover_id": handover_id, "denominations": counted_map})
    h = _handover(db, tenant_id, handover_id)
    replay = _accept_replay(db, tenant_id, actor, h, idempotency_key, digest)
    if replay is not None:
        return replay
    cp = _cash_point(db, tenant_id, h.cash_point_id)
    require(actor, RECEIVE, **_scope(cp))
    require(actor, OPEN, **_scope(cp))
    # locks: receiver user (FOR SHARE) -> box -> cash point -> source session -> handover
    user = _lock_receiver(db, actor.user_id)
    _box(db, tenant_id, cp.branch_id)
    cp = _cash_point(db, tenant_id, h.cash_point_id, lock=True)
    s = _session(db, tenant_id, h.source_session_id, lock=True)
    h = _handover(db, tenant_id, handover_id, lock=True)
    replay = _accept_replay(db, tenant_id, actor, h, idempotency_key, digest)  # an identical request won meanwhile
    if replay is not None:
        return replay
    if h.state != "pending":
        raise SessionHandoverNotPending()
    if h.declined_at is not None:
        raise HandoverDeclined()
    if s.state != "closing":
        raise SessionHandoverNotPending()
    if actor.user_id == h.from_user_id:
        raise MakerCannotAccept()
    if actor.user_id != h.to_user_id:
        raise NotNamedReceiver()
    # permissions and status are re-read NOW (the principal of the request may predate a revocation or a disable)
    if user is None or user.company_id != tenant_id or not user.is_active:
        raise InvalidReceiver()
    fresh = build_principal(db, user, 0)
    if not (fresh.allows(RECEIVE, **_scope(cp)) and fresh.allows(OPEN, **_scope(cp))):
        raise PermissionDenied()
    if cp.status != "active":
        raise CashPointNotActive()  # D18: the acceptance opens a NEW session, so suspension blocks it
    ensure_cash_point_usable(db, tenant_id, cp.id, "DOP")
    if counted != h.amount:
        raise ReceiverCountMismatch()  # nothing is written: no session, no movement, no difference
    at = now_utc()
    # 1. claim the accept key FIRST, alone in a savepoint, before any economic effect
    try:
        with db.begin_nested():
            h.accept_idempotency_key, h.accept_request_digest = idempotency_key, digest
            db.flush()
    except IntegrityError as exc:
        if not _violates(exc, ACCEPT_KEY_CONSTRAINT):
            raise
        replay = _accept_replay(db, tenant_id, actor, h, idempotency_key, digest)  # the winner (other branch/request)
        if replay is None:
            raise
        return replay
    # 2. the source's negative movement
    out_movement = CashMovement(
        box_id=s.box_id,
        session_id=s.id,
        kind=HANDOVER_OUT_KIND,
        amount=-h.amount,
        actor_id=actor.user_id,
        notes="Entrega directa a la siguiente jornada",
        reference=f"ENT-{h.id}",
        session_handover_id=h.id,
    )
    db.add(out_movement)
    db.flush()
    # 3. the handover is confirmed
    h.state = "confirmed"
    h.accepted_by, h.accepted_at = actor.user_id, at
    h.version += 1
    db.flush()
    # 4. the source closes BEFORE the new session enters (the active-slot index is not deferrable)
    s.balance = s.balance - h.amount
    s.state = "closed"
    s.version += 1
    db.flush()
    # 5. the receiving session, opened by (and owned by) the receiver, with the receiver's own count
    receiving = CashSession(
        box_id=s.box_id,
        tenant_id=tenant_id,
        cash_point_id=cp.id,
        currency_code="DOP",
        business_date=business_date(at),
        state="open",
        opening_source="handover",
        opening_contract="v2",
        opening_expected=h.amount,
        opening_counted=counted,
        opening_denominations=counted_map,
        opening_handover_id=h.id,
        balance=0,
        balance_base=0,
        notes="",
        opened_by=actor.user_id,
        cashier_id=actor.user_id,
        opened_at=at,
    )
    db.add(receiving)
    db.flush()
    # 6. + 7. the receiving movement and the balance it backs
    fund = CashMovement(
        box_id=s.box_id,
        session_id=receiving.id,
        kind=OPENING_HANDOVER_FUND_KIND,
        amount=h.amount,
        actor_id=actor.user_id,
        notes="Fondo de apertura recibido de la jornada anterior",
        reference=f"APH-{h.id}",
        session_handover_id=h.id,
    )
    db.add(fund)
    db.flush()
    receiving.balance = receiving.balance + h.amount
    receiving.version += 1
    db.flush()
    # 8. audit: ids and structured facts only
    _audit(
        db,
        "cash.session_handover.accepted",
        actor,
        client_ip,
        h.from_user_id,
        session_handover_id=h.id,
        source_session_id=s.id,
        receiving_session_id=receiving.id,
        cash_point_id=cp.id,
        branch_id=cp.branch_id,
        amount=_amt(h.amount),
        source_movement_id=out_movement.id,
        receiving_movement_id=fund.id,
    )
    _audit(
        db,
        "cash.session.opened",
        actor,
        client_ip,
        actor.user_id,
        session_id=receiving.id,
        cash_point_id=cp.id,
        branch_id=cp.branch_id,
        opening_source="handover",
        opening_expected=_amt(h.amount),
        opening_counted=_amt(counted),
        session_handover_id=h.id,
    )
    return handover_detail(db, h, replayed=False)


# --- decline ---------------------------------------------------------------------------------------------
def decline_session_handover(
    db: Session,
    actor: Principal,
    handover_id: int,
    *,
    idempotency_key: str,
    reason: str | None,
    client_ip: str | None = None,
) -> dict:
    """The named receiver refuses the custody: an immutable annotation on the pending row (never a fourth state).

    No permission is needed to REFUSE cash (it may have been revoked meanwhile); authentication and tenant still apply.
    """
    tenant_id = _gate(actor)
    reason = _reason(reason)
    digest = _digest({"operation": "decline_session_handover", "handover_id": handover_id, "reason": reason})
    h = _handover(db, tenant_id, handover_id)
    replay = _decline_replay(db, tenant_id, actor, h, idempotency_key, digest)
    if replay is not None:
        return replay
    cp = _cash_point(db, tenant_id, h.cash_point_id)
    # locks: box -> handover (every writer of a handover starts with the box, which serialises the branch)
    _box(db, tenant_id, cp.branch_id)
    h = _handover(db, tenant_id, handover_id, lock=True)
    replay = _decline_replay(db, tenant_id, actor, h, idempotency_key, digest)  # an identical request won meanwhile
    if replay is not None:
        return replay
    if actor.user_id != h.to_user_id:
        raise NotNamedReceiver()
    if h.state != "pending":
        raise SessionHandoverNotPending()
    if h.declined_at is not None:
        raise HandoverDeclined()
    at = now_utc()
    try:
        with db.begin_nested():  # savepoint: only this UPDATE (which claims the tenant-global key) is undone
            h.decline_idempotency_key, h.decline_request_digest = idempotency_key, digest
            h.declined_by, h.declined_at, h.decline_reason = actor.user_id, at, reason
            h.version += 1
            db.flush()
    except IntegrityError as exc:
        if not _violates(exc, DECLINE_KEY_CONSTRAINT):
            raise
        replay = _decline_replay(db, tenant_id, actor, h, idempotency_key, digest)
        if replay is None:
            raise
        return replay
    _audit(
        db,
        "cash.session_handover.declined",
        actor,
        client_ip,
        h.from_user_id,
        session_handover_id=h.id,
        source_session_id=h.source_session_id,
        cash_point_id=cp.id,
        branch_id=cp.branch_id,
        amount=_amt(h.amount),
    )
    return handover_detail(db, h, replayed=False)


# --- redirect --------------------------------------------------------------------------------------------
def redirect_session_handover(
    db: Session,
    actor: Principal,
    handover_id: int,
    *,
    idempotency_key: str,
    destination: str,
    receiver_user_id: int,
    reason: str | None,
    client_ip: str | None = None,
) -> dict:
    """Atomically cancel the row, create its replacement and fill its single forward pointer (no bare cancel exists)."""
    tenant_id = _gate(actor)
    if destination not in ("next_session", "capital"):
        raise InvalidHandoverDestination()
    reason = _reason(reason)
    digest = _digest(
        {
            "operation": "redirect_session_handover",
            "handover_id": handover_id,
            "destination": destination,
            "receiver_user_id": receiver_user_id,
            "reason": reason,
        }
    )
    h = _handover(db, tenant_id, handover_id)
    replay = _redirect_replay(db, tenant_id, actor, h, idempotency_key, digest)
    if replay is not None:
        return replay
    cp = _cash_point(db, tenant_id, h.cash_point_id)
    supervisor = actor.allows(REDIRECT, **_scope(cp))
    if actor.user_id != h.from_user_id and not supervisor:
        raise RedirectNotAuthorized()
    # locks: new receiver user (FOR SHARE) -> box -> cash point -> source session -> handover
    _lock_receiver(db, receiver_user_id)
    _box(db, tenant_id, cp.branch_id)
    cp = _cash_point(db, tenant_id, h.cash_point_id, lock=True)
    s = _session(db, tenant_id, h.source_session_id, lock=True)
    h = _handover(db, tenant_id, handover_id, lock=True)
    replay = _redirect_replay(db, tenant_id, actor, h, idempotency_key, digest)  # an identical request won meanwhile
    if replay is not None:
        return replay
    if h.state != "pending" or s.state != "closing":
        raise SessionHandoverNotPending()
    if h.declined_at is not None:  # a disputed cash is redirected by supervision only, never by the maker alone
        if not supervisor:
            raise RedirectNotAuthorized()
    elif actor.user_id != h.from_user_id and not supervisor:
        raise RedirectNotAuthorized()
    if destination == "next_session":
        _valid_direct_receiver(db, tenant_id, cp, receiver_user_id, h.from_user_id)
    else:
        _valid_receiver(db, tenant_id, cp, receiver_user_id, h.from_user_id)
    at = now_utc()
    # 1. cancel the old row, claiming the redirect key as the first write (one UPDATE inside a savepoint)
    try:
        with db.begin_nested():
            h.state = "cancelled"
            h.redirect_idempotency_key, h.redirect_request_digest = idempotency_key, digest
            h.redirected_by, h.redirected_at, h.redirect_reason = actor.user_id, at, reason
            h.version += 1
            db.flush()
    except IntegrityError as exc:
        if not _violates(exc, REDIRECT_KEY_CONSTRAINT):
            raise
        replay = _redirect_replay(db, tenant_id, actor, h, idempotency_key, digest)
        if replay is None:
            raise
        return replay
    # 2. the replacement
    if destination == "next_session":
        replacement = CashSessionHandover(
            tenant_id=tenant_id,
            box_id=h.box_id,
            cash_point_id=h.cash_point_id,
            source_session_id=h.source_session_id,
            from_user_id=h.from_user_id,
            to_user_id=receiver_user_id,
            amount=h.amount,
            currency_code="DOP",
            state="pending",
        )
    else:
        replacement = CashCustodyTransfer(
            company_id=tenant_id,
            box_id=h.box_id,
            session_id=h.source_session_id,
            kind="closing_capital",
            from_user_id=h.from_user_id,
            to_user_id=receiver_user_id,
            amount=h.amount,
            currency_code="DOP",
            state="pending",
            provenance="v2",
            notes="",
        )
    db.add(replacement)
    db.flush()
    # 3. the single immutable forward pointer of the cancelled row
    if destination == "next_session":
        h.redirected_to_session_handover_id = replacement.id
    else:
        h.redirected_to_capital_handover_id = replacement.id
    db.flush()
    _audit(
        db,
        "cash.session_handover.redirected",
        actor,
        client_ip,
        h.from_user_id,
        session_handover_id=h.id,
        source_session_id=h.source_session_id,
        cash_point_id=cp.id,
        branch_id=cp.branch_id,
        amount=_amt(h.amount),
        destination=destination,
        replacement_id=replacement.id,
        after_decline=h.declined_at is not None,
    )
    return handover_detail(db, h, replayed=False)


# --- reads (pure) ----------------------------------------------------------------------------------------
def get_session_handover(db: Session, actor: Principal, handover_id: int) -> dict:
    tenant_id = _gate(actor)
    h = _handover(db, tenant_id, handover_id)
    cp = _cash_point(db, tenant_id, h.cash_point_id)
    if actor.user_id not in (h.from_user_id, h.to_user_id) and not any(
        actor.allows(p, **_scope(cp)) for p in (RECEIVE, REDIRECT, READ)
    ):
        raise PermissionDenied()
    return handover_detail(db, h)


def list_session_handovers(
    db: Session, actor: Principal, *, state: str, branch_id: int | None, limit: int, before_id: int | None
) -> dict:
    """The direct handovers the actor takes part in, or may receive / redirect / read for their scope."""
    tenant_id = _gate(actor)
    everywhere, branches, points = False, set(), set()
    for permission in (RECEIVE, REDIRECT, READ):
        try:
            b, p = _visible(actor, permission, branch_id)
        except PermissionDenied:
            continue
        if b is None:
            everywhere = True
        else:
            branches |= set(b)
            points |= set(p)
    stmt = (
        select(CashSessionHandover)
        .join(CashPoint, CashPoint.id == CashSessionHandover.cash_point_id)
        .where(CashSessionHandover.tenant_id == tenant_id, CashSessionHandover.state == state)
        .order_by(CashSessionHandover.id.desc())
        .limit(limit)
    )
    if not everywhere:
        stmt = stmt.where(
            or_(
                CashSessionHandover.to_user_id == actor.user_id,
                CashSessionHandover.from_user_id == actor.user_id,
                CashPoint.branch_id.in_(branches),
                CashPoint.id.in_(points),
            )
        )
    if branch_id is not None:
        stmt = stmt.where(CashPoint.branch_id == branch_id)
    if before_id is not None:
        stmt = stmt.where(CashSessionHandover.id < before_id)
    rows = db.scalars(stmt).all()
    receiving = dict(
        db.execute(
            select(CashSession.opening_handover_id, CashSession.id).where(
                CashSession.opening_handover_id.in_([h.id for h in rows])
            )
        ).all()
    )
    items = [{**session_handover_out(h), "receiving_session_id": receiving.get(h.id)} for h in rows]
    return {"items": items, "next_before_id": items[-1]["id"] if len(items) == limit else None}
