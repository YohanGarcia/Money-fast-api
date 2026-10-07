"""Cash port (T-007): the ONLY place where Credit touches the cash custody tables.

Credit asks, Cash moves the money and answers with a confirmation (movement id). Today Cash is the legacy runtime
(one ``CashBox`` per branch, ``CashSession`` custody, RD$ only, mutable session balance); T-008 rebuilds it and only this
adapter changes. Nothing here decides *whether* a loan may be disbursed: that belongs to Credit.

The adapter writes inside the caller's transaction and never commits: money and loan succeed or fail together.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.cash import CashAudit, CashBox, CashConfig, CashMovement, CashSession

CASH_CURRENCY = "DOP"  # the legacy cash ledger has no currency column: every amount is RD$ (T-008 will change this)
CENT = Decimal("0.01")


class CashUnavailable(AppError):
    status_code, code = 409, "cash_unavailable"
    default_message = "La caja no esta disponible para esta operacion."


class CashCurrencyUnsupported(AppError):
    status_code, code = 409, "cash_currency_unsupported"
    default_message = "BLOCKED_BY_EVIDENCE: la caja actual opera solo en RD$ (DOP); no hay soporte de otras monedas hasta el paquete de Caja."


class CashSessionNotOwned(AppError):
    status_code, code = 403, "cash_session_not_owned"
    default_message = "La jornada indicada no pertenece al usuario que ejecuta la operacion."


class InsufficientCash(AppError):
    status_code, code = 409, "insufficient_cash"
    default_message = "El efectivo disponible en la jornada no alcanza para esta salida."


@dataclass(frozen=True)
class CashWithdrawal:
    movement_id: int
    box_id: int
    session_id: int
    amount: Decimal
    balance_after: Decimal


def _open_custody(
    db: Session,
    *,
    tenant_id: int,
    branch_id: int,
    session_id: int,
    amount: Decimal,
    currency: str,
    cashier_id: int | None = None,
) -> tuple[CashBox, CashSession]:
    """Validate and lock (box, then session: the legacy order) the open custody session of the branch's box."""
    if currency != CASH_CURRENCY:
        raise CashCurrencyUnsupported()
    if db.get(CashConfig, tenant_id) is None:
        raise CashUnavailable("Caja no esta habilitada en esta agencia.")
    if amount <= 0 or amount != amount.quantize(CENT):
        raise CashUnavailable("El monto debe ser positivo y expresarse en centavos.")
    box = db.scalar(
        select(CashBox).where(CashBox.company_id == tenant_id, CashBox.branch_id == branch_id).with_for_update()
    )
    if box is None:
        raise CashUnavailable("La sucursal indicada no tiene caja configurada.")
    session = db.scalar(
        select(CashSession).where(CashSession.id == session_id, CashSession.box_id == box.id).with_for_update()
    )
    if session is None or session.state != "open":
        raise CashUnavailable("La jornada indicada no esta abierta en la caja de la sucursal indicada.")
    if cashier_id is not None and session.cashier_id != cashier_id:
        raise CashSessionNotOwned()  # the cash physically leaves the drawer of the person executing the operation
    return box, session


def _record(
    db: Session,
    box: CashBox,
    session: CashSession,
    *,
    signed: Decimal,
    actor_user_id: int,
    kind: str,
    reference: str,
    notes: str,
    reverses_id: int | None = None,
) -> CashWithdrawal:
    session.balance += signed
    session.version += 1
    movement = CashMovement(
        box_id=box.id,
        session_id=session.id,
        kind=kind,
        amount=signed,
        actor_id=actor_user_id,
        notes=notes,
        reference=reference,
        reverses_id=reverses_id,
    )
    db.add(movement)
    db.flush()
    db.add(
        CashAudit(
            box_id=box.id,
            actor_id=actor_user_id,
            action=kind,
            details={
                "movement_id": movement.id,
                "session_id": session.id,
                "amount": str(signed),
                "reference": reference,
            },
        )
    )
    return CashWithdrawal(movement.id, box.id, session.id, abs(signed), session.balance)


def withdraw(
    db: Session,
    *,
    tenant_id: int,
    branch_id: int,
    session_id: int,
    amount: Decimal,
    currency: str,
    actor_user_id: int,
    kind: str,
    reference: str,
    notes: str,
    reverses_id: int | None = None,
    require_cashier_id: int | None = None,
) -> CashWithdrawal:
    """Cash OUT of one open custody session of the branch.

    ``reverses_id`` links a compensating movement to the one it compensates (T-009); ``require_cashier_id`` demands that
    the session belongs to that user. Never commits."""
    box, session = _open_custody(
        db,
        tenant_id=tenant_id,
        branch_id=branch_id,
        session_id=session_id,
        amount=amount,
        currency=currency,
        cashier_id=require_cashier_id,
    )
    if session.balance - amount < 0:
        raise InsufficientCash()
    return _record(
        db,
        box,
        session,
        signed=-amount,
        actor_user_id=actor_user_id,
        kind=kind,
        reference=reference,
        notes=notes,
        reverses_id=reverses_id,
    )


FIELD_RENDITION_KIND = (
    "credit_field_rendition"  # T-019; NOT 'delivery' (legacy): the legacy cash reversal never sees it
)


def deposit_field_rendition(
    db: Session,
    *,
    tenant_id: int,
    branch_id: int,
    session_id: int,
    amount: Decimal,
    currency: str,
    cashier_user_id: int,
    reference: str,
    notes: str,
) -> CashWithdrawal:
    """Cash IN of an accepted field rendition (T-019): the collector's physical cash enters the drawer of the cashier who
    counted it. The session is explicit, must be open, belong to the branch's box AND to ``cashier_user_id``; the kind is
    fixed. Locks box then session (port order). Never commits."""
    box, session = _open_custody(
        db,
        tenant_id=tenant_id,
        branch_id=branch_id,
        session_id=session_id,
        amount=amount,
        currency=currency,
        cashier_id=cashier_user_id,
    )
    return _record(
        db,
        box,
        session,
        signed=amount,
        actor_user_id=cashier_user_id,
        kind=FIELD_RENDITION_KIND,
        reference=reference,
        notes=notes,
    )


def deposit(
    db: Session,
    *,
    tenant_id: int,
    branch_id: int,
    session_id: int,
    amount: Decimal,
    currency: str,
    actor_user_id: int,
    kind: str,
    reference: str,
    notes: str,
) -> CashWithdrawal:
    """Cash IN to one open custody session of the branch (e.g. a loan payment received at the counter).

    ``kind`` must be a kind the legacy cash reversal does not accept (``credit_payment_receipt``): reversing only the cash
    would leave the debt applied. Never commits."""
    box, session = _open_custody(
        db, tenant_id=tenant_id, branch_id=branch_id, session_id=session_id, amount=amount, currency=currency
    )
    return _record(
        db, box, session, signed=amount, actor_user_id=actor_user_id, kind=kind, reference=reference, notes=notes
    )
