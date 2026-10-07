"""Field cash custody and rendition (T-019): PHYSICAL custody of field-collected cash, separate from debt and from cash sessions.

Three truths are kept apart: the payment (debt applied, T-008), the custody receipt (who physically holds that cash) and the
cash session (when the cash was accepted into a drawer). A payment applied to debt does not prove the cash is in a drawer;
a reversal of the debt (T-009) does not prove a refund, so it NEVER changes custody.

* Birth: ``create_receipt`` runs inside ``payments.pay`` for ``origin = field`` (same transaction). Custodian = the payment's
  ``collected_by`` = the authenticated actor: there is no proxy collection. Indivisible: the whole payment.
* Rendition: the custodian declares receipts (``declared``); a DIFFERENT user who owns an open session of the receipts'
  branch accepts it when the count is EXACT (one Cash movement ``credit_field_rendition`` through the Cash port), or
  rejects it (reason; releases the receipts); the custodian may cancel it while declared (releases the receipts).
* Outstanding custody is derived: a receipt without a live item of an accepted rendition.
* Lock order: declare = receipts ascending id (FOR UPDATE), never loans; decisions = the rendition row, then (accept) the
  Cash port's box and session. Nothing here touches payments, applications, reversals, obligations or loans.
"""

import base64
import hashlib
import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflict
from app.core.time import now_utc
from app.models.branch import Branch
from app.modules.cash import port as cash_port
from app.modules.credit.rules import canonical_json
from app.modules.customers.service import visible_branch_ids
from app.modules.field_custody.errors import (
    CustodyBranchMismatch,
    CustodyReceiptNotFound,
    InvalidCustodyAmount,
    MakerCheckerViolation,
    NotCustodian,
    ReceiptAlreadyClaimed,
    RenditionCountMismatch,
    RenditionNotDeclared,
    RenditionNotFound,
)
from app.modules.field_custody.models import (
    CreditFieldCustodyReceipt as Receipt,
)
from app.modules.field_custody.models import (
    CreditFieldRendition as Rendition,
)
from app.modules.field_custody.models import (
    CreditFieldRenditionItem as Item,
)
from app.modules.field_custody.receipts import require_active_user
from app.modules.field_custody.schemas import AcceptIn, CancelIn, DeclareIn, RejectIn
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, require
from app.modules.identity.errors import PermissionDenied, TenantMismatch
from app.modules.loans.errors import InvalidCursor
from app.modules.loans.models import CreditPayment
from app.modules.loans.payments import _gate_tenant
from app.modules.origination.service import _amt, _next_number

READ, RENDER, ACCEPT = "cash.field_custody.read", "cash.field_custody.render", "cash.field_custody.accept"
CENT = Decimal("0.01")


# --- helpers --------------------------------------------------------------------------------------------
def _digest(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _cents(raw: str | None, *, allow_zero: bool) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise InvalidCustodyAmount() from None
    if value < 0 or (value == 0 and not allow_zero) or value != value.quantize(CENT):
        raise InvalidCustodyAmount()
    return value


def _branch(db: Session, actor: Principal, branch_id: int) -> Branch:
    branch = db.get(Branch, branch_id)
    if branch is None or branch.company_id != actor.tenant_id:
        raise TenantMismatch()  # another tenant's branch is a 404
    return branch


def _can_read(actor: Principal, branch_id: int, custodian_user_id: int) -> bool:
    """Branch readers see the branch; a custodian sees ONLY their own custody (never widening branch access)."""
    if actor.allows(READ, tenant_id=actor.tenant_id, branch_id=branch_id):
        return True
    return custodian_user_id == actor.user_id and actor.allows(RENDER, tenant_id=actor.tenant_id, branch_id=branch_id)


def _item_rows(db: Session, rendition_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {i: [] for i in rendition_ids}
    if not rendition_ids:
        return out
    rows = db.execute(
        select(Item.rendition_id, Item.receipt_id, Item.payment_id, Item.amount, Item.released)
        .where(Item.rendition_id.in_(rendition_ids))
        .order_by(Item.rendition_id, Item.payment_id)
    )
    for r in rows:
        out[r.rendition_id].append(
            {"receipt_id": r.receipt_id, "payment_id": r.payment_id, "amount": _amt(r.amount), "released": r.released}
        )
    return out


def _rendition_out(r: Rendition, items: list[dict]) -> dict:
    """Ids, amounts, states and timestamps only: no keys, digests or names."""
    return {
        "id": r.id,
        "rendition_number": r.rendition_number,
        "receiving_branch_id": r.receiving_branch_id,
        "custodian_user_id": r.custodian_user_id,
        "currency_code": r.currency_code,
        "declared_amount": _amt(r.declared_amount),
        "state": r.state,
        "declared_at": r.declared_at,
        "decided_by": r.decided_by,
        "decided_at": r.decided_at,
        "decision_reason": r.decision_reason,
        "counted_amount": _amt(r.counted_amount),
        "cash_session_id": r.cash_session_id,
        "cash_movement_id": r.cash_movement_id,
        "items": items,
    }


def _out(db: Session, r: Rendition, *, replayed: bool | None = None) -> dict:
    out = _rendition_out(r, _item_rows(db, [r.id])[r.id])
    if replayed is not None:
        out["replayed"] = replayed
    return out


def _audit(db: Session, event: str, actor: Principal, r: Rendition, client_ip: str | None, **extra) -> None:
    record_event(
        db,
        event,
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        subject_id=r.custodian_user_id,
        client_ip=client_ip,
        details={
            "rendition_id": r.id,
            "rendition_number": r.rendition_number,
            "receiving_branch_id": r.receiving_branch_id,
            "custodian_user_id": r.custodian_user_id,
            "declared_amount": _amt(r.declared_amount),
            "currency_code": r.currency_code,
            "state": r.state,
            **extra,
        },
    )


# --- declare -------------------------------------------------------------------------------------------
def declare(db: Session, actor: Principal, body: DeclareIn, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    _branch(db, actor, body.receiving_branch_id)
    require(actor, RENDER, tenant_id=actor.tenant_id, branch_id=body.receiving_branch_id)
    payment_ids = sorted(body.payment_ids)
    digest = _digest(
        {
            "operation": "declare_field_rendition",
            "custodian_user_id": actor.user_id,
            "receiving_branch_id": body.receiving_branch_id,
            "payment_ids": payment_ids,
        }
    )
    prior = db.scalar(
        select(Rendition).where(
            Rendition.tenant_id == actor.tenant_id, Rendition.create_idempotency_key == body.idempotency_key
        )
    )
    if prior is not None:
        if prior.create_request_digest == digest:
            return _out(db, prior, replayed=True)
        raise IdempotencyConflict()
    require_active_user(db, actor.user_id)
    # 1. the receipts, ascending id (FOR UPDATE): two declarations of the same receipt serialise here. No loan lock.
    receipts = db.scalars(
        select(Receipt)
        .where(Receipt.tenant_id == actor.tenant_id, Receipt.payment_id.in_(payment_ids))
        .order_by(Receipt.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if len(receipts) != len(payment_ids):
        raise CustodyReceiptNotFound()  # counter, pre-T-019 or foreign payments have no receipt
    if any(r.custodian_user_id != actor.user_id for r in receipts):
        raise NotCustodian()
    if any(r.receiving_branch_id != body.receiving_branch_id for r in receipts):
        raise CustodyBranchMismatch()
    if db.scalar(select(Item.id).where(Item.receipt_id.in_([r.id for r in receipts]), Item.released.is_(False))):
        raise ReceiptAlreadyClaimed()  # in a declared (pending) or accepted (rendered) rendition
    rendition = Rendition(
        tenant_id=actor.tenant_id,
        rendition_number=_next_number(db, actor.tenant_id, "credit_field_rendition", "REN-"),
        receiving_branch_id=body.receiving_branch_id,
        custodian_user_id=actor.user_id,
        currency_code=receipts[0].currency_code,
        declared_amount=sum((r.amount for r in receipts), Decimal(0)),  # server-derived: the receipts' full amounts
        state="declared",
        declared_by=actor.user_id,
        declared_at=now_utc(),
        create_idempotency_key=body.idempotency_key,
        create_request_digest=digest,
    )
    db.add(rendition)
    try:
        db.flush()
        for r in receipts:
            db.add(
                Item(
                    tenant_id=actor.tenant_id,
                    rendition_id=rendition.id,
                    receipt_id=r.id,
                    payment_id=r.payment_id,
                    receiving_branch_id=r.receiving_branch_id,
                    custodian_user_id=r.custodian_user_id,
                    currency_code=r.currency_code,
                    amount=r.amount,  # indivisible: never a client amount
                    released=False,
                )
            )
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise (IdempotencyConflict() if "create_key" in str(exc.orig) else ReceiptAlreadyClaimed()) from None
    _audit(db, "field_custody.rendition_declared", actor, rendition, client_ip, payment_ids=payment_ids)
    db.commit()
    return _out(db, rendition, replayed=False)


# --- decisions (accept / reject / cancel): one terminal transition --------------------------------------
def _locked_rendition(db: Session, actor: Principal, rendition_id: int) -> Rendition:
    _gate_tenant(actor)
    r = db.scalar(
        select(Rendition)
        .where(Rendition.id == rendition_id, Rendition.tenant_id == actor.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if r is None:
        raise RenditionNotFound()
    return r


def _decision_replay(db: Session, actor: Principal, r: Rendition, key: str, digest: str) -> dict | None:
    """Same key + same content on this rendition: the stored answer. Any other use of the key: 409."""
    if r.decision_idempotency_key == key:
        if r.decision_request_digest == digest:
            return _out(db, r, replayed=True)
        raise IdempotencyConflict()
    if db.scalar(
        select(Rendition.id).where(Rendition.tenant_id == actor.tenant_id, Rendition.decision_idempotency_key == key)
    ):
        raise IdempotencyConflict()  # the key already decided another rendition
    if r.state != "declared":
        raise RenditionNotDeclared()
    return None


def _release(db: Session, r: Rendition) -> None:
    db.execute(update(Item).where(Item.rendition_id == r.id, Item.released.is_(False)).values(released=True))


def accept(db: Session, actor: Principal, rendition_id: int, body: AcceptIn, client_ip: str | None) -> dict:
    r = _locked_rendition(db, actor, rendition_id)  # 1. the rendition row
    require(actor, ACCEPT, tenant_id=actor.tenant_id, branch_id=r.receiving_branch_id)
    counted = _cents(body.counted_amount, allow_zero=True)
    digest = _digest(
        {
            "operation": "accept_field_rendition",
            "rendition_id": r.id,
            "cash_session_id": body.cash_session_id,
            "counted_amount": format(counted, "f"),
        }
    )
    replay = _decision_replay(db, actor, r, body.idempotency_key, digest)
    if replay is not None:
        return replay
    if actor.user_id == r.custodian_user_id:
        raise MakerCheckerViolation()
    if counted != r.declared_amount:
        raise RenditionCountMismatch()  # v1: exact only; nothing moves
    # 2-3. Cash port: box then session, explicit session owned by the acceptor, fixed kind; never commits
    movement = cash_port.deposit_field_rendition(
        db,
        tenant_id=actor.tenant_id,
        branch_id=r.receiving_branch_id,
        session_id=body.cash_session_id,
        amount=r.declared_amount,
        currency=r.currency_code,
        cashier_user_id=actor.user_id,
        reference=r.rendition_number,
        notes=f"Rendicion de campo {r.rendition_number}",
    )
    r.state = "accepted"
    r.decided_by, r.decided_at, r.counted_amount = actor.user_id, now_utc(), counted
    r.cash_session_id, r.cash_movement_id = movement.session_id, movement.movement_id
    r.decision_idempotency_key, r.decision_request_digest = body.idempotency_key, digest
    db.flush()
    _audit(
        db,
        "field_custody.rendition_accepted",
        actor,
        r,
        client_ip,
        cash_session_id=movement.session_id,
        cash_movement_id=movement.movement_id,
        counted_amount=_amt(counted),
    )
    db.commit()
    return _out(db, r, replayed=False)


def reject(db: Session, actor: Principal, rendition_id: int, body: RejectIn, client_ip: str | None) -> dict:
    r = _locked_rendition(db, actor, rendition_id)
    require(actor, ACCEPT, tenant_id=actor.tenant_id, branch_id=r.receiving_branch_id)
    counted = _cents(body.counted_amount, allow_zero=True)
    digest = _digest(
        {
            "operation": "reject_field_rendition",
            "rendition_id": r.id,
            "reason": body.reason,
            "counted_amount": format(counted, "f") if counted is not None else None,
        }
    )
    replay = _decision_replay(db, actor, r, body.idempotency_key, digest)
    if replay is not None:
        return replay
    if actor.user_id == r.custodian_user_id:
        raise MakerCheckerViolation()
    r.state = "rejected"
    r.decided_by, r.decided_at, r.counted_amount, r.decision_reason = actor.user_id, now_utc(), counted, body.reason
    r.decision_idempotency_key, r.decision_request_digest = body.idempotency_key, digest
    db.flush()
    _release(db, r)  # the receipts are outstanding again (the cash stays with the custodian); nothing is booked
    _audit(db, "field_custody.rendition_rejected", actor, r, client_ip, counted_amount=_amt(counted))
    db.commit()
    return _out(db, r, replayed=False)


def cancel(db: Session, actor: Principal, rendition_id: int, body: CancelIn, client_ip: str | None) -> dict:
    r = _locked_rendition(db, actor, rendition_id)
    require(actor, RENDER, tenant_id=actor.tenant_id, branch_id=r.receiving_branch_id)
    digest = _digest({"operation": "cancel_field_rendition", "rendition_id": r.id})
    replay = _decision_replay(db, actor, r, body.idempotency_key, digest)
    if replay is not None:
        return replay
    if actor.user_id != r.custodian_user_id:
        raise NotCustodian()
    r.state = "cancelled"
    r.decided_by, r.decided_at = actor.user_id, now_utc()
    r.decision_idempotency_key, r.decision_request_digest = body.idempotency_key, digest
    db.flush()
    _release(db, r)
    _audit(db, "field_custody.rendition_cancelled", actor, r, client_ip)
    db.commit()
    return _out(db, r, replayed=False)


# --- user lifecycle guard -------------------------------------------------------------------------------
def has_open_custody(db: Session, tenant_id: int, user_id: int) -> bool:
    """Outstanding custody (a receipt not in an accepted rendition) or a declared rendition of this custodian. ONE query."""
    accepted_live = exists().where(
        Item.receipt_id == Receipt.id,
        Item.released.is_(False),
        Item.rendition_id == Rendition.id,
        Rendition.state == "accepted",
    )
    outstanding = exists().where(Receipt.tenant_id == tenant_id, Receipt.custodian_user_id == user_id, ~accepted_live)
    declared = exists().where(
        Rendition.tenant_id == tenant_id, Rendition.custodian_user_id == user_id, Rendition.state == "declared"
    )
    return bool(db.scalar(select(or_(outstanding, declared))))


# --- reads (pure) ---------------------------------------------------------------------------------------
def _encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"i": last_id}, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
        if (
            not isinstance(raw, dict)
            or set(raw) != {"i"}
            or isinstance(raw["i"], bool)
            or not isinstance(raw["i"], int)
        ):
            raise ValueError
        if raw["i"] <= 0:
            raise ValueError
        return raw["i"]
    except (ValueError, TypeError, UnicodeDecodeError):
        raise InvalidCursor() from None


def _scope_filter(actor: Principal, branch_column, custodian_column):
    """Rows the actor may read: branches with READ, plus their OWN custody where they hold RENDER (never others')."""
    try:
        read_scope = visible_branch_ids(actor, READ)
    except PermissionDenied:
        read_scope = set()
    try:
        render_scope = visible_branch_ids(actor, RENDER)
    except PermissionDenied:
        render_scope = set()
    if read_scope is None:
        return None  # tenant-wide reader
    own = custodian_column == actor.user_id
    own_cond = own if render_scope is None else and_(own, branch_column.in_(render_scope))
    conds = []
    if read_scope:
        conds.append(branch_column.in_(read_scope))
    if render_scope is None or render_scope:
        conds.append(own_cond)
    if not conds:
        raise PermissionDenied()
    return or_(*conds)


def _live_join():
    """Receipt -> its (at most one, partial UNIQUE) live item -> that item's rendition."""
    return (
        select(Receipt, Item.rendition_id, Rendition.state.label("rendition_state"), Rendition.rendition_number)
        .outerjoin(Item, and_(Item.receipt_id == Receipt.id, Item.released.is_(False)))
        .outerjoin(Rendition, Rendition.id == Item.rendition_id)
    )


def list_custody(
    db: Session,
    actor: Principal,
    *,
    receiving_branch_id: int | None,
    custodian_user_id: int | None,
    limit: int,
    cursor: str | None,
) -> dict:
    """OUTSTANDING custody (not accepted), ascending receipt id, keyset. A declared receipt is still the custodian's cash
    (``pending_rendition_id`` set). Inactive custodians are never hidden. ONE query."""
    _gate_tenant(actor)
    scope = _scope_filter(actor, Receipt.receiving_branch_id, Receipt.custodian_user_id)
    stmt = _live_join().where(Receipt.tenant_id == actor.tenant_id, Rendition.state.is_distinct_from("accepted"))
    if scope is not None:
        stmt = stmt.where(scope)
    if receiving_branch_id is not None:
        stmt = stmt.where(Receipt.receiving_branch_id == receiving_branch_id)
    if custodian_user_id is not None:
        stmt = stmt.where(Receipt.custodian_user_id == custodian_user_id)
    if cursor:
        stmt = stmt.where(Receipt.id > _decode_cursor(cursor))
    rows = db.execute(stmt.order_by(Receipt.id).limit(limit + 1)).all()
    page, more = rows[:limit], len(rows) > limit
    return {
        "items": [
            {
                "receipt_id": r.CreditFieldCustodyReceipt.id,
                "payment_id": r.CreditFieldCustodyReceipt.payment_id,
                "loan_id": r.CreditFieldCustodyReceipt.loan_id,
                "receiving_branch_id": r.CreditFieldCustodyReceipt.receiving_branch_id,
                "custodian_user_id": r.CreditFieldCustodyReceipt.custodian_user_id,
                "currency_code": r.CreditFieldCustodyReceipt.currency_code,
                "amount": _amt(r.CreditFieldCustodyReceipt.amount),
                "received_at": r.CreditFieldCustodyReceipt.received_at,
                "pending_rendition_id": r.rendition_id,  # declared: still the custodian's cash, in handoff
            }
            for r in page
        ],
        "next_cursor": _encode_cursor(page[-1].CreditFieldCustodyReceipt.id) if more and page else None,
        "limit": limit,
    }


def custody_summary(db: Session, actor: Principal, *, receiving_branch_id: int) -> dict:
    """Per custodian of ONE branch: outstanding receipts / amount, of which pending (declared). ONE query."""
    _gate_tenant(actor)
    _branch(db, actor, receiving_branch_id)
    require(actor, READ, tenant_id=actor.tenant_id, branch_id=receiving_branch_id)
    pending = Rendition.state == "declared"
    stmt = (
        _live_join()
        .with_only_columns(
            Receipt.custodian_user_id,
            func.count(Receipt.id),
            func.sum(Receipt.amount),
            func.count(Receipt.id).filter(pending),
            func.coalesce(func.sum(Receipt.amount).filter(pending), 0),
        )
        .where(
            Receipt.tenant_id == actor.tenant_id,
            Receipt.receiving_branch_id == receiving_branch_id,
            Rendition.state.is_distinct_from("accepted"),
        )
        .group_by(Receipt.custodian_user_id)
        .order_by(Receipt.custodian_user_id)
    )
    return {
        "receiving_branch_id": receiving_branch_id,
        "currency_code": cash_port.CASH_CURRENCY,
        "custodians": [
            {
                "custodian_user_id": c,
                "outstanding_receipts": n,
                "outstanding_amount": _amt(total),
                "pending_receipts": pn,
                "pending_amount": _amt(Decimal(pt)),
            }
            for c, n, total, pn, pt in db.execute(stmt)
        ],
    }


def list_renditions(
    db: Session, actor: Principal, *, receiving_branch_id: int | None, state: str | None, limit: int, cursor: str | None
) -> dict:
    """Renditions visible to the actor, ascending id, keyset. Two queries (headers, then the page's items)."""
    _gate_tenant(actor)
    scope = _scope_filter(actor, Rendition.receiving_branch_id, Rendition.custodian_user_id)
    stmt = select(Rendition).where(Rendition.tenant_id == actor.tenant_id)
    if scope is not None:
        stmt = stmt.where(scope)
    if receiving_branch_id is not None:
        stmt = stmt.where(Rendition.receiving_branch_id == receiving_branch_id)
    if state is not None:
        stmt = stmt.where(Rendition.state == state)
    if cursor:
        stmt = stmt.where(Rendition.id > _decode_cursor(cursor))
    rows = db.scalars(stmt.order_by(Rendition.id).limit(limit + 1)).all()
    page, more = rows[:limit], len(rows) > limit
    items = _item_rows(db, [r.id for r in page])
    return {
        "items": [_rendition_out(r, items[r.id]) for r in page],
        "next_cursor": _encode_cursor(page[-1].id) if more and page else None,
        "limit": limit,
    }


def get_rendition(db: Session, actor: Principal, rendition_id: int) -> dict:
    _gate_tenant(actor)
    r = db.scalar(select(Rendition).where(Rendition.id == rendition_id, Rendition.tenant_id == actor.tenant_id))
    if r is None:
        raise RenditionNotFound()
    if not _can_read(actor, r.receiving_branch_id, r.custodian_user_id):
        raise PermissionDenied()
    return _out(db, r)


def payment_custody(db: Session, actor: Principal, payment_id: int) -> dict:
    """The physical custody of ONE payment. A counter payment is ``not_applicable``; a field payment born before T-019 is
    ``pre_custody`` (no custodian, no outstanding or rendered claim is made). Otherwise outstanding / pending_rendition /
    rendered, with every rendition the receipt was ever in."""
    _gate_tenant(actor)
    p = db.scalar(
        select(CreditPayment).where(CreditPayment.id == payment_id, CreditPayment.tenant_id == actor.tenant_id)
    )
    if p is None:
        raise TenantMismatch()
    receipt = db.scalar(select(Receipt).where(Receipt.payment_id == p.id))
    custodian = receipt.custodian_user_id if receipt else p.collected_by
    if not _can_read(actor, p.receiving_branch_id, custodian):
        raise PermissionDenied()
    base = {"payment_id": p.id, "origin": p.origin, "receiving_branch_id": p.receiving_branch_id}
    if p.origin != "field":
        return {**base, "tracking_status": "not_applicable"}  # counter: the cash entered a session when collected
    if receipt is None:
        return {**base, "tracking_status": "pre_custody"}  # recorded before T-019: never backfilled
    history = db.execute(
        select(Rendition.id, Rendition.rendition_number, Rendition.state, Rendition.cash_session_id, Item.released)
        .join(Item, Item.rendition_id == Rendition.id)
        .where(Item.receipt_id == receipt.id)
        .order_by(Rendition.id)
    ).all()
    live = next((h for h in history if not h.released), None)
    status = "outstanding" if live is None else ("rendered" if live.state == "accepted" else "pending_rendition")
    return {
        **base,
        "tracking_status": status,
        "receipt_id": receipt.id,
        "custodian_user_id": receipt.custodian_user_id,
        "currency_code": receipt.currency_code,
        "amount": _amt(receipt.amount),
        "renditions": [
            {
                "rendition_id": h.id,
                "rendition_number": h.rendition_number,
                "state": h.state,
                "cash_session_id": h.cash_session_id,
                "released": h.released,
            }
            for h in history
        ],
    }
