import base64
from datetime import date, datetime, time, timedelta, UTC
from html import escape
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape as xml_escape
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.branch import Branch
from app.models.customer import Customer
from app.models.loan import Loan
from app.models.payment import Payment
from app.models.loan_application import LoanApplication
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

def public(row):
    data={c.name:getattr(row,c.name) for c in row.__table__.columns if c.name not in ('proof','payload')}
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
            for u in users if u.branch_id not in ready]
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
def workspace(branch_id:int|None=None,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    box=svc.scope(db,user,branch_id)
    users={u.id:u for u in db.scalars(select(User).where(User.company_id==user.company_id)).all()}
    def enrich(row):
        result=public(row)
        if isinstance(row,CashMovement) and row.kind=='bank_disbursement' and row.loan_id:
            result['bank_amount']=str(db.get(Loan,row.loan_id).principal_amount)
        for k in ('collector_id','cashier_id','actor_id','opened_by','closed_by','reviewed_by'):
            uid=result.get(k)
            if uid: result[k+'_name']=users[uid].full_name if uid in users else 'Usuario histórico'
        return result
    mine=user.id if user.role=='collector' else None
    payments=db.scalars(select(Payment).where(Payment.branch_id==box.branch_id).order_by(Payment.id.desc())).all()
    if mine: payments=[p for p in payments if p.collected_by_id==mine]
    deliveries=db.scalars(select(CashDelivery).where(CashDelivery.box_id==box.id).order_by(CashDelivery.id.desc())).all()
    transfers=db.scalars(select(CashTransfer).where(CashTransfer.box_id==box.id).order_by(CashTransfer.id.desc())).all()
    if mine:
        deliveries=[d for d in deliveries if d.collector_id==mine]
        transfers=[t for t in transfers if t.collector_id==mine]
    outstanding=svc.outstanding_rows(db,box,mine)
    totals={}
    for p,amount in outstanding: totals[p.collected_by_id]=totals.get(p.collected_by_id,svc.ZERO)+amount
    data=dict(branch_id=box.branch_id,branch_name=db.get(Branch,box.branch_id).name,
        pending=str(svc.pending(db,box,mine)),transfers_pending=str(sum((t.amount for t in transfers if t.state=='pending'),svc.ZERO)),
        deliveries=[{**enrich(d),'allocations':[dict(payment_id=a.payment_id,amount=str(a.amount)) for a in db.scalars(select(CashAllocation).where(CashAllocation.delivery_id==d.id)).all()]} for d in deliveries],transfers=[enrich(t) for t in transfers],
        payments=[{**enrich(p),'customer_name':db.get(Customer,db.get(Loan,p.loan_id).customer_id).full_name} for p in payments],
        collectors=[dict(id=uid,name=users[uid].full_name,pending=str(amount)) for uid,amount in totals.items()],
        outstanding=[dict(payment_id=p.id,collector_id=p.collected_by_id,loan_id=p.loan_id,paid_at=p.paid_at,amount=str(p.amount),pending=str(amount)) for p,amount in outstanding])
    if not mine:
        session=svc.active_session(db,box,False)
        sessions=db.scalars(select(CashSession).where(CashSession.box_id==box.id).order_by(CashSession.id.desc())).all()
        movements=db.scalars(select(CashMovement).where(CashMovement.box_id==box.id).order_by(CashMovement.id.desc())).all()
        last=sessions[0] if sessions else None
        historical_users={m.actor_id for m in movements}|{d.collector_id for d in deliveries}|{d.cashier_id for d in deliveries}
        branch_users=[dict(id=u.id,name=u.full_name,role=u.role) for u in users.values() if u.branch_id==box.branch_id or u.role=='admin' or u.id in historical_users]
        candidates_loans=db.scalars(select(Loan).join(Customer).where(Customer.company_id==user.company_id,Loan.status.in_(['active','late']))).all()
        loans=[dict(id=l.id,name=db.get(Customer,l.customer_id).full_name,balance=str(l.principal_balance+l.interest_balance+l.late_fee_balance)) for l in candidates_loans if svc.branch_for_customer(db,db.get(Customer,l.customer_id))==box.branch_id or (user.role=='admin' and svc.branch_for_customer(db,db.get(Customer,l.customer_id)) is None)]
        data.update(users=branch_users,loans=loans,session=enrich(session) if session else None,sessions=[enrich(s) for s in sessions],movements=[enrich(m) for m in movements],
          expected_opening=str(last.counted if last and last.state=='closed' else box.initial_balance),
          audit=[enrich(a) for a in db.scalars(select(CashAudit).where(CashAudit.box_id==box.id).order_by(CashAudit.id.desc())).all()])
        candidates=db.scalars(select(LoanApplication).where(LoanApplication.company_id==user.company_id,LoanApplication.status=='signed')).all()
        data['applications']=[dict(id=a.id,version=a.version,name=a.data.get('full_name'),amount=a.data.get('requested_amount')) for a in candidates if a.customer_id and (svc.branch_for_customer(db,db.get(Customer,a.customer_id))==box.branch_id or (user.role=='admin' and svc.branch_for_customer(db,db.get(Customer,a.customer_id)) is None))]
    return jsonable_encoder(data)

def filtered_report(db,user,branch_id,start,end,collector_id,actor_id,kind,state):
    svc.require_role(user,'admin','cashier','manager')
    if start>end: svc.fail('Rango de fechas inválido.')
    data=workspace(branch_id,db,user)
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

@router.get('/proof/{kind}/{target_id}')
def proof(kind:str,target_id:int,branch_id:int|None=None,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    box=svc.scope(db,user,branch_id)
    cls={'transfer':CashTransfer,'movement':CashMovement}.get(kind)
    if cls is None:svc.fail('Documento no encontrado.',404)
    row=db.get(cls,target_id)
    if not row or row.box_id!=box.id or (user.role=='collector' and (kind!='transfer' or row.collector_id!=user.id)):svc.fail('Documento no encontrado.',404)
    if not row.proof:svc.fail('Sin comprobante.',404)
    return Response(base64.b64decode(row.proof['content_base64']),media_type=row.proof['media_type'],headers={'Content-Disposition':'attachment; filename="comprobante"','X-Content-Type-Options':'nosniff'})
