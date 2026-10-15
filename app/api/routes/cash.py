import base64
from datetime import date, datetime, time, timedelta, UTC
from html import escape
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape as xml_escape
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.branch import Branch
from app.models.customer import Customer
from app.models.route import Route
from app.models.loan import Loan
from app.models.payment import Payment
from app.models.loan_application import LoanApplication
from app.models.bank_account import BankAccount
from app.models.company import Company
from app.models.cash import *
from app.schemas.cash import CashCommand, CashSetup
from app.services import cash_service as svc

router=APIRouter()
from app.api.routes.cash_socket import router as socket_router
from app.services.cash_live import publish
router.include_router(socket_router)

def commit(db, fn):
    try:
        result=fn();db.commit()
        for company_id, branch_id in db.info.pop('cash_notifications', set()):
            publish(company_id, branch_id)
        return result
    except (IntegrityError,OperationalError,StaleDataError) as e:
        db.info.pop('cash_notifications', None)
        db.rollback();raise HTTPException(409,'Otra operación modificó la caja. Actualiza y reintenta con la misma clave.') from e
    except Exception:
        db.info.pop('cash_notifications', None)
        db.rollback();raise

PRIVATE_COLUMNS=('proof','payload','open_idempotency_key','open_request_digest','close_idempotency_key','close_request_digest',
                 'accept_idempotency_key','accept_request_digest')

def public(row):
    data={c.name:getattr(row,c.name) for c in row.__table__.columns if c.name not in PRIVATE_COLUMNS}
    if hasattr(row,'proof'): data['has_proof']=bool(row.proof)
    return jsonable_encoder(data)

@router.get('/config')
def config(db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    svc.require_role(user,'admin','cashier','manager','collector')
    bs=db.scalars(select(Branch).where(Branch.company_id==user.company_id)).all()
    if user.role!='admin': bs=[b for b in bs if b.id==user.branch_id]
    boxes=db.scalars(select(CashBox).where(CashBox.company_id==user.company_id)).all()
    pending_users=[]
    if user.role=='admin':
        ready={b.id for b in bs if b.is_active and any(x.branch_id==b.id for x in boxes)}
        users=db.scalars(select(User).where(User.company_id==user.company_id,User.is_active==True,User.role.in_(['collector','cashier'])).order_by(User.full_name)).all()
        pending_users=[dict(id=u.id,name=u.full_name,email=u.email,role=u.role,
            reason='Sin sucursal asignada' if not u.branch_id else 'Su sucursal está inactiva o no tiene caja configurada')
            for u in users if u.branch_id not in ready and 'Notario' not in u.branch_roles]
    return dict(enabled=svc.enabled(db,user.company_id),pending_users=pending_users,branches=[dict(id=b.id,name=b.name,active=b.is_active,configured=any(x.branch_id==b.id for x in boxes)) for b in bs],role=user.role,branch_id=user.branch_id)

@router.post('/setup')
def setup(p:CashSetup,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return commit(db,lambda:svc.setup(db,user,p))

@router.post('/activate')
def activate(db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return commit(db,lambda:svc.activate(db,user))

@router.post('/commands')
def command(p:CashCommand,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return commit(db,lambda:svc.command(db,user,p))

@router.get('/workspace')
def workspace(branch_id:int|None=None,history_limit:int=Query(200,ge=20,le=1000),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return build_workspace(db,user,branch_id,history_limit)

def build_workspace(db,user,branch_id,history_limit):
    """Branch cash workspace (``history_limit=None`` = the whole history, used by the date-range report). The historical lists (payments, deliveries, transfers, sessions, movements, custody, audit)
    carry the ``history_limit`` most recent rows PLUS everything still active (pending, open or closing), so the day-to-day
    screens are complete while the payload stays bounded. ``truncated`` tells the web how many rows each list holds in all;
    older history is read through the paged ``/cash/report``."""
    box=svc.scope(db,user,branch_id)
    truncated={}
    def bounded(name,model,where,active=None):
        base=select(model).where(*where)
        newest=base.order_by(model.id.desc())
        rows=list(db.scalars(newest.limit(history_limit) if history_limit is not None else newest).all())
        if active is not None and history_limit is not None:
            have={r.id for r in rows}
            rows=sorted(rows+[r for r in db.scalars(base.where(active)).all() if r.id not in have],key=lambda r:-r.id)
        total=db.scalar(select(func.count()).select_from(model).where(*where)) or 0
        truncated[name]=dict(total=total,shown=len(rows))
        return rows
    users={u.id:u for u in db.scalars(select(User).where(User.company_id==user.company_id)).all()}
    def enrich(row):
        result=public(row)
        if isinstance(row,CashMovement) and row.kind=='bank_disbursement' and row.loan_id:
            result['bank_amount']=str(db.get(Loan,row.loan_id).principal_amount)
        for k in ('collector_id','cashier_id','actor_id','opened_by','closed_by','reviewed_by','from_user_id','to_user_id','accepted_by'):
            uid=result.get(k)
            if uid: result[k+'_name']=users[uid].full_name if uid in users else 'Usuario histórico'
        return result
    mine=user.id if user.role=='collector' else None
    outstanding=svc.outstanding_rows(db,box,mine)
    pay_where=[Payment.branch_id==box.branch_id]
    del_where=[CashDelivery.box_id==box.id]
    tr_where=[CashTransfer.box_id==box.id]
    if mine:
        pay_where.append(Payment.collected_by_id==mine)
        del_where.append(CashDelivery.collector_id==mine)
        tr_where.append(CashTransfer.collector_id==mine)
    owed_ids=[p.id for p,_ in outstanding]
    payments=bounded('payments',Payment,pay_where,Payment.id.in_(owed_ids) if owed_ids else None)
    deliveries=bounded('deliveries',CashDelivery,del_where,CashDelivery.state.in_(['pending','surplus_review']))
    transfers=bounded('transfers',CashTransfer,tr_where,CashTransfer.state=='pending')
    bank_accounts=db.scalars(select(BankAccount).where(BankAccount.company_id==user.company_id,BankAccount.is_active==True).order_by(BankAccount.bank_name)).all()
    totals={}
    for p,amount in outstanding: totals[p.collected_by_id]=totals.get(p.collected_by_id,svc.ZERO)+amount
    data=dict(branch_id=box.branch_id,branch_name=db.get(Branch,box.branch_id).name,
        bank_accounts=[dict(id=a.id,bank_name=a.bank_name,account_number=a.account_number,account_holder=a.account_holder,label=a.label) for a in bank_accounts],
        pending=str(svc.pending(db,box,mine)),transfers_pending=str(sum((t.amount for t in transfers if t.state=='pending'),svc.ZERO)),
        deliveries=[{**enrich(d),'allocations':[dict(payment_id=a.payment_id,amount=str(a.amount)) for a in db.scalars(select(CashAllocation).where(CashAllocation.delivery_id==d.id)).all()]} for d in deliveries],transfers=[enrich(t) for t in transfers],
        payments=[{**enrich(p),'customer_name':db.get(Customer,db.get(Loan,p.loan_id).customer_id).full_name} for p in payments],
        collectors=[dict(id=uid,name=users[uid].full_name,pending=str(amount)) for uid,amount in totals.items()],
        outstanding=[dict(payment_id=p.id,collector_id=p.collected_by_id,loan_id=p.loan_id,paid_at=p.paid_at,amount=str(p.amount),pending=str(amount)) for p,amount in outstanding])
    if not mine:
        session=svc.active_session(db,box,False,user.id) if user.role=='cashier' else None
        ses_where=[CashSession.box_id==box.id]
        mov_where=[CashMovement.box_id==box.id]
        cus_where=[CashCustodyTransfer.box_id==box.id]
        if user.role == 'cashier':
            ses_where.append(CashSession.cashier_id==user.id)
            mov_where.append(CashMovement.session_id.in_(select(CashSession.id).where(*ses_where)))
            cus_where.append(or_(CashCustodyTransfer.to_user_id==user.id,CashCustodyTransfer.from_user_id==user.id))
        live=select(CashSession.id).where(*ses_where,CashSession.state.in_(['open','closing']))
        sessions=bounded('sessions',CashSession,ses_where,CashSession.state.in_(['open','closing']))
        movements=bounded('movements',CashMovement,mov_where,CashMovement.session_id.in_(live))
        custody_transfers=bounded('custody_transfers',CashCustodyTransfer,cus_where)
        last=sessions[0] if sessions else None
        historical_users={m.actor_id for m in movements}|{d.collector_id for d in deliveries}|{d.cashier_id for d in deliveries}
        # A user holding the "Notario" role is a legacy `collector` but never collects: keep it out of the cash lists.
        branch_users=[dict(id=u.id,name=u.full_name,role=u.role) for u in users.values()
            if (u.branch_id==box.branch_id or u.role=='admin' or u.id in historical_users)
            and not (u.id not in historical_users and 'Notario' in u.branch_roles)]
        # One query for the loans with their customers and one each for routes / collectors: no per-loan lookups.
        rows=db.execute(select(Loan,Customer).join(Customer,Customer.id==Loan.customer_id).where(Customer.company_id==user.company_id,Loan.status.in_(['active','late'])).order_by(Loan.id)).all()
        route_branch={r.id:r.branch_id for r in db.scalars(select(Route).where(Route.company_id==user.company_id)).all()}
        collector_branch={u.id:u.branch_id for u in users.values()}
        def customer_branch(c):
            if c.cash_branch_id: return c.cash_branch_id
            if c.route_id and route_branch.get(c.route_id): return route_branch[c.route_id]
            if c.assigned_collector_id: return collector_branch.get(c.assigned_collector_id)
            return None
        loans=[]
        for l,c in rows:
            owed=l.principal_balance+l.interest_balance+l.late_fee_balance
            where=customer_branch(c)
            if where==box.branch_id or (user.role=='admin' and where is None):
                loans.append(dict(id=l.id,name=c.full_name,balance=str(owed),installment_amount=str(min(l.installment_amount,owed))))
        data.update(users=branch_users,loans=loans,session=enrich(session) if session else None,active_sessions=[enrich(s) for s in sessions if s.state in ('open','closing')],sessions=[enrich(s) for s in sessions],movements=[enrich(m) for m in movements],custody_transfers=[enrich(t) for t in custody_transfers],
          # The next opening is independent. This field is retained for API compatibility,
          # but is informational only and never becomes an opening obligation.
          expected_opening='0.00',
          audit=[enrich(a) for a in bounded('audit',CashAudit,[CashAudit.box_id==box.id])])
        candidates=db.scalars(select(LoanApplication).where(LoanApplication.company_id==user.company_id,LoanApplication.status=='signed')).all()
        data['applications']=[dict(id=a.id,version=a.version,name=a.data.get('full_name'),amount=a.data.get('requested_amount')) for a in candidates if a.customer_id and (svc.branch_for_customer(db,db.get(Customer,a.customer_id))==box.branch_id or (user.role=='admin' and svc.branch_for_customer(db,db.get(Customer,a.customer_id)) is None))]
    if not mine:
        # Money the administration asked to send to / take from a cashier's box (the cashier only confirms it).
        from app.models.cash import CashAdditionRequest
        reqs=db.scalars(select(CashAdditionRequest).where(CashAdditionRequest.box_id==box.id).order_by(CashAdditionRequest.id.desc()).limit(60)).all()
        if user.role=='cashier': reqs=[r for r in reqs if r.cashier_id==user.id]
        name=lambda uid: users[uid].full_name if uid in users else 'Usuario histórico'
        data['addition_requests']=[dict(id=r.id,direction=r.direction,amount=str(r.amount),notes=r.notes,reference=r.reference,state=r.state,
            version=r.version,cashier_id=r.cashier_id,cashier_name=name(r.cashier_id),requested_by=r.requested_by,requested_by_name=name(r.requested_by),
            requested_at=r.requested_at,resolved_at=r.resolved_at,resolution_note=r.resolution_note) for r in reqs]
    if user.role == 'admin':
        # Administrative supervision is company-wide. This read-only
        # aggregate never becomes an operable session or replaces workspace
        # authorization for a selected branch.
        boxes=db.scalars(select(CashBox).where(CashBox.company_id==user.company_id)).all()
        branches={b.id:b.name for b in db.scalars(select(Branch).where(Branch.company_id==user.company_id)).all()}
        branch_rows=[]
        consolidated=svc.ZERO
        closed_cash=svc.ZERO
        for candidate in boxes:
            live=db.scalars(select(CashSession).where(CashSession.box_id==candidate.id,CashSession.state.in_(['open','closing']))).all()
            amount=sum((s.balance for s in live),svc.ZERO)
            consolidated += amount
            # What the cashier counted at the box's last closed shift: it is still the money of a box with no open shift.
            last=db.scalar(select(CashSession).where(CashSession.box_id==candidate.id,CashSession.state=='closed',CashSession.counted.is_not(None)).order_by(CashSession.closed_at.desc(),CashSession.id.desc()).limit(1))
            counted=last.counted if (last is not None and not live) else None
            if counted is not None: closed_cash += counted
            branch_rows.append({'branch_id':candidate.branch_id,'branch_name':branches.get(candidate.branch_id,'Sucursal histórica'),'balance':str(amount),
                'last_closed_counted':str(last.counted) if last is not None else None,'last_closed_at':last.closed_at if last is not None else None,
                'last_closed_session_id':last.id if last is not None else None,'has_open_shift':bool(live)})
        data['cash_boxes']=branch_rows
        data['consolidated_balance']=str(consolidated)
        data['closed_cash_total']=str(closed_cash)
    data['truncated']=truncated
    return jsonable_encoder(data)

def filtered_report(db,user,branch_id,start,end,collector_id,actor_id,kind,state):
    svc.require_role(user,'admin','cashier','manager')
    if start>end: svc.fail('Rango de fechas inválido.')
    if not (1900<=start.year<=2200 and 1900<=end.year<=2200): svc.fail('La fecha está fuera del rango permitido (1900-2200).',422)
    data=build_workspace(db,user,branch_id,None)  # the report reads the whole history, not the bounded view
    low=datetime.combine(start,time.min,svc.TZ).astimezone(UTC)
    high=datetime.combine(end+timedelta(days=1),time.min,svc.TZ).astimezone(UTC)
    def included(row,field):
        value=row.get(field) or row.get('created_at') or row.get('opened_at')
        if not value:return False
        when=datetime.fromisoformat(value)
        when=when.replace(tzinfo=UTC) if when.tzinfo is None else when
        return low<=when<high
    rows=[]
    for category,field in [('movements','created_at'),('sessions','closed_at'),('deliveries','confirmed_at'),('transfers','reviewed_at')]:
        for r in data.get(category,[]):
            if not included(r,field):continue
            if collector_id and r.get('collector_id')!=collector_id:continue
            if actor_id and r.get('actor_id',r.get('cashier_id',r.get('closed_by',r.get('reviewed_by'))))!=actor_id:continue
            if kind and r.get('kind',category)!=kind:continue
            if state and r.get('state','confirmed')!=state:continue
            rows.append(dict(category=category,**r))
    return dict(branch_name=data['branch_name'],start=str(start),end=str(end),rows=rows)

def report_table(data):
    headers=['Registro','Número','Fecha','Estado','Tipo','Importe','Cobrador','Responsable','Concepto / referencia','Apertura','Entradas','Salidas','Esperado','Contado','Diferencia','Pendiente cobradores','Conteo por denominaciones','Importe bancario RD$']
    rows=[]
    for r in data['rows']:
        snap=r.get('snapshot',{})
        rows.append([r['category'],str(r['id']),r.get('closed_at') or r.get('confirmed_at') or r.get('reviewed_at') or r.get('created_at') or r.get('opened_at',''),r.get('state','confirmed'),r.get('kind',''),str(r.get('amount',r.get('received',''))),r.get('collector_id_name',''),r.get('actor_id_name',r.get('cashier_id_name',r.get('closed_by_name',r.get('reviewed_by_name','')))),r.get('notes','')+' '+r.get('reference',''),snap.get('opening',''),snap.get('incoming',''),snap.get('outgoing',''),snap.get('expected',''),str(r.get('counted','')),str(r.get('difference','')),snap.get('collector_pending',''),'; '.join(str(k)+' x '+str(v) for k,v in r.get('denominations',{}).items() if v),r.get('bank_amount','')])
    for row in rows:
        if row[2]:
            dt=datetime.fromisoformat(row[2]);row[2]=(dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(svc.TZ).isoformat()
    return headers,rows

@router.get('/report')
def report(start:date,end:date,branch_id:int|None=None,collector_id:int|None=None,actor_id:int|None=None,kind:str='',state:str='',format:str='json',db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    data=filtered_report(db,user,branch_id,start,end,collector_id,actor_id,kind,state)
    headers,rows=report_table(data)
    if format=='json':return data
    if format=='print':
        html='<html><head><meta charset="utf-8"><title>Historial de caja</title><style>@page{size:A4 landscape}body{font:11px Arial}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:5px;word-break:break-word}</style></head><body><h1>Historial de caja · '+escape(data['branch_name'])+'</h1><p>'+escape(str(start)+' — '+str(end))+'</p><table><tr>'+''.join('<th>'+escape(x)+'</th>' for x in headers)+'</tr>'+''.join('<tr>'+''.join('<td>'+escape(str(x))+'</td>' for x in row)+'</tr>' for row in rows)+'</table></body></html>'
        return {'html':html}
    if format!='xlsx':svc.fail('Formato inválido.')
    # Minimal OOXML workbook, inline strings prevent spreadsheet formula injection.
    buf=BytesIO()
    with ZipFile(buf,'w',ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Caja" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join('<row>'+''.join('<c t="inlineStr"><is><t xml:space="preserve">'+xml_escape(str(v))+'</t></is></c>' for v in row)+'</row>' for row in [headers,*rows])+'</sheetData></worksheet>')
    return Response(buf.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="historial-caja.xlsx"'})

@router.get('/receipt/{delivery_id}')
def receipt(delivery_id:int,branch_id:int|None=None,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    box=svc.scope(db,user,branch_id)
    d=db.get(CashDelivery,delivery_id)
    if not d or d.box_id!=box.id or (user.role=='collector' and d.collector_id!=user.id):svc.fail('Comprobante no encontrado.',404)
    if d.state not in ('confirmed','reversed','surplus_confirmed'):svc.fail('Entrega aún no confirmada.')
    fields={'Sucursal':db.get(Branch,box.branch_id).name,'Cobrador':db.get(User,d.collector_id).full_name,'Cajero':db.get(User,d.cashier_id).full_name,'Fecha':d.confirmed_at.replace(tzinfo=UTC).astimezone(svc.TZ).isoformat(),'Declarado RD$':d.declared,'Recibido RD$':d.received,'Pendiente tras entrega RD$':d.remaining,'Estado':d.state,'Observación':d.notes}
    return {'html':'<html><head><meta charset="utf-8"><title>Comprobante de entrega</title></head><body><h1>Comprobante ENT-'+str(d.id)+'</h1>'+''.join('<p><b>'+escape(k)+': </b>'+escape(str(v))+'</p>' for k,v in fields.items())+'</body></html>'}


def custody_receipt_visible(user, transfer):
    """Who may read the receipt of a closing hand-over to capital (F17).

    Supervision (admin/manager; ``svc.scope`` already confines them to the box's company/branch) and the PARTIES of
    the hand-over (the closing cashier and the named receiver), whatever their legacy role. Any other cashier or
    collector of the branch is denied exactly like a missing receipt (same 404, same body)."""
    if user.role in ('admin', 'manager'):
        return True
    return user.id in (transfer.from_user_id, transfer.to_user_id)


@router.get('/custody-receipt/{transfer_id}')
def custody_receipt(transfer_id:int,branch_id:int|None=None,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    box=svc.scope(db,user,branch_id)
    transfer=db.get(CashCustodyTransfer,transfer_id)
    if not transfer or transfer.box_id!=box.id or transfer.state!='confirmed': svc.fail('Comprobante de custodia no encontrado.',404)
    if not custody_receipt_visible(user,transfer): svc.fail('Comprobante de custodia no encontrado.',404)
    names={u.id:u.full_name for u in db.scalars(select(User).where(User.company_id==user.company_id)).all()}
    fields={'Empresa':db.get(Company,user.company_id).name,'Sucursal':db.get(Branch,box.branch_id).name,'Jornada':transfer.session_id,'Tipo':transfer.kind,'Entrega':names.get(transfer.from_user_id,'Usuario histórico'),'Recepción':names.get(transfer.to_user_id,'Usuario histórico'),'Importe RD$':transfer.amount,'Aceptado por':names.get(transfer.accepted_by,'Usuario histórico'),'Fecha de aceptación':transfer.accepted_at,'Identificador de aceptación':transfer.acceptance_id,'Método':transfer.acceptance_method,'Observación':transfer.notes}
    return {'html':'<html><head><meta charset="utf-8"><title>Comprobante de custodia</title></head><body><h1>Comprobante CUST-'+str(transfer.id)+'</h1>'+''.join('<p><b>'+escape(k)+': </b>'+escape(str(v))+'</p>' for k,v in fields.items())+'</body></html>'}

@router.get('/proof/{kind}/{target_id}')
def proof(kind:str,target_id:int,branch_id:int|None=None,inline:bool=False,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    box=svc.scope(db,user,branch_id)
    cls={'transfer':CashTransfer,'movement':CashMovement}.get(kind)
    if cls is None:svc.fail('Documento no encontrado.',404)
    row=db.get(cls,target_id)
    if not row or row.box_id!=box.id or (user.role=='collector' and (kind!='transfer' or row.collector_id!=user.id)):svc.fail('Documento no encontrado.',404)
    if not row.proof:svc.fail('Sin comprobante.',404)
    disposition = 'inline' if inline else 'attachment'
    return Response(base64.b64decode(row.proof['content_base64']),media_type=row.proof['media_type'],headers={'Content-Disposition':f'{disposition}; filename="comprobante"','X-Content-Type-Options':'nosniff'})


def _local(value):
    if value is None: return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(svc.TZ).isoformat()


@router.get('/sessions/{session_id}/ticket')
def session_ticket(session_id:int,kind:str='opening',branch_id:int|None=None,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    """FM-SIMPLE-CASH-010: data of the opening / closing ticket, built ONLY from the persisted session (so a reprint is
    identical and a failed print never needs a second financial write). Cashier: own sessions; admin/manager: their scope."""
    svc.require_role(user,'admin','cashier','manager')
    if kind not in ('opening','closing'): svc.fail('Tipo de ticket inválido.')
    box=svc.scope(db,user,branch_id)
    s=db.get(CashSession,session_id)
    if not s or s.box_id!=box.id or (user.role=='cashier' and s.cashier_id!=user.id): svc.fail('Jornada no encontrada.',404)
    if kind=='closing' and s.state!='closed': svc.fail('La jornada aún no está cerrada.',409)
    names={u.id:u.full_name for u in db.scalars(select(User).where(User.company_id==user.company_id)).all()}
    snap=s.snapshot or {}
    data=dict(title='APERTURA DE CAJA' if kind=='opening' else 'CIERRE DE CAJA',kind=kind,brand='FACT MONEY',
        ticket_number=f'J-{s.id:06d}',session_id=s.id,company=db.get(Company,user.company_id).name,branch=db.get(Branch,box.branch_id).name,
        cashier=names.get(s.cashier_id,'Usuario histórico'),opening_source=s.opening_source,simple=s.opening_source=='initial',
        opened_at=_local(s.opened_at),initial_amount=str(s.opening_expected))
    if kind=='closing':
        diff=s.difference or svc.ZERO
        data.update(closed_at=_local(s.closed_at),closed_by=names.get(s.closed_by,'Usuario histórico'),
            incoming=snap.get('incoming'),outgoing=snap.get('outgoing'),additions=snap.get('additions'),expected=str(s.closing_expected),counted=str(s.counted),
            difference=str(diff),result='EXACTO' if diff==0 else ('SOBRANTE' if diff>0 else 'FALTANTE'))
    return data
