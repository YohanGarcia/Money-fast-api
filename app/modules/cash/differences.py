"""Cash difference review and resolution (T-022A, DR-007): the decision about a physical difference, never its erasure.

* A difference is the immutable observation T-021 recorded. Resolving it adds ONE immutable
  ``CashDifferenceResolution`` and moves its status ``pending_review -> resolved`` (nothing else exists: no
  ``under_review``, no ``dismissed``). ``resolved`` means "review decision complete": it is NOT "accounting posted".
* It is resolved only once its session is ``closed`` (terminal), by someone other than the cashier, the opener, the
  closer and the detector, holding the explicit ``cash.differences.resolve`` permission for the CashPoint (no admin
  bypass).
* Types: ``no_further_action`` (opening or closing, either sign, disposition ``none``); ``accepted_loss`` (closing
  shortage) and ``accepted_surplus`` (closing overage), disposition ``posting_required``, with a mandatory reference. An
  opening difference never owns an economic consequence, so one session has at most one ``posting_required``.
* A resolution moves NO cash and NO capital (capital already holds the counted cash of the T-021 handover) and rewrites
  no session, count or movement. Any accounting posting is T-022B's own truth, keyed by ``resolution_id``.

Lock order: cash box (legacy container) -> difference. The session is only read: ``closed`` is terminal. Functions here
never commit: the HTTP layer commits the unit of work.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflict
from app.core.time import now_utc
from app.models.cash import DISPOSITION_BY_TYPE, CashDifferenceResolution, CashSession, CashSessionDifference
from app.modules.cash.errors import (
    DifferenceNotFound,
    DifferenceNotPending,
    MakerCannotResolve,
    ResolutionNotApplicable,
    ResolutionReasonRequired,
    ResolutionReferenceRequired,
    SessionNotClosed,
)
from app.modules.cash.sessions import (
    DIFF_READ,
    _audit,
    _box,
    _cash_point,
    _digest,
    _gate,
    _scope,
    _session,
    difference_out,
    resolution_out,
)
from app.modules.identity.authorization import Principal, require
from app.modules.identity.errors import PermissionDenied
from app.modules.organization.models import CashPoint

RESOLVE = "cash.differences.resolve"
MIN_REASON = 10
MIN_REFERENCE = 3
EVENT_RESOLVED = "cash.difference.resolved"


# --- rules ----------------------------------------------------------------------------------------------
def applicable_types(d: CashSessionDifference) -> list[str]:
    """The resolution types the difference admits (opening: only ``no_further_action``; closing: also by sign)."""
    types = ["no_further_action"]
    if d.phase == "closing":
        types.append("accepted_loss" if d.difference < 0 else "accepted_surplus")
    return types


def _independent(actor: Principal, s: CashSession, d: CashSessionDifference) -> bool:
    """The resolver is none of: the cashier, the opener, the closer, the detector (a missing closer excludes no one)."""
    return actor.user_id not in {s.cashier_id, s.opened_by, s.closed_by, d.detected_by}


def _difference(db: Session, tenant_id: int, difference_id: int, *, lock: bool = False) -> CashSessionDifference:
    stmt = select(CashSessionDifference).where(
        CashSessionDifference.id == difference_id, CashSessionDifference.tenant_id == tenant_id
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    d = db.scalar(stmt)
    if d is None:
        raise DifferenceNotFound()
    return d


def _resolution(db: Session, difference_id: int) -> CashDifferenceResolution | None:
    return db.scalar(select(CashDifferenceResolution).where(CashDifferenceResolution.difference_id == difference_id))


def _can_read(actor: Principal, cp: CashPoint) -> bool:
    return actor.allows(DIFF_READ, **_scope(cp)) or actor.allows(RESOLVE, **_scope(cp))


def difference_detail(db: Session, d: CashSessionDifference, s: CashSession, *, replayed: bool | None = None) -> dict:
    """The difference, its resolution if any, and the same session's opening/closing pair (derived, never stored)."""
    siblings = {
        x.phase: x
        for x in db.scalars(select(CashSessionDifference).where(CashSessionDifference.session_id == d.session_id))
    }
    resolution = _resolution(db, d.id)
    next_action = None
    if d.status == "pending_review":
        if s.state == "closed":
            next_action = {
                "action": "resolve_difference",
                "endpoint": f"/api/v2/cash/differences/{d.id}/resolve",
                "resolution_types": applicable_types(d),
            }
        else:
            next_action = {"action": "wait_session_closed", "session_state": s.state}
    out = {
        "difference": difference_out(d),
        "resolution": resolution_out(resolution) if resolution is not None else None,
        "accounting_disposition": resolution.accounting_disposition if resolution is not None else None,
        "session": {
            "id": s.id,
            "state": s.state,
            "cash_point_id": s.cash_point_id,
            "cashier_user_id": s.cashier_id,
            "opened_by": s.opened_by,
            "closed_by": s.closed_by,
            "closed_at": s.closed_at,
        },
        "opening_difference": difference_out(siblings["opening"]) if "opening" in siblings else None,
        "closing_difference": difference_out(siblings["closing"]) if "closing" in siblings else None,
        "next_action": next_action,
    }
    if replayed is not None:
        out["replayed"] = replayed
    return out


# --- reads (pure) ---------------------------------------------------------------------------------------
def get_difference(db: Session, actor: Principal, difference_id: int) -> dict:
    tenant_id = _gate(actor)
    d = _difference(db, tenant_id, difference_id)
    cp = _cash_point(db, tenant_id, d.cash_point_id)
    if not _can_read(actor, cp):
        raise PermissionDenied()
    return difference_detail(db, d, _session(db, tenant_id, d.session_id))


# --- command --------------------------------------------------------------------------------------------
def _check_text(resolution_type: str, reason: str | None, reference: str | None) -> tuple[str, str | None]:
    reason = (reason or "").strip()
    reference = (reference or "").strip() or None
    if len(reason) < MIN_REASON:
        raise ResolutionReasonRequired()
    if resolution_type != "no_further_action" and (reference is None or len(reference) < MIN_REFERENCE):
        raise ResolutionReferenceRequired()
    return reason, reference


def _replay(
    db: Session, tenant_id: int, actor: Principal, idempotency_key: str, digest: str, difference_id: int
) -> dict | None:
    prior = db.scalar(
        select(CashDifferenceResolution).where(
            CashDifferenceResolution.tenant_id == tenant_id, CashDifferenceResolution.idempotency_key == idempotency_key
        )
    )
    if prior is None:
        return None
    if prior.request_digest == digest and prior.resolved_by == actor.user_id and prior.difference_id == difference_id:
        d = _difference(db, tenant_id, difference_id)
        return difference_detail(db, d, _session(db, tenant_id, d.session_id), replayed=True)
    raise IdempotencyConflict()


def resolve_difference(
    db: Session,
    actor: Principal,
    difference_id: int,
    *,
    idempotency_key: str,
    resolution_type: str,
    reason: str | None = None,
    reference: str | None = None,
    client_ip: str | None = None,
) -> dict:
    """Record the review decision of a closed session's pending difference: one resolution + status ``resolved``."""
    tenant_id = _gate(actor)
    d = _difference(db, tenant_id, difference_id)
    cp = _cash_point(db, tenant_id, d.cash_point_id)
    require(actor, RESOLVE, **_scope(cp))
    reason, reference = _check_text(resolution_type, reason, reference)
    digest = _digest(
        {
            "operation": "resolve_cash_difference",
            "difference_id": difference_id,
            "resolution_type": resolution_type,
            "reason": reason,
            "reference": reference,
        }
    )
    replay = _replay(db, tenant_id, actor, idempotency_key, digest, difference_id)
    if replay is not None:
        return replay
    # locks: box -> difference (the session is only read: closed is terminal)
    _box(db, tenant_id, cp.branch_id)
    d = _difference(db, tenant_id, difference_id, lock=True)
    replay = _replay(db, tenant_id, actor, idempotency_key, digest, difference_id)  # a concurrent identical request won
    if replay is not None:
        return replay
    if d.status != "pending_review":
        raise DifferenceNotPending()
    if resolution_type not in applicable_types(d):
        raise ResolutionNotApplicable()
    s = _session(db, tenant_id, d.session_id)
    if s.state != "closed":
        raise SessionNotClosed()
    if not _independent(actor, s, d):
        raise MakerCannotResolve()
    resolution = CashDifferenceResolution(
        tenant_id=tenant_id,
        difference_id=d.id,
        session_id=d.session_id,
        phase=d.phase,
        resolution_type=resolution_type,
        accounting_disposition=DISPOSITION_BY_TYPE[resolution_type],
        reason=reason,
        reference=reference,
        resolved_by=actor.user_id,
        resolved_at=now_utc(),
        idempotency_key=idempotency_key,
        request_digest=digest,
    )
    db.add(resolution)
    db.flush()
    d.status = "resolved"
    db.flush()
    _audit(
        db,
        EVENT_RESOLVED,
        actor,
        client_ip,
        s.cashier_id,
        difference_id=d.id,
        resolution_id=resolution.id,
        session_id=s.id,
        cash_point_id=cp.id,
        branch_id=cp.branch_id,
        phase=d.phase,
        provenance=d.provenance,
        resolution_type=resolution.resolution_type,
        accounting_disposition=resolution.accounting_disposition,
    )
    return difference_detail(db, d, s, replayed=False)
