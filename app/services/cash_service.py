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
from app.services import capital_service

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
        CashSession.state.in_(['opening_pending','opening_review','open','closing_review','closing_transfer_pending']),
    )
    if cashier_id is not None:
        statement = statement.where(CashSession.cashier_id == cashier_id)
    row = db.scalar(statement.order_by(CashSession.id.desc()))
    if required and (not row or row.state!='open'):
        fail('Debes abrir caja y resolver sus diferencias antes de operar.',409)
    return row

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


def _same_company_branch(db, user, candidate, branch_id):
    if not candidate or candidate.company_id != user.company_id or not candidate.is_active:
        return False
    return candidate.role == 'admin' or candidate.branch_id == branch_id


def _custody_transfer(db, *, box, session, kind, from_user_id, to_user_id, amount, notes=''):
    row = CashCustodyTransfer(
        company_id=box.company_id,
        box_id=box.id,
        session_id=session.id,
        kind=kind,
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        amount=amount,
        state='pending',
        notes=notes or '',
    )
    db.add(row)
    db.flush()
    return row


def _require_acceptance(p):
    if not p.acceptance_id or p.acceptance_method != 'authenticated_confirmation':
        fail('La entrega requiere una aceptación autenticada e identificable.')


def _record_capital_handover(db, user, transfer, session, *, direction, amount, notes):
    """Record one physical custody leg and its single capital ledger entry."""
    if transfer.cash_movement_id or transfer.capital_movement_id:
        fail('La transferencia física ya fue registrada.', 409)
    if amount == ZERO:
        return None, None
    movement_amount = amount if direction == 'to_cash' else -amount
    movement = CashMovement(
        box_id=transfer.box_id,
        session_id=session.id,
        kind='opening_fund' if direction == 'to_cash' else 'capital_transfer',
        amount=movement_amount,
        actor_id=user.id,
        notes=notes or ('Entrega de fondo desde capital' if direction == 'to_cash' else 'Entrega total de cierre a capital'),
        reference=f'CUST-{transfer.id}',
        custody_transfer_id=transfer.id,
    )
    db.add(movement)
    db.flush()
    capital_row = capital_service.record(
        db,
        transfer.company_id,
        user.id,
        direction,
        amount,
        notes or '',
        cash_movement_id=movement.id,
    )
    transfer.cash_movement_id = movement.id
    transfer.capital_movement_id = capital_row.id
    return movement, capital_row

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
    if user.role=='cashier' and payload.origin!='counter': fail('El cajero registra cobros de ventanilla.',403)
    loan=db.get(Loan,payload.loan_id)
    if not loan: fail('Préstamo no encontrado.',404)
    ensure_customer(db,user,loan,box)
    if payload.amount is None: fail('Actualiza la app: registra un importe explícito.')
    session=active_session(db,box,cashier_id=user.id if user.role=='cashier' else None) if payload.origin=='counter' else None
    if payload.method=='transfer':
        if not payload.reference_code or not payload.bank_account_id or not payload.proof: fail('La transferencia requiere referencia, cuenta bancaria de destino y comprobante.')
        account=db.get(BankAccount,payload.bank_account_id)
        if not account or account.company_id!=user.company_id or not account.is_active: fail('Selecciona una cuenta bancaria activa de la empresa.')
        row=CashTransfer(box_id=box.id,collector_id=user.id,loan_id=loan.id,amount=payload.amount,reference=payload.reference_code,destination=account.label,bank_account_id=account.id,proof=payload.proof.model_dump(),payload=payload.model_dump(mode='json'))
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
    audit(db,row,user,'configured',initial_balance=payload.initial_balance,notes=payload.notes)
    return dict(box_id=row.id)

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

def command(db,user,p):
    box=scope(db,user,p.branch_id,lock=True)
    old,digest=replay(db,user,p.idempotency_key,p.model_dump(mode='json'))
    if old: return old
    action=p.action
    if action=='declare': require_role(user,'collector')
    elif action in ('resolve','reverse','confirm_surplus','reject_surplus'): require_role(user,'admin')
    elif action in ('confirm_opening','confirm_closing_transfer'): require_role(user,'admin','manager','cashier')
    else: require_role(user,'admin','cashier')
    result={}
    if action=='open':
        cashier = user
        deliverer = user
        if user.role == 'admin' and p.target_id:
            candidate = db.get(User, p.target_id)
            if candidate and candidate.role == 'cashier':
                cashier = candidate
                deliverer = user
        elif user.role == 'cashier':
            candidate = db.get(User, p.target_id) if p.target_id else None
            if not candidate or candidate.role not in ('admin','manager') or not _same_company_branch(db, user, candidate, box.branch_id):
                fail('El cajero debe identificar al encargado que entrega el fondo.')
            deliverer = candidate
        if not _same_company_branch(db, user, cashier, box.branch_id):
            fail('El cajero receptor no pertenece a la sucursal.', 403)
        if deliverer.id == cashier.id and user.role != 'admin':
            fail('La entrega y la recepción deben quedar separadas.', 403)
        if active_session(db,box,False,cashier_id=cashier.id):
            fail('Este cajero ya tiene una jornada abierta o pendiente de resolver.',409)
        row=CashSession(box_id=box.id,business_date=today(),opening_expected=ZERO,opening_counted=p.amount,balance=ZERO,opened_by=user.id,cashier_id=cashier.id,state='opening_pending',notes=p.notes)
        db.add(row);db.flush();result={'session_id':row.id,'state':row.state}
        transfer=_custody_transfer(db,box=box,session=row,kind='opening_fund',from_user_id=deliverer.id,to_user_id=cashier.id,amount=p.amount,notes=p.notes)
        result['transfer_id']=transfer.id
    elif action=='confirm_opening':
        session=db.get(CashSession,p.target_id)
        if not session or session.box_id!=box.id or session.state!='opening_pending': fail('La apertura no está pendiente de confirmación.',409)
        if session.cashier_id != user.id and user.role != 'admin': fail('Solo el cajero receptor puede confirmar el fondo.',403)
        transfer=db.scalar(select(CashCustodyTransfer).where(CashCustodyTransfer.session_id==session.id,CashCustodyTransfer.kind=='opening_fund',CashCustodyTransfer.state=='pending'))
        if not transfer: fail('No existe una entrega de fondo pendiente.',409)
        check_version(session,p.version)
        check_version(transfer, p.transfer_version if p.transfer_version is not None else transfer.version)
        _require_acceptance(p)
        if p.amount != transfer.amount: fail('El importe recibido debe coincidir con el fondo entregado.',409)
        _record_capital_handover(db,user,transfer,session,direction='to_cash',amount=transfer.amount,notes=p.notes)
        transfer.state='confirmed';transfer.acceptance_id=p.acceptance_id;transfer.acceptance_method=p.acceptance_method;transfer.accepted_by=user.id;transfer.accepted_at=now();transfer.notes=p.notes or transfer.notes
        session.opening_counted=transfer.amount;session.balance=transfer.amount;session.state='open';session.opened_at=now()
        result={'session_id':session.id,'transfer_id':transfer.id,'state':session.state}
    elif action=='declare':
        reserved=db.scalar(select(func.coalesce(func.sum(CashDelivery.declared),0)).where(CashDelivery.box_id==box.id,CashDelivery.collector_id==user.id,CashDelivery.state=='pending'))
        if p.amount<=0 or p.amount>pending(db,box,user.id)-reserved: fail('La entrega supera el pendiente disponible o ya declarado.')
        row=CashDelivery(box_id=box.id,collector_id=user.id,declared=p.amount,notes=p.notes)
        db.add(row);db.flush();result={'delivery_id':row.id}
    elif action in ('receive','receive_collector','reject_delivery'):
        if action=='receive_collector':
            session=active_session(db,box,cashier_id=user.id if user.role=='cashier' else None)
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
            session=active_session(db,box,cashier_id=user.id if user.role=='cashier' else None)
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
        session=active_session(db,box,cashier_id=user.id if user.role=='cashier' else None)
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
            session=active_session(db,box,cashier_id=user.id if user.role=='cashier' else None)
            row.received=row.declared;row.remaining=pending(db,box,row.collector_id);row.state='surplus_confirmed'
            add_movement(db,box,session,user,'surplus',row.declared,p.notes,collector_id=row.collector_id,delivery_id=row.id,reference=f'SOB-{row.id}')
        else: row.state='surplus_rejected'
        row.cashier_id=user.id;row.confirmed_at=now();row.notes=p.notes
        result={'delivery_id':row.id,'state':row.state}
    elif action=='movement':
        from app.services import capital_service
        session=active_session(db,box,cashier_id=user.id if user.role=='cashier' else None);check_version(session,p.version)
        if not p.kind or p.amount<=0 or not p.notes.strip(): fail('Indica tipo, importe positivo y concepto.')
        if p.kind=='bank_deposit' and not p.reference.strip(): fail('Indica la referencia del depósito.')
        if p.capital and p.kind not in ('contribution','withdrawal'): fail('El movimiento con capital debe ser aporte (desde capital) o retiro (hacia capital).')
        # capital -> caja needs enough reserve; checked before the cash entry so both stay consistent.
        if p.capital and p.kind=='contribution' and p.amount>capital_service.balance(db,box.company_id): fail('El capital disponible no alcanza para este aporte a caja.',409)
        amount=p.amount if p.kind=='contribution' else -p.amount
        row=add_movement(db,box,session,user,p.kind,amount,p.notes,reference=p.reference,proof=p.proof.model_dump() if p.proof else None)
        if p.capital:
            capital_service.record(db,box.company_id,user.id,'to_cash' if p.kind=='contribution' else 'from_cash',p.amount,p.notes,cash_movement_id=row.id)
        result={'movement_id':row.id}
    elif action=='close':
        session=active_session(db,box,cashier_id=user.id if user.role=='cashier' else None);check_version(session,p.version)
        if db.scalar(select(CashDelivery.id).where(CashDelivery.box_id==box.id,CashDelivery.state=='surplus_review')):
            fail('El administrador debe resolver los sobrantes reportados antes del cuadre.',409)
        if not p.denominations or any(k not in DENOMINATIONS or isinstance(v,bool) or v<0 or v>1_000_000 for k,v in p.denominations.items()): fail('Conteo por denominaciones inválido.')
        counted=sum((Decimal(k)*v for k,v in p.denominations.items()),ZERO)
        if counted>Decimal('9999999999.99'): fail('Conteo fuera de rango.')
        difference=counted-session.balance
        if difference and not p.notes.strip(): fail('Indica el motivo del faltante o sobrante.')
        movements=db.scalars(select(CashMovement).where(CashMovement.session_id==session.id,CashMovement.kind.not_in(('opening_adjustment','opening_fund','capital_transfer')))).all()
        session.snapshot=dict(opening=str(session.opening_counted),incoming=str(sum((m.amount for m in movements if m.amount>0),ZERO)),outgoing=str(-sum((m.amount for m in movements if m.amount<0),ZERO)),expected=str(session.balance),collector_pending=str(pending(db,box)),movement_ids=[m.id for m in movements])
        session.counted=counted;session.difference=difference;session.denominations=p.denominations;session.notes=p.notes;session.closed_by=user.id;session.closed_at=now()
        if difference:
            session.state='closing_review'
            result={'session_id':session.id,'state':session.state,'difference':str(difference)}
        else:
            receiver = db.get(User,p.target_id) if p.target_id else (user if user.role=='admin' else None)
            if not receiver or receiver.role not in ('admin','manager') or not _same_company_branch(db,user,receiver,box.branch_id):
                fail('Identifica al encargado que recibirá físicamente el efectivo de cierre.',403)
            if receiver.id == session.cashier_id and user.role != 'admin': fail('La entrega de cierre requiere un responsable distinto.',403)
            transfer=_custody_transfer(db,box=box,session=session,kind='closing_capital',from_user_id=session.cashier_id or session.opened_by,to_user_id=receiver.id,amount=counted,notes=p.notes)
            session.state='closing_transfer_pending'
            result={'session_id':session.id,'transfer_id':transfer.id,'state':session.state,'difference':str(difference)}
    elif action=='resolve':
        row=db.get(CashSession,p.target_id)
        if not row or row.box_id!=box.id: fail('Jornada no encontrada.',404)
        check_version(row,p.version)
        if row.state not in ('opening_review','closing_review'): fail('La jornada no requiere resolución.',409)
        if not p.notes.strip() or not p.resolution: fail('Indica resolución y motivo.')
        opening=row.state=='opening_review'
        if p.resolution=='return':
            if opening:
                # Return to the expected float; physical correction is confirmed by the admin.
                fail('Para corregir la apertura indica el conteo corregido y aprueba la resolución.')
            row.state='open';row.closed_at=None;row.closed_by=None
        else:
            difference=(row.opening_counted-row.opening_expected) if opening else row.difference
            if opening:
                row.opening_counted=p.amount;row.balance=p.amount;difference=p.amount-row.opening_expected
                row.state='open'
            else:
                receiver = db.get(User,p.target_id) if p.target_id else (user if user.role=='admin' else None)
                if not receiver or receiver.role not in ('admin','manager') or not _same_company_branch(db,user,receiver,box.branch_id):
                    fail('Identifica al encargado que recibirá físicamente el efectivo de cierre.',403)
                transfer=_custody_transfer(db,box=box,session=row,kind='closing_capital',from_user_id=row.cashier_id or row.opened_by,to_user_id=receiver.id,amount=row.counted or ZERO,notes=p.notes)
                row.state='closing_transfer_pending'
                result['transfer_id']=transfer.id
            # Adjustment is recorded, never applied twice to expected / counted totals.
            if difference:
                db.add(CashMovement(box_id=box.id,session_id=row.id,actor_id=user.id,kind='opening_adjustment' if opening else 'closing_adjustment',amount=difference,notes=p.notes))
        row.resolved_by=user.id
        result.update({'session_id':row.id,'state':row.state})
    elif action=='confirm_closing_transfer':
        transfer=db.get(CashCustodyTransfer,p.target_id)
        if not transfer or transfer.box_id!=box.id or transfer.kind!='closing_capital': fail('Transferencia física de cierre no encontrada.',404)
        if transfer.state!='pending': fail('La transferencia física ya fue procesada.',409)
        session=db.get(CashSession,transfer.session_id)
        if not session or session.state!='closing_transfer_pending': fail('La jornada no está pendiente de transferencia.',409)
        if user.id != transfer.to_user_id and user.role != 'admin': fail('Solo el responsable receptor puede confirmar la entrega.',403)
        check_version(session,p.version)
        check_version(transfer, p.transfer_version if p.transfer_version is not None else transfer.version)
        _require_acceptance(p)
        _record_capital_handover(db,user,transfer,session,direction='from_cash',amount=transfer.amount,notes=p.notes)
        session.balance=ZERO;session.state='closed';session.closed_at=now();session.closed_by=user.id
        transfer.state='confirmed';transfer.acceptance_id=p.acceptance_id;transfer.acceptance_method=p.acceptance_method;transfer.accepted_by=user.id;transfer.accepted_at=now();transfer.notes=p.notes or transfer.notes
        result={'session_id':session.id,'transfer_id':transfer.id,'state':session.state,'amount':str(transfer.amount)}
    elif action=='reverse':
        original=db.get(CashMovement,p.target_id)
        if not original or original.box_id!=box.id: fail('Movimiento no encontrado.',404)
        if original.kind not in ('contribution','expense','withdrawal','bank_deposit','delivery','counter_payment','disbursement','surplus'): fail('Este tipo de movimiento no admite otro reverso.')
        if not p.notes.strip(): fail('Indica el motivo del reverso.')
        if db.scalar(select(CashMovement).where(CashMovement.reverses_id==original.id)): fail('Movimiento ya revertido.',409)
        session=active_session(db,box,cashier_id=user.id if user.role=='cashier' else None);check_version(session,p.version)
        row=add_movement(db,box,session,user,'reversal',-original.amount,p.notes,reverses_id=original.id,collector_id=original.collector_id)
        cap=db.scalar(select(CapitalMovement).where(CapitalMovement.cash_movement_id==original.id))
        if cap:
            from app.services import capital_service
            # Undo the capital side: a to_cash (capital->caja) is returned to capital, and vice versa.
            capital_service.record(db,box.company_id,user.id,'from_cash' if cap.kind=='to_cash' else 'to_cash',cap.amount,'Reverso: '+p.notes,cash_movement_id=row.id)
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
        if not p.reference.strip() or not p.first_payment_date or p.first_payment_date<today(): fail('Indica referencia y primera fecha de pago válida.')
        if p.method in ('transfer','check') and not p.proof: fail('Adjunta el comprobante bancario del desembolso.')
        session=active_session(db,box,cashier_id=user.id if user.role=='cashier' else None)
        amount=Decimal(item.data['requested_amount'])
        if p.method=='cash' and session.balance<amount: fail('Efectivo insuficiente.',409)
        enforce_can_create(db,user.company_id,'loan')
        c.cash_branch_id=box.branch_id
        loan=create_loan(db,LoanCreate(customer_id=c.id,principal_amount=amount,payment_frequency={'Semanal':'weekly','Quincenal':'biweekly','Mensual':'monthly'}[item.data['payment_frequency']],start_date=p.first_payment_date,auto_approve=True,requires_promissory_note=True,**{k:item.terms[k] for k in ('interest_rate','installment_count','late_fee_rate','grace_days')}),user.id)
        loan.cash_branch_id=box.branch_id
        item.loan_id=loan.id;item.status='disbursed';item.terms={**item.terms,'disbursement_reference':p.reference,'disbursement_date':str(today()),'first_payment_date':str(p.first_payment_date),'method':p.method,'branch_id':box.branch_id}
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
