from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
import hashlib
import json
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, update, func
from app.models.cash import (CashConfig, CashBox, CashSession, CashCustodyTransfer, CashMovement, CashDelivery, CashAllocation, CashTransfer, CashAudit, CashRequest)
from app.models.bank_account import BankAccount
from app.models.capital import CapitalMovement
from app.models.branch import Branch
from app.models.user import User
from app.models.customer import Customer
from app.models.route import Route
from app.models.loan import Loan
from app.models.payment import Payment
from app.models.company import Company
from app.modules.cash import sessions as cash_core
from app.modules.cash.errors import DifferenceReviewNotAvailable, OnBehalfOpeningForbidden
from app.modules.identity.authorization import build_principal

ZERO = Decimal('0.00')
TZ = ZoneInfo('America/Santo_Domingo')
DENOMINATIONS = {'2000','1000','500','200','100','50','25','10','5','1','0.50','0.25','0.10','0.05','0.01'}

def now(): return datetime.now(UTC)
def today(): return now().astimezone(TZ).date()
def enabled(db, company_id): return db.get(CashConfig, company_id) is not None
def lock_company(db, company_id):
    db.execute(update(Company).where(Company.id==company_id).values(name=Company.name))
    db.expire_all()
def fail(message, code=422): raise HTTPException(code, message)
def require_role(user, *roles):
    if user.role not in roles: fail('No tienes permiso para esta operación de caja.', 403)

def scope(db, user, branch_id=None, lock=False):
    require_role(user, 'admin','cashier','collector','manager')
    if not enabled(db, user.company_id): fail('Caja no está habilitada en esta empresa.',409)
    bid = branch_id if user.role == 'admin' else user.branch_id
    if not bid or (user.role != 'admin' and branch_id is not None and branch_id != bid):
        fail('Se requiere tu sucursal asignada; no puedes operar otra caja.',403)
    box = db.scalar(select(CashBox).where(CashBox.company_id==user.company_id, CashBox.branch_id==bid))
    if not box: fail('La sucursal no tiene caja configurada.',404)
    branch = db.get(Branch,bid)
    if lock:
        if not branch.is_active: fail('La sucursal está inactiva.',409)
        # A write lock serializes the entire branch transaction, including first opening.
        db.execute(update(CashBox).where(CashBox.id==box.id).values(version=CashBox.version+1))
        db.flush()
        db.expire_all()
        box = db.get(CashBox,box.id)
        if not user.is_active or (user.role!='admin' and user.branch_id!=box.branch_id):
            fail('Tu asignación cambió. Inicia sesión de nuevo antes de operar.',409)
    return box

def check_version(row, version):
    if version is None or version != row.version: fail('Los datos cambiaron. Actualiza antes de guardar.',409)
    row.version += 1

def active_session(db, box, required=True, cashier_id=None):
    statement = select(CashSession).where(
        CashSession.box_id==box.id,
        CashSession.state.in_(['open','closing']),  # T-021 lifecycle: open -> closing -> closed
    )
    if cashier_id is not None:
        statement = statement.where(CashSession.cashier_id == cashier_id)
    row = db.scalar(statement.order_by(CashSession.id.desc()))
    if required and (not row or row.state!='open'):
        fail('Debes abrir caja y resolver sus diferencias antes de operar.',409)
    return row

def operational_session(db, box, user, session_id=None):
    """Select the exact open custody for money-changing operations.

    An administrator must identify the session when multiple cashiers work
    concurrently. Choosing the latest session silently can move another
    cashier's money.
    """
    statement = select(CashSession).where(CashSession.box_id == box.id, CashSession.state == 'open')
    if user.role == 'cashier':
        statement = statement.where(CashSession.cashier_id == user.id)
    if session_id is not None:
        row = db.scalar(statement.where(CashSession.id == session_id))
        if row is None:
            fail('La jornada seleccionada no está abierta o no te pertenece.', 409)
        return row
    rows = db.scalars(statement.order_by(CashSession.id.desc()).limit(2)).all()
    if not rows:
        fail('Debes abrir caja y resolver sus diferencias antes de operar.', 409)
    if len(rows) > 1:
        fail('Hay varias jornadas abiertas. Selecciona explícitamente session_id.', 409)
    return rows[0]

def audit(db, box, user, action, **details):
    db.info.setdefault('cash_notifications', set()).add((box.company_id, box.branch_id))
    db.add(CashAudit(box_id=box.id,actor_id=user.id,action=action,details=jsonable_encoder(details)))

def replay(db,user,key,payload):
    if not key or len(key)<12: fail('Actualiza la app: se requiere clave de operación.')
    digest=hashlib.sha256(json.dumps(jsonable_encoder(payload),sort_keys=True).encode()).hexdigest()
    old=db.scalar(select(CashRequest).where(CashRequest.company_id==user.company_id,CashRequest.actor_id==user.id,CashRequest.key==key))
    if old and old.digest!=digest: fail('La clave de operación pertenece a otro contenido.',409)
    return (old.result if old else None), digest

def remember(db,user,key,digest,result):
    result=jsonable_encoder(result)
    db.add(CashRequest(company_id=user.company_id,actor_id=user.id,key=key,digest=digest,result=result))
    return result

def outstanding_rows(db,box,collector_id=None):
    query=select(Payment).where(Payment.branch_id==box.branch_id,Payment.method=='cash',Payment.origin=='field',Payment.cash_state=='confirmed').order_by(Payment.paid_at,Payment.id)
    if collector_id is not None: query=query.where(Payment.collected_by_id==collector_id)
    rows=[]
    for p in db.scalars(query).all():
        delivered=db.scalar(select(func.coalesce(func.sum(CashAllocation.amount),0)).where(CashAllocation.payment_id==p.id))
        remaining=p.amount-delivered
        if remaining>0: rows.append((p,remaining))
    return rows

def pending(db,box,collector_id=None): return sum((n for _,n in outstanding_rows(db,box,collector_id)),ZERO)

def add_movement(db,box,session,user,kind,amount,notes,**kw):
    if session.state!='open': fail('La jornada no admite movimientos.',409)
    if session.balance+amount<0: fail('El efectivo disponible no alcanza para esta salida.',409)
    session.balance += amount
    session.version += 1
    row=CashMovement(box_id=box.id,session_id=session.id,actor_id=user.id,kind=kind,amount=amount,notes=notes,**kw)
    db.add(row); db.flush()
    return row


def _require_acceptance(p):
    if not p.acceptance_id or p.acceptance_method != 'authenticated_confirmation':
        fail('La entrega requiere una aceptación autenticada e identificable.')


def _legacy_result(out, **extra):
    """T-021 session answer in the legacy command shape (plus the full session and the action that completes it)."""
    handover = out.get('handover') or {}
    return dict(session_id=out['id'], state=out['state'], cash_point_id=out['cash_point_id'],
                transfer_id=handover.get('id'), difference=out.get('difference'),
                next_action=out.get('next_action'), session=out, **extra)

def branch_for_customer(db, c):
    if c.cash_branch_id: return c.cash_branch_id
    if c.route_id:
        r=db.get(Route,c.route_id)
        if r and r.branch_id: return r.branch_id
    if c.assigned_collector_id:
        u=db.get(User,c.assigned_collector_id)
        if u: return u.branch_id
    return None

def customer_ids(db,user):
    if not user.branch_id: return []
    return [c.id for c in db.scalars(select(Customer).where(Customer.company_id==user.company_id)).all() if branch_for_customer(db,c)==user.branch_id]

def ensure_customer(db,user,loan,box):
    c=db.get(Customer,loan.customer_id)
    if not c or c.company_id!=user.company_id: fail('Préstamo no encontrado.',404)
    if user.role=='collector' and c.assigned_collector_id!=user.id: fail('Préstamo no asignado.',404)
    bid=branch_for_customer(db,c)
    if bid is not None and bid!=box.branch_id: fail('El cliente pertenece a otra sucursal.',403)
    if bid is None and user.role!='admin': fail('El administrador debe asignar la sucursal del cliente.',409)
    c.cash_branch_id=box.branch_id

def apply_exact_payment(db,payload,actor_id):
    from app.services.payment_service import apply_payment
    payment=apply_payment(db,payload,actor_id)
    if payment.amount != payment.principal_applied+payment.interest_applied+payment.late_fee_applied:
        fail('El importe supera el saldo aplicable. Revisa el saldo y registra el importe exacto.')
    return payment

def register_payment(db,user,payload):
    from app.schemas.payment import PaymentCreate,PaymentRead
    from app.services.payment_service import apply_payment
    require_role(user,'admin','cashier','collector')
    if not payload.method or not payload.origin: fail('Actualiza la app: selecciona medio de pago y origen.')
    box=scope(db,user,payload.branch_id,lock=True)
    old,digest=replay(db,user,payload.idempotency_key,payload.model_dump(mode='json'))
    if old: return old
    if user.role=='collector' and payload.origin!='field': fail('El cobrador registra cobros de campo.',403)
    if payload.origin=='counter' and user.role!='cashier': fail('Los cobros de ventanilla pertenecen a la caja del cajero.',403)
    if user.role=='cashier' and payload.origin!='counter': fail('El cajero registra cobros de ventanilla.',403)
    loan=db.get(Loan,payload.loan_id)
    if not loan: fail('Préstamo no encontrado.',404)
    ensure_customer(db,user,loan,box)
    # An explicit component breakdown is also an exact amount: preserve composed payments.
    payment_amount = payload.amount if payload.amount is not None else (
        payload.installment_amount + payload.principal_amount + payload.interest_amount + payload.custom_amount
    )
    if payment_amount <= ZERO or payment_amount > Decimal('9999999999.99'):
        fail('El importe total debe ser positivo y no superar el límite monetario.')
    session=operational_session(db,box,user,payload.session_id) if payload.origin=='counter' else None
    if payload.method=='transfer':
        if not payload.reference_code or not payload.bank_account_id or not payload.proof: fail('La transferencia requiere referencia, cuenta bancaria de destino y comprobante.')
        account=db.get(BankAccount,payload.bank_account_id)
        if not account or account.company_id!=user.company_id or not account.is_active: fail('Selecciona una cuenta bancaria activa de la empresa.')
        row=CashTransfer(box_id=box.id,collector_id=user.id,loan_id=loan.id,amount=payment_amount,reference=payload.reference_code,destination=account.label,bank_account_id=account.id,proof=payload.proof.model_dump(),payload=payload.model_dump(mode='json'))
        db.add(row);db.flush()
        result=dict(transfer_id=row.id,status='pending',amount=str(row.amount),message='Transferencia pendiente de confirmación; aún no abona al préstamo.')
    else:
        p=apply_exact_payment(db,payload,user.id)
        p.branch_id=box.branch_id;p.method='cash';p.origin=payload.origin;p.cash_state='confirmed'
        db.flush()
        if session: add_movement(db,box,session,user,'counter_payment',p.amount,'Cobro en ventanilla',payment_id=p.id,collector_id=user.id,reference=payload.reference_code or '')
        result=PaymentRead.model_validate(p).model_dump(mode='json')
    audit(db,box,user,'payment_recorded',result=result)
    return remember(db,user,payload.idempotency_key,digest,result)

def setup(db,user,payload):
    require_role(user,'admin')
    b=db.get(Branch,payload.branch_id)
    if not b or b.company_id!=user.company_id or not b.is_active: fail('Sucursal no válida.',404)
    if db.scalar(select(CashBox).where(CashBox.branch_id==b.id)): fail('Esta caja ya está configurada.',409)
    row=CashBox(company_id=user.company_id,branch_id=b.id,initial_balance=payload.initial_balance)
    db.add(row); db.flush()
    # T-021: the database creates the box's base CashPoint with it (no box ever lacks its operational position)
    cash_point_id=cash_core.base_cash_point_id(db,row.id)
    audit(db,row,user,'configured',initial_balance=payload.initial_balance,notes=payload.notes,cash_point_id=cash_point_id)
    return dict(box_id=row.id,cash_point_id=cash_point_id)

def activate(db,user):
    require_role(user,'admin')
    lock_company(db,user.company_id)
    if enabled(db,user.company_id): return dict(enabled=True)
    branches=db.scalars(select(Branch).where(Branch.company_id==user.company_id,Branch.is_active==True)).all()
    boxes=db.scalars(select(CashBox).where(CashBox.company_id==user.company_id)).all()
    configured={b.branch_id for b in boxes}
    if not branches or any(b.id not in configured for b in branches): fail('Configura la caja y saldo inicial de cada sucursal activa.')
    db.add(CashConfig(company_id=user.company_id,activated_by=user.id))
    for box in boxes: audit(db,box,user,'activated',historical_payments_excluded=True)
    return dict(enabled=True)

_ACTION_LABELS={
    'counter':'Cobró en ventanilla','receive_collector':'Recibió efectivo de un cobrador','reject_delivery':'Rechazó una entrega de cobrador',
    'reject_addition':'Rechazó una solicitud de dinero','cash_addition':'Confirmó dinero que entra a la caja','cash_withdrawal':'Confirmó dinero que sale de la caja',
    'open':'Abrió la caja','close':'Cerró la caja','declare':'Declaró una entrega a caja','receive':'Recibió efectivo de un cobrador',
    'reverse':'Revirtió un movimiento de caja','confirm_surplus':'Confirmó un sobrante','reject_surplus':'Rechazó un sobrante',
    'report_surplus':'Reportó un sobrante','confirm_transfer':'Confirmó una transferencia','reject_transfer':'Rechazó una transferencia',
    'request_addition':'Solicitó dinero para una caja','cancel_addition':'Canceló una solicitud de dinero','disburse':'Desembolsó un préstamo',
    'movement':'Registró un movimiento de caja','withdrawal':'Retiró dinero de la caja','contribution':'Registró un aporte a la caja','expense':'Registró un gasto','bank_deposit':'Registró un depósito bancario',
    'confirm_addition':'Confirmó dinero que entra a la caja','confirm_withdrawal':'Confirmó dinero que sale de la caja',
}


def _audit_hint(action, kind=None):
    from app.core import audit_changes

    label=_ACTION_LABELS.get(str(kind)) if action=='movement' and kind else None
    audit_changes.hint(label or _ACTION_LABELS.get(str(action)) or f'Operación de caja ({action})', entity_type='cash')


def command(db,user,p):
    box=scope(db,user,p.branch_id,lock=True)
    old,digest=replay(db,user,p.idempotency_key,p.model_dump(mode='json',exclude_none=False,exclude={'opening_mode','counted'} if p.opening_mode is None and p.counted is None else set()))
    if old: return old
    action=p.action
    _audit_hint(action, getattr(p, 'kind', None))
    if action=='resolve':
        # T-021 (DR-007): the difference is a separate record; a closed session is never reopened nor adjusted here.
        raise DifferenceReviewNotAvailable()
    if action=='declare': require_role(user,'collector')
    elif action in ('reverse','confirm_surplus','reject_surplus','confirm_transfer','reject_transfer','request_addition','cancel_addition'): require_role(user,'admin')
    else: require_role(user,'cashier')
    # T-021: open / close ALSO need the explicit permissions (no role grants them implicitly)
    result={}
    actor=build_principal(db,user,0) if action in ('open','close') else None
    if action=='open':
        if p.target_id and p.target_id!=user.id: raise OnBehalfOpeningForbidden()
        cash_point_id=p.cash_point_id or cash_core.base_cash_point_id(db,box.id)
        point=cash_core._cash_point(db,user.company_id,cash_point_id) if cash_point_id else None
        if point is None or point.branch_id!=box.branch_id: fail('Selecciona una caja de esta sucursal.',404)
        if p.opening_mode=='initial':
            # FM-SIMPLE-CASH-010: a declared initial amount (zero allowed); it never touches Capital.
            if p.capital: fail('La apertura simple no usa capital.')
            out=cash_core.open_session(db,actor,cash_point_id=point.id,source='initial',amount=p.amount,
                denominations=None,observation_note=p.notes,idempotency_key=p.idempotency_key)
        else:
            # Capital no longer funds a session: the only cash that enters is the declared initial amount.
            if p.capital or p.amount>0:
                fail('La apertura con capital ya no existe: abre la caja indicando el monto inicial.')
            out=cash_core.open_session(db,actor,cash_point_id=point.id,source='zero',amount=p.amount,
                denominations=p.denominations,observation_note=p.notes,idempotency_key=p.idempotency_key)
        result=_legacy_result(out,opening_mode=out['opening_source'])

    elif action=='declare':
        reserved=db.scalar(select(func.coalesce(func.sum(CashDelivery.declared),0)).where(CashDelivery.box_id==box.id,CashDelivery.collector_id==user.id,CashDelivery.state=='pending'))
        if p.amount<=0 or p.amount>pending(db,box,user.id)-reserved: fail('La entrega supera el pendiente disponible o ya declarado.')
        row=CashDelivery(box_id=box.id,collector_id=user.id,declared=p.amount,notes=p.notes)
        db.add(row);db.flush();result={'delivery_id':row.id}
    elif action in ('receive','receive_collector','reject_delivery'):
        if action=='receive_collector':
            session=operational_session(db,box,user,p.session_id)
            check_version(session,p.version)
            collector=db.get(User,p.target_id)
            if not collector or collector.company_id!=user.company_id or collector.role!='collector' or collector.branch_id!=box.branch_id:
                fail('Selecciona un cobrador de esta sucursal.',404)
            if db.scalar(select(CashDelivery.id).where(CashDelivery.box_id==box.id,CashDelivery.collector_id==collector.id,CashDelivery.state=='pending')):
                fail('Este cobrador ya tiene una entrega declarada. Recíbela desde la lista de entregas.',409)
            due=pending(db,box,collector.id)
            if p.amount<=0 or p.amount>due: fail('El efectivo recibido debe ser mayor que cero y no superar el pendiente del cobrador.')
            row=CashDelivery(box_id=box.id,collector_id=collector.id,declared=due,notes=p.notes)
            db.add(row);db.flush()
        else:
            row=db.get(CashDelivery,p.target_id)
        if not row or row.box_id!=box.id: fail('Entrega no encontrada.',404)
        if row.state!='pending': fail('Esta entrega ya fue procesada.',409)
        if action!='receive_collector': check_version(row,p.version)
        if action=='reject_delivery':
            if not p.notes.strip(): fail('Indica el motivo de rechazo.')
            row.state='rejected';row.notes=p.notes;row.cashier_id=user.id;row.confirmed_at=now()
        else:
            session=operational_session(db,box,user,p.session_id)
            due=pending(db,box,row.collector_id)
            if p.amount<=0 or p.amount>min(row.declared,due): fail('Recibe un importe positivo que no supere lo declarado ni lo pendiente.')
            if p.amount!=row.declared and not p.notes.strip(): fail('Indica el motivo del faltante.')
            remaining=p.amount
            for payment,available in outstanding_rows(db,box,row.collector_id):
                take=min(remaining,available)
                if take: db.add(CashAllocation(delivery_id=row.id,payment_id=payment.id,amount=take))
                remaining-=take
                if not remaining: break
            row.received=p.amount;row.remaining=due-p.amount;row.state='confirmed';row.cashier_id=user.id;row.session_id=session.id;row.confirmed_at=now();row.notes=p.notes
            add_movement(db,box,session,user,'delivery',p.amount,p.notes or 'Entrega de cobrador',collector_id=row.collector_id,delivery_id=row.id,reference=f'ENT-{row.id}')
        result={'delivery_id':row.id,'state':row.state}
    elif action=='report_surplus':
        session=operational_session(db,box,user,p.session_id)
        collector=db.get(User,p.target_id)
        if not collector or collector.company_id!=user.company_id or collector.branch_id!=box.branch_id or collector.role!='collector': fail('Selecciona un cobrador de esta sucursal.')
        if p.amount<=0 or not p.notes.strip(): fail('Indica el importe sobrante y su explicación.')
        row=CashDelivery(box_id=box.id,collector_id=collector.id,declared=p.amount,state='surplus_review',cashier_id=user.id,session_id=session.id,notes=p.notes)
        db.add(row);db.flush();result={'delivery_id':row.id,'state':row.state}
    elif action in ('confirm_surplus','reject_surplus'):
        row=db.get(CashDelivery,p.target_id)
        if not row or row.box_id!=box.id: fail('Sobrante no encontrado.',404)
        if row.state!='surplus_review': fail('El sobrante ya fue revisado.',409)
        check_version(row,p.version)
        if not p.notes.strip(): fail('Registra el resultado de la revisión del sobrante.')
        if action=='confirm_surplus':
            session=operational_session(db,box,user,p.session_id)
            row.received=row.declared;row.remaining=pending(db,box,row.collector_id);row.state='surplus_confirmed'
            add_movement(db,box,session,user,'surplus',row.declared,p.notes,collector_id=row.collector_id,delivery_id=row.id,reference=f'SOB-{row.id}')
        else: row.state='surplus_rejected'
        row.cashier_id=user.id;row.confirmed_at=now();row.notes=p.notes
        result={'delivery_id':row.id,'state':row.state}
    elif action=='movement':
        session=operational_session(db,box,user,p.session_id);check_version(session,p.version)
        if p.kind=='withdrawal': fail('Un retiro de caja lo solicita la administración y el cajero confirma la entrega.',403)
        if p.capital: fail('El cajero no mueve el capital de la empresa: los aportes y retiros con capital los registra la administración.',403)
        if p.kind=='contribution': fail('Un aporte de dinero a caja lo registra la administración y el cajero solo confirma que lo recibió.',403)
        if not p.kind or p.amount<=0 or not p.notes.strip(): fail('Indica tipo, importe positivo y concepto.')
        if p.kind=='bank_deposit' and not p.reference.strip(): fail('Indica la referencia del depósito.')
        amount=-p.amount
        row=add_movement(db,box,session,user,p.kind,amount,p.notes,reference=p.reference,proof=p.proof.model_dump() if p.proof else None)
        result={'movement_id':row.id}
    elif action in ('request_addition','cancel_addition','cash_addition','cash_withdrawal','reject_addition'):
        # The ADMINISTRATION asks for money to go into (in) or out of a cashier's box; the cashier only confirms it was
        # received / handed over (or rejects it). The cashier never originates the amount nor its reason.
        from app.models.cash import CashAdditionRequest
        stamp=datetime.now(UTC)
        if action=='request_addition':
            if p.direction not in ('in','out'): fail('Indica si el dinero entra o sale de la caja.')
            cashier=db.get(User,p.cashier_id) if p.cashier_id else None
            if not cashier or cashier.company_id!=user.company_id or cashier.role!='cashier' or not cashier.is_active or cashier.branch_id!=box.branch_id:
                fail('Elige un cajero activo de esta sucursal.',404)
            if p.amount<=0: fail('Indica un monto mayor que cero.')
            if not p.notes.strip(): fail('Indica el motivo.')
            if p.direction=='in' and not p.reference.strip(): fail('Indica el comprobante o la referencia.')
            row=CashAdditionRequest(company_id=user.company_id,box_id=box.id,cashier_id=cashier.id,direction=p.direction,amount=p.amount,
                                    notes=p.notes.strip(),reference=p.reference.strip(),requested_by=user.id)
            db.add(row); db.flush()
            result={'request_id':row.id,'state':'pending'}
        else:
            row=db.get(CashAdditionRequest,p.target_id) if p.target_id else None
            if not row or row.box_id!=box.id: fail('Solicitud no encontrada.',404)
            if row.state!='pending': fail('La solicitud ya fue resuelta.',409)
            if action!='cancel_addition' and row.cashier_id!=user.id: fail('Esta solicitud no es para ti.',403)
            check_version(row,p.version)
            if action=='cancel_addition':
                row.state='cancelled'
            elif action=='reject_addition':
                if not p.notes.strip(): fail('Indica por qué la rechazas.')
                row.state='rejected'
            else:
                want='in' if action=='cash_addition' else 'out'
                if row.direction!=want: fail('Esta solicitud no corresponde a esta operación.',409)
                session=operational_session(db,box,user,p.session_id)
                if want=='out' and session.balance<row.amount: fail('El efectivo disponible no alcanza para esta salida.',409)
                mv=add_movement(db,box,session,user,'cash_addition' if want=='in' else 'withdrawal',row.amount if want=='in' else -row.amount,
                                row.notes,reference=row.reference)
                row.state='confirmed'; row.movement_id=mv.id
                result={'movement_id':mv.id}
            row.resolved_by=user.id; row.resolved_at=stamp; row.resolution_note=p.notes.strip()
            result={**result,'request_id':row.id,'state':row.state}
    elif action=='close':
        session=operational_session(db,box,user,p.session_id)
        if p.version is None or p.version!=session.version: fail('Los datos cambiaron. Actualiza antes de guardar.',409)
        if db.scalar(select(CashDelivery.id).where(CashDelivery.box_id==box.id,CashDelivery.state=='surplus_review')):
            fail('El administrador debe resolver los sobrantes reportados antes del cuadre.',409)
        if session.opening_source=='initial':
            # FM-SIMPLE-CASH-010: counted vs expected, any difference allowed and recorded; no handover to Capital.
            if p.counted is None: fail('Indica el dinero contado.')
            out=cash_core.close_simple_session(db,actor,session.id,counted_amount=p.counted,observation_note=p.notes,
                idempotency_key=p.idempotency_key,snapshot_extra=dict(collector_pending=str(pending(db,box))))
        else:
            out=cash_core.close_session(db,actor,session.id,denominations=p.denominations,observation_note=p.notes,
                receiver_user_id=p.receiver_id,idempotency_key=p.idempotency_key,destination='next_session',
                snapshot_extra=dict(collector_pending=str(pending(db,box))))
        result=_legacy_result(out)
    elif action=='reverse':
        original=db.get(CashMovement,p.target_id)
        if not original or original.box_id!=box.id: fail('Movimiento no encontrado.',404)
        if original.kind not in ('contribution','cash_addition','expense','withdrawal','bank_deposit','delivery','counter_payment','disbursement','surplus'): fail('Este tipo de movimiento no admite otro reverso.')
        if not p.notes.strip(): fail('Indica el motivo del reverso.')
        if db.scalar(select(CashMovement).where(CashMovement.reverses_id==original.id)): fail('Movimiento ya revertido.',409)
        if db.scalar(select(CapitalMovement.id).where(CapitalMovement.cash_movement_id==original.id)):
            fail('Este movimiento quedó vinculado a Capital (historial) y ya no admite reverso.',409)
        session=operational_session(db,box,user,p.session_id);check_version(session,p.version)
        row=add_movement(db,box,session,user,'reversal',-original.amount,p.notes,reverses_id=original.id,collector_id=original.collector_id)
        if original.kind=='counter_payment' and original.payment_id:
            # the collected money leaves the loan too: its balances and installments go back to before the payment
            from app.services.payment_service import revert_payment
            reverted=db.get(Payment,original.payment_id)
            if reverted is not None: revert_payment(db,reverted)
        if original.delivery_id:
            delivery=db.get(CashDelivery,original.delivery_id)
            if db.get(User,delivery.collector_id).branch_id!=box.branch_id: fail('El cobrador cambió de sucursal; resuelve su asignación antes de revertir.')
            delivery.state='reversed';delivery.version+=1
            # Keep original allocations immutable. Compensating negative entries use a reversal receipt.
            correction=CashDelivery(box_id=box.id,collector_id=delivery.collector_id,declared=ZERO,received=-delivery.received,state='reversal',cashier_id=user.id,session_id=session.id,notes=p.notes,confirmed_at=now())
            db.add(correction);db.flush()
            for a in db.scalars(select(CashAllocation).where(CashAllocation.delivery_id==delivery.id)).all():
                db.add(CashAllocation(delivery_id=correction.id,payment_id=a.payment_id,amount=-a.amount))
        result={'movement_id':row.id}
    elif action in ('confirm_transfer','reject_transfer'):
        from app.schemas.payment import PaymentCreate
        from app.services.payment_service import apply_payment
        row=db.get(CashTransfer,p.target_id)
        if not row or row.box_id!=box.id: fail('Transferencia no encontrada.',404)
        if row.state!='pending': fail('Transferencia ya procesada.',409)
        check_version(row,p.version)
        if action=='reject_transfer':
            if not p.notes.strip(): fail('Indica motivo de rechazo.')
            row.state='rejected'
        else:
            payload=PaymentCreate.model_validate(row.payload)
            payment=apply_exact_payment(db,payload,row.collector_id)
            payment.branch_id=box.branch_id;payment.method='transfer';payment.origin=payload.origin;payment.cash_state='confirmed'
            db.flush();row.payment_id=payment.id;row.state='confirmed'
        row.notes=p.notes;row.reviewed_by=user.id;row.reviewed_at=now()
        result={'transfer_id':row.id,'state':row.state}
    elif action=='disburse':
        from app.models.loan_application import LoanApplication
        from app.schemas.loan import LoanCreate
        from app.services.loan_service import create_loan
        from app.services.plan_limits import enforce_can_create
        from app.api.routes.loan_applications import record
        item=db.get(LoanApplication,p.target_id)
        if not item or item.company_id!=user.company_id: fail('Solicitud no encontrada.',404)
        if item.status!='signed' or item.loan_id: fail('La solicitud debe estar firmada y sin desembolso previo.',409)
        check_version(item,p.version)
        if not item.customer_id: fail('Vincula el cliente antes de desembolsar.')
        c=db.get(Customer,item.customer_id)
        bid=branch_for_customer(db,c)
        if bid is not None and bid!=box.branch_id: fail('La solicitud pertenece a otra sucursal.',403)
        if bid is None and user.role!='admin': fail('El administrador debe asignar la sucursal del cliente.')
        from app.services.loan_rules import APPLICATION_FREQUENCY,enforce_principal,enforce_route,enforce_term,first_payment_date
        frequency_key=APPLICATION_FREQUENCY.get(str(item.data.get('payment_frequency')),'')
        first_date=first_payment_date(db,user.company_id,p.first_payment_date,frequency_key)
        if not p.reference.strip() or not first_date or first_date<today(): fail('Indica referencia y primera fecha de pago válida.')
        enforce_principal(db,user.company_id,item.data.get('requested_amount'))
        enforce_term(db,user.company_id,int(item.terms.get('installment_count',0)),frequency_key)
        enforce_route(db,user.company_id,c)
        if p.method in ('transfer','check') and not p.proof: fail('Adjunta el comprobante bancario del desembolso.')
        session=operational_session(db,box,user,p.session_id)
        amount=Decimal(item.data['requested_amount'])
        if p.method=='cash' and session.balance<amount: fail('Efectivo insuficiente.',409)
        enforce_can_create(db,user.company_id,'loan')
        c.cash_branch_id=box.branch_id
        loan=create_loan(db,LoanCreate(customer_id=c.id,principal_amount=amount,payment_frequency={'Semanal':'weekly','Quincenal':'biweekly','Mensual':'monthly'}[item.data['payment_frequency']],start_date=first_date,auto_approve=True,requires_promissory_note=True,**{k:item.terms[k] for k in ('interest_rate','installment_count','late_fee_rate','grace_days')}),user.id)
        loan.cash_branch_id=box.branch_id
        item.loan_id=loan.id;item.status='disbursed';item.terms={**item.terms,'disbursement_reference':p.reference,'disbursement_date':str(today()),'first_payment_date':str(first_date),'method':p.method,'branch_id':box.branch_id}
        record(item,user,'disburse',p.notes)
        add_movement(db,box,session,user,'disbursement' if p.method=='cash' else 'bank_disbursement',-amount if p.method=='cash' else ZERO,p.notes or 'Desembolso de préstamo',loan_id=loan.id,reference=p.reference,proof=p.proof.model_dump() if p.proof else None)
        result={'loan_id':loan.id,'application_id':item.id}
    audit(db,box,user,action,**result,notes=p.notes,resolution=p.resolution,input=p.model_dump(mode='json',exclude={'proof','idempotency_key'}))
    return remember(db,user,p.idempotency_key,digest,result)

def user_has_pending(db,user):
    for box in db.scalars(select(CashBox).where(CashBox.company_id==user.company_id)).all():
        if pending(db,box,user.id)>0: return True
        if db.scalar(select(CashDelivery.id).where(CashDelivery.box_id==box.id,CashDelivery.collector_id==user.id,CashDelivery.state.in_(['pending','surplus_review']))): return True
        if db.scalar(select(CashTransfer.id).where(CashTransfer.box_id==box.id,CashTransfer.collector_id==user.id,CashTransfer.state=='pending')): return True
    return False
