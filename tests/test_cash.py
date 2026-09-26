"""Cash lifecycle integration tests against an isolated SQLite database."""
import base64
import unittest
import uuid
from decimal import Decimal
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from zipfile import ZipFile
from tests import test_api as legacy
from app.core.database import SessionLocal
from app.models.payment import Payment
from app.models.cash import CashCustodyTransfer, CashMovement, CashSession
from app.services.cash_service import today

class CashTests(unittest.TestCase):
    setUpClass = classmethod(legacy.MoneyFastApiTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.MoneyFastApiTests.tearDownClass.__func__)
    register_owner = legacy.MoneyFastApiTests.register_owner
    _set_company_unlimited = legacy.MoneyFastApiTests._set_company_unlimited
    login = legacy.MoneyFastApiTests.login
    auth_headers = legacy.MoneyFastApiTests.auth_headers
    owner_session = legacy.MoneyFastApiTests.owner_session
    create_customer = legacy.MoneyFastApiTests.create_customer
    create_loan = legacy.MoneyFastApiTests.create_loan

    def req(self,path,body=None,headers=None,code=200,method=None):
        r=self.client.request(method or ('POST' if body is not None else 'GET'),'/api/v1'+path,json=body,headers=headers or self.admin)
        self.assertEqual(r.status_code,code,r.text)
        return r.json() if r.content else None

    def setUp(self):
        legacy.MoneyFastApiTests.setUp(self)
        self.admin=self.owner_session()
        self.branch=self.req('/branches',dict(name='Central',address='Calle Centro 1',manager_name='Gerente QA',notary_name='Notario QA',phone='8095555555'),code=201)['id']
        self.other=self.req('/branches',dict(name='Norte',address='Calle Norte 1',manager_name='Gerente QA',notary_name='Notario QA',phone='8095555555'),code=201)['id']
        self.roles={}
        self.users={}
        for role in ('cashier','collector','manager'):
            u=self.req('/users',dict(full_name=role+' QA',email=role+'@example.com',password='workerpass123',role=role,branch_id=self.branch),code=201)
            self.users[role]=u
            self.roles[role]=self.auth_headers(self.login(role+'@example.com','workerpass123')['access_token'])
        self.customer=self.create_customer(self.admin,collector_id=self.users['collector']['id'])
        self.loan=self.create_loan(self.admin,self.customer['id'])
        for bid in (self.branch,self.other): self.req('/cash/setup',dict(branch_id=bid,initial_balance='1000',notes='Prueba inicial'))
        self.req('/cash/activate',{})
        self.bank_account=self.req('/bank-accounts',dict(bank_name='Banco QA',account_number='1234567890',account_holder='Empresa QA'),code=201)

    def workspace(self,headers=None): return self.req(f'/cash/workspace?branch_id={self.branch}',headers=headers)
    def cmd(self,action,headers=None,code=200,**fields):
        return self.req('/cash/commands',dict(action=action,branch_id=self.branch,idempotency_key=str(uuid.uuid4()),**fields),headers,code)
    def payment(self,amount='1000',method='cash',origin='field',headers=None,code=201,**extra):
        body=dict(loan_id=self.loan['id'],payment_type='custom',amount=amount,method=method,origin=origin,branch_id=self.branch,idempotency_key=str(uuid.uuid4()),**extra)
        return self.req('/payments',body,headers or self.roles['collector'],code)
    def _finish_pending_close(self):
        with SessionLocal() as db:
            transfer=db.query(CashCustodyTransfer).filter(CashCustodyTransfer.kind=='closing_capital', CashCustodyTransfer.state=='pending').order_by(CashCustodyTransfer.id.desc()).first()
            if not transfer:
                return
            session=db.get(CashSession,transfer.session_id)
            session_version=session.version
            transfer_version=transfer.version
        self.cmd('confirm_closing_transfer',headers=self.admin,target_id=transfer.id,version=session_version,transfer_version=transfer_version,acceptance_id='legacy-close-'+uuid.uuid4().hex,acceptance_method='authenticated_confirmation',notes='Recepción administrativa de cierre')

    def open(self,amount='1000'):
        self._finish_pending_close()
        if Decimal(amount)>0:
            self.req('/capital/movements',dict(kind='injection',amount=amount,notes='Fondo de prueba'),code=200)
        created=self.cmd('open',target_id=self.users['cashier']['id'],amount=amount,notes='Conteo inicial')
        return self.cmd('confirm_opening',headers=self.roles['cashier'],target_id=created['session_id'],version=1,transfer_version=1,amount=amount,acceptance_id='legacy-open-'+uuid.uuid4().hex,acceptance_method='authenticated_confirmation',notes='Recibido conforme')
    def delivery(self,amount='1000'):
        return self.cmd('declare',headers=self.roles['collector'],amount=amount)['delivery_id']
    def receive(self,did,amount='600'):
        return self.cmd('receive',headers=self.roles['cashier'],target_id=did,version=1,amount=amount,notes='Entrega parcial')
    def transfer(self):
        return self.payment(method='transfer',reference_code='BANCO-QA',bank_account_id=self.bank_account['id'],proof=dict(filename='test.pdf',media_type='application/pdf',content_base64=base64.b64encode(b'%PDF-1.4 test proof').decode()))

    def test_partial_delivery_does_not_pay_twice_and_carries(self):
        self.open(); p=self.payment(); d=self.delivery(); self.receive(d)
        w=self.workspace()
        self.assertEqual(Decimal(w['pending']),400)
        self.assertEqual(Decimal(w['session']['balance']),1600)
        ps=self.req('/payments');self.assertEqual(len(ps),1);self.assertEqual(ps[0]['id'],p['id'])
        self.cmd('close',version=w['session']['version'],denominations={'1000':1,'500':1,'100':1})
        self.open('1600');self.assertEqual(Decimal(self.workspace()['pending']),400)
        self.receive(self.delivery('400'),'400')
        self.assertEqual(Decimal(self.workspace()['pending']),0)
        receipt=self.req(f'/cash/receipt/{d}?branch_id={self.branch}')
        self.assertIn('ENT-'+str(d),receipt['html'])
        self.assertIn('600',receipt['html'])

    def test_transfer_confirmation_idempotent_and_rejection(self):
        transfer=self.transfer()
        self.assertEqual(transfer['status'],'pending');self.assertEqual(len(self.req('/payments')),0)
        body=dict(action='confirm_transfer',branch_id=self.branch,target_id=transfer['transfer_id'],version=1,idempotency_key=str(uuid.uuid4()))
        result=self.req('/cash/commands',body,self.roles['cashier'])
        self.assertEqual(self.req('/cash/commands',body,self.roles['cashier']),result)
        self.assertEqual(len(self.req('/payments')),1)
        self.cmd('confirm_transfer',target_id=transfer['transfer_id'],version=1,code=409)
        second=self.transfer()
        self.cmd('reject_transfer',target_id=second['transfer_id'],version=1,notes='Referencia no encontrada')
        self.assertEqual(len(self.req('/payments')),1)
        self.assertEqual(Decimal(self.workspace()['pending']),0)

    def test_counter_expenses_insufficient_rollback_and_stale_version(self):
        self.payment(origin='counter',headers=self.roles['cashier'],code=409)
        self.open();self.payment(origin='counter',headers=self.roles['cashier'])
        w=self.workspace();v=w['session']['version']
        self.cmd('movement',kind='expense',amount='2001',notes='Gasto',version=v,code=409)
        self.assertEqual(self.workspace()['session']['version'],v)
        self.cmd('movement',kind='bank_deposit',amount='400',reference='DEP-1',notes='Deposito bancario',version=v)
        self.cmd('movement',kind='expense',amount='100',notes='Gasto',version=v,code=409)
        self.assertEqual(Decimal(self.workspace()['session']['balance']),1600)

    def test_closing_difference_review_and_immutable_snapshot(self):
        self.open();w=self.workspace()
        self.cmd('close',version=w['session']['version'],denominations={'500':1},notes='Faltante contado')
        w=self.workspace();s=w['session'];saved=s['snapshot']
        self.cmd('movement',kind='contribution',amount='100',notes='Aporte',version=s['version'],code=409)
        self.cmd('resolve',headers=self.roles['cashier'],target_id=s['id'],version=s['version'],resolution='approve',notes='No autorizado',code=403)
        self.cmd('resolve',target_id=s['id'],version=s['version'],resolution='return',notes='Recontar')
        s=self.workspace()['session']
        self.cmd('close',version=s['version'],denominations={'500':1},notes='Faltante verificado')
        s=self.workspace()['session']
        self.cmd('resolve',target_id=s['id'],version=s['version'],resolution='approve',notes='Ajuste aprobado')
        self.assertEqual(self.workspace()['sessions'][0]['snapshot'],saved)
        self.open('500');self.assertEqual(Decimal(self.workspace()['session']['balance']),500)

    def test_opening_is_independent_and_totals_remain_consistent(self):
        self.open('900');s=self.workspace()['session'];self.assertEqual(s['state'],'open');self.assertEqual(Decimal(s['opening_expected']),Decimal('0'))
        self.cmd('close',version=s['version'],denominations={'500':1,'200':2},notes='Conteo exacto')
        self._finish_pending_close()
        snap=self.workspace()['sessions'][0]['snapshot']
        self.assertEqual(Decimal(snap['opening'])+Decimal(snap['incoming'])-Decimal(snap['outgoing']),Decimal(snap['expected']))

    def test_reversal_restores_custody_not_loan(self):
        self.open();self.payment();self.receive(self.delivery())
        w=self.workspace();m=w['movements'][0]
        self.cmd('reverse',target_id=m['id'],version=w['session']['version'],notes='Corrección recepción')
        w=self.workspace();self.assertEqual(Decimal(w['pending']),1000);self.assertEqual(Decimal(w['session']['balance']),1000)
        self.assertEqual(len(self.req('/payments')),1)
        self.cmd('reverse',target_id=m['id'],version=w['session']['version'],notes='Repetido',code=409)

    def test_permissions_branches_and_company_isolation(self):
        self.req(f'/cash/workspace?branch_id={self.other}',headers=self.roles['cashier'],code=403)
        self.req('/users',headers=self.roles['cashier'],code=403)
        self.req('/loan-applications',headers=self.roles['cashier'],code=403)
        self.cmd('open',headers=self.roles['manager'],amount='1000',code=403)
        self.cmd('open',headers=self.roles['collector'],amount='1000',code=403)
        self.register_owner('other@example.com')
        other=self.auth_headers(self.login('other@example.com')['access_token'])
        self.req('/cash/setup',dict(branch_id=self.branch,initial_balance='1',notes='Intruso'),headers=other,code=404)
        self.payment(method=None,code=422)
        self.req('/loans',dict(customer_id=self.customer['id'],principal_amount='100',interest_rate='10',installment_count=2,payment_frequency='monthly',start_date='2026-12-01'),code=409)

    def test_pending_blocks_user_move_and_reassignment_keeps_original(self):
        self.payment();u=self.users['collector']
        self.req(f"/users/{u['id']}",dict(full_name=u['full_name'],email=u['email'],role='collector',is_active=True,branch_id=self.other),method='PUT',code=409)
        c=self.req(f"/customers/{self.customer['id']}")
        self.req(f"/customers/{c['id']}",{**c,'assigned_collector_id':None},method='PUT')
        self.assertEqual(Decimal(self.workspace(self.roles['collector'])['pending']),1000)

    def test_simultaneous_open_and_identical_payment(self):
        def opening(_):
            return self.client.post('/api/v1/cash/commands',json=dict(action='open',branch_id=self.branch,amount='1000',idempotency_key=str(uuid.uuid4())),headers=self.admin).status_code
        with ThreadPoolExecutor(2) as pool: self.assertEqual(sorted(pool.map(opening,range(2))),[200,409])
        payload=dict(loan_id=self.loan['id'],payment_type='custom',amount='1000',method='cash',origin='field',branch_id=self.branch,idempotency_key=str(uuid.uuid4()))
        def pay(_):return self.client.post('/api/v1/payments',json=payload,headers=self.roles['collector'])
        with ThreadPoolExecutor(2) as pool: responses=list(pool.map(pay,range(2)))
        self.assertEqual([r.status_code for r in responses],[201,201])
        self.assertEqual(responses[0].json()['id'],responses[1].json()['id'])
        self.assertEqual(len(self.req('/payments')),1)

    def test_report_filters_pending_and_xlsx(self):
        self.transfer();self.open();self.payment();self.delivery()
        query=f'/cash/report?branch_id={self.branch}&start={today()}&end={today()}&state=pending'
        rows=self.req(query)['rows'];self.assertEqual(len(rows),2)
        r=self.client.get('/api/v1'+query+'&format=xlsx',headers=self.admin)
        self.assertEqual(r.status_code,200,r.text[:50] if r.status_code!=200 else '')
        with ZipFile(BytesIO(r.content)) as z:self.assertIn('pending',z.read('xl/worksheets/sheet1.xml').decode())
        self.assertEqual(self.req(query.replace(str(today()),str(today()-timedelta(days=1))))['rows'],[])

    def test_historical_cash_is_excluded(self):
        with SessionLocal() as db:
            p=Payment(loan_id=self.loan['id'],collected_by_id=self.users['collector']['id'],amount=100,principal_applied=100,interest_applied=0,late_fee_applied=0,payment_type='custom',branch_id=self.branch,cash_state='historical')
            db.add(p);db.commit()
        self.assertEqual(Decimal(self.workspace()['pending']),0)

    def signed_application(self,amount='500'):
        from app.models.loan_application import LoanApplication
        with SessionLocal() as db:
            a=LoanApplication(company_id=self.users['cashier']['company_id'],created_by_id=self.users['cashier']['id'],customer_id=self.customer['id'],modality='unsecured',status='signed',data={'full_name':'Cliente QA','requested_amount':amount,'payment_frequency':'Mensual'},terms={'interest_rate':'10','installment_count':4,'late_fee_rate':'0','grace_days':0})
            db.add(a);db.commit();return a.id

    def test_disbursement_atomic_cash_and_transfer(self):
        self.open();aid=self.signed_application()
        before=len(self.req('/loans'))
        result=self.cmd('disburse',target_id=aid,version=1,reference='DES-1',first_payment_date=str(today()+timedelta(days=30)))
        self.assertEqual(len(self.req('/loans')),before+1)
        self.assertEqual(len(self.req('/customers')),1)
        self.assertEqual(Decimal(self.workspace()['session']['balance']),500)
        loan=self.req('/loans/'+str(result['loan_id']));self.assertEqual(len(loan['installments']),4)
        self.cmd('disburse',target_id=aid,version=1,reference='DES-1',first_payment_date=str(today()+timedelta(days=30)),code=409)
        aid=self.signed_application('600')
        self.cmd('disburse',target_id=aid,version=1,reference='DES-2',first_payment_date=str(today()+timedelta(days=30)),code=409)
        self.assertEqual(len(self.req('/loans')),before+1)
        self.cmd('disburse',target_id=aid,version=1,method='transfer',reference='DES-2',first_payment_date=str(today()+timedelta(days=30)),proof=dict(filename='test.pdf',media_type='application/pdf',content_base64=base64.b64encode(b'%PDF-1.4 test').decode()))
        self.assertEqual(Decimal(self.workspace()['session']['balance']),500)

    def test_failure_after_loan_creation_rolls_back_everything(self):
        from unittest.mock import patch
        from app.models.loan_application import LoanApplication
        self.open();aid=self.signed_application()
        with patch('app.services.cash_service.add_movement',side_effect=RuntimeError('Simulated persistence failure')):
            with self.assertRaises(RuntimeError):self.cmd('disburse',target_id=aid,version=1,reference='DES-FAIL',first_payment_date=str(today()+timedelta(days=30)))
        self.assertEqual(len(self.req('/loans')),1)
        self.assertEqual(Decimal(self.workspace()['session']['balance']),1000)
        with SessionLocal() as db:
            a=db.get(LoanApplication,aid);self.assertEqual(a.status,'signed');self.assertEqual(a.version,1);self.assertIsNone(a.loan_id)

    def test_overpayment_rolls_back_installments(self):
        before=self.req('/loans/'+str(self.loan['id']))
        self.payment(amount='99999',code=422)
        after=self.req('/loans/'+str(self.loan['id']))
        self.assertEqual(before['principal_balance'],after['principal_balance'])
        self.assertEqual(before['installments'],after['installments'])
        self.assertEqual(len(self.req('/payments')),0)

    def test_cashier_counts_against_plan_limit(self):
        from app.models.company import Company
        from app.models.plan import Plan
        with SessionLocal() as db:
            company=db.get(Company,self.users['cashier']['company_id'])
            # A plan of its own (rather than a named catalog plan, which can change)
            # with a limit already met by the admin+manager+cashier+collector fixture.
            tight_plan=Plan(name='Plan de prueba (límite de usuarios)',customer_limit=0,loan_limit=0,user_limit=3,monthly_price_usd='9.99')
            db.add(tight_plan);db.flush()
            company.plan_id=tight_plan.id;db.commit()
        self.req('/users',dict(full_name='Otra Cajera',email='extra@example.com',password='workerpass123',role='cashier',branch_id=self.branch),code=402)

    def test_midnight_and_inclusive_local_range(self):
        from datetime import UTC,datetime,time
        from app.models.cash import CashSession
        self.open()
        with SessionLocal() as db:
            s=db.get(CashSession,self.workspace()['session']['id'])
            # UTC 03:59 is the preceding local calendar day in Santo Domingo.
            s.opened_at=datetime.combine(today(),time(3,59),UTC);s.business_date=today()-timedelta(days=1);db.commit()
        s=self.workspace()['session'];self.cmd('close',version=s['version'],denominations={'1000':1})
        report=self.req(f'/cash/report?branch_id={self.branch}&start={today()}&end={today()}&kind=sessions')
        self.assertEqual(len(report['rows']),1)
        self.assertEqual(report['rows'][0]['business_date'],str(today()-timedelta(days=1)))

    def test_surplus_requires_admin_and_never_reduces_custody(self):
        self.open();self.payment()
        excess=self.cmd('report_surplus',headers=self.roles['cashier'],target_id=self.users['collector']['id'],amount='50',notes='Billete adicional sin cobro asociado')['delivery_id']
        s=self.workspace()['session']
        self.cmd('close',version=s['version'],denominations={'1000':1,'50':1},notes='No cerrar sin revisión',code=409)
        self.cmd('confirm_surplus',headers=self.roles['cashier'],target_id=excess,version=1,notes='No autorizado',code=403)
        self.cmd('confirm_surplus',target_id=excess,version=1,notes='Sobrante verificado y registrado')
        w=self.workspace();self.assertEqual(Decimal(w['pending']),1000);self.assertEqual(Decimal(w['session']['balance']),1050)
        self.assertEqual(len(self.req('/payments')),1)

    def test_reversal_after_close_does_not_rewrite_closed_day(self):
        self.open();self.payment();self.receive(self.delivery())
        w=self.workspace();m=w['movements'][0]
        self.cmd('close',version=w['session']['version'],denominations={'1000':1,'500':1,'100':1})
        snapshot=self.workspace()['sessions'][0]['snapshot']
        self.open('1600')
        self.cmd('reverse',target_id=m['id'],version=self.workspace()['session']['version'],notes='Reverso de jornada anterior')
        w=self.workspace();self.assertEqual(w['sessions'][1]['snapshot'],snapshot)
        self.assertEqual(Decimal(w['pending']),1000);self.assertEqual(Decimal(w['session']['balance']),1000)

    def test_websocket_payment_after_commit(self):
        from unittest.mock import patch
        from app.services.cash_live import publish
        with self.client.websocket_connect('/api/v1/cash/live') as ws:
            ws.send_json(dict(token=self.admin['Authorization'].split()[1], branch_id=self.branch))
            self.assertEqual(ws.receive_json()['type'], 'ready')
            self.payment('1000')
            self.assertEqual(ws.receive_json()['type'], 'cash_changed')
            self.assertEqual(Decimal(self.workspace()['pending']), 1000)
            with patch('app.api.routes.cash.publish', wraps=publish) as notify:
                self.payment('-1', code=422)
                notify.assert_not_called()

    def test_websocket_scope_and_auth(self):
        from starlette.websockets import WebSocketDisconnect
        for token, branch, code in [('invalid', self.branch, 4401), (self.roles['cashier']['Authorization'].split()[1], self.other, 4403)]:
            with self.client.websocket_connect('/api/v1/cash/live') as ws:
                ws.send_json(dict(token=token, branch_id=branch))
                with self.assertRaises(WebSocketDisconnect) as caught:
                    ws.receive_json()
                self.assertEqual(caught.exception.code, code)

    def test_receive_collector_direct_partial_and_retry(self):
        self.open()
        self.payment('1000')
        before=self.client.get(f'/api/v1/loans/{self.loan["id"]}',headers=self.admin).json()
        payload=dict(action='receive_collector',branch_id=self.branch,target_id=self.users['collector']['id'],version=self.workspace()['session']['version'],amount='600',notes='Entrega parcial',idempotency_key=str(uuid.uuid4()))
        result=self.req('/cash/commands',payload,headers=self.roles['cashier'])
        self.assertEqual(self.req('/cash/commands',payload,headers=self.roles['cashier']),result)
        w=self.workspace()
        self.assertEqual(Decimal(w['pending']),400)
        self.assertEqual(Decimal(w['session']['balance']),1600)
        self.assertEqual(len(w['deliveries']),1)
        after=self.client.get(f'/api/v1/loans/{self.loan["id"]}',headers=self.admin).json()
        self.assertEqual(before['principal_balance'],after['principal_balance'])
        self.assertEqual(before['interest_balance'],after['interest_balance'])
        self.cmd('receive_collector',target_id=self.users['collector']['id'],version=w['session']['version'],amount='401',notes='Exceso',code=422)
        self.delivery('400')
        self.cmd('receive_collector',target_id=self.users['collector']['id'],version=self.workspace()['session']['version'],amount='400',code=409)
        self.assertEqual(Decimal(self.workspace()['pending']),400)

if __name__=='__main__':unittest.main()
