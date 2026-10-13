"""FM-SIMPLE-CASH-010: the simple cash day (initial opening, direct close, tickets), on the disposable PostgreSQL.

A session opened with ``opening_mode = initial`` declares its amount (zero allowed), never touches Capital, closes
``open -> closed`` with the counted cash whatever the difference, and its tickets are built from persisted data only.
The legacy flow (capital opening + denomination count + handover) is covered by tests/test_cash.py and stays untouched;
here only its compatibility with the new fields is pinned.
"""
import unittest
import uuid
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import SessionLocal
from app.models.capital import CapitalMovement
from app.models.cash import CashMovement, CashSession, CashSessionDifference
from app.modules.cash import simple_ddl
from tests.cash_fixtures import denominations_for, grant_cash_permissions
from tests import test_cash as _cash

CASHIER_PERMS = _cash.CASHIER_PERMS


class SimpleCashTests(_cash.CashTests):
    """Reuses the cash harness (owner, branches, cashier/collector/manager) of tests/test_cash.py."""

    # the inherited test methods belong to test_cash.py; they must not run twice from this class
    for _name in [n for n in dir(_cash.CashTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def simple_open(self,amount='1500',headers=None,key=None,code=200,**extra):
        body=dict(action='open',branch_id=self.branch,idempotency_key=key or str(uuid.uuid4()),opening_mode='initial',amount=amount,**extra)
        return self.req('/cash/commands',body,headers or self.roles['cashier'],code)

    def simple_close(self,counted,version=None,headers=None,key=None,code=200,notes='',session_id=None):
        if version is None: version=self.workspace(headers)['session']['version']
        body=dict(action='close',branch_id=self.branch,idempotency_key=key or str(uuid.uuid4()),version=version,counted=counted,notes=notes)
        if session_id: body['session_id']=session_id
        return self.req('/cash/commands',body,headers or self.roles['cashier'],code)

    def capital_state(self):
        with SessionLocal() as db:
            n=db.scalar(select(func.count()).select_from(CapitalMovement))
            return n

    def movements(self,session_id):
        with SessionLocal() as db:
            return [(m.kind,Decimal(m.amount)) for m in db.scalars(select(CashMovement).where(CashMovement.session_id==session_id).order_by(CashMovement.id)).all()]

    def differences(self,session_id):
        with SessionLocal() as db:
            return [(d.phase,Decimal(d.expected),Decimal(d.counted),Decimal(d.difference)) for d in db.scalars(select(CashSessionDifference).where(CashSessionDifference.session_id==session_id)).all()]

    def ticket(self,session_id,kind,headers=None,code=200,branch=None):
        return self.req(f'/cash/sessions/{session_id}/ticket?kind={kind}&branch_id={branch or self.branch}',headers=headers or self.roles['cashier'],code=code)

    # -- opening ------------------------------------------------------------------------------------------------
    def test_open_with_zero(self):
        before=self.capital_state()
        out=self.simple_open('0')
        s=out['session']
        self.assertEqual((s['state'],s['opening_source'],Decimal(s['balance']),Decimal(s['opening_expected'])),('open','initial',0,0))
        self.assertEqual(self.movements(out['session_id']),[])
        self.assertEqual(self.capital_state(),before)

    def test_open_with_positive_amount_creates_one_fund_and_no_capital(self):
        before=self.capital_state()
        out=self.simple_open('2500.50')
        s=out['session']
        self.assertEqual((Decimal(s['balance']),Decimal(s['opening_counted'])),(Decimal('2500.50'),Decimal('2500.50')))
        self.assertEqual(self.movements(out['session_id']),[('opening_initial_fund',Decimal('2500.50'))])
        self.assertEqual(self.capital_state(),before,'a simple opening must not create capital movements')
        self.assertEqual(self.differences(out['session_id']),[])
        w=self.workspace()
        self.assertEqual(w['session']['id'],out['session_id'])

    def test_second_opening_is_refused_and_same_key_replays_one_session(self):
        key=str(uuid.uuid4())
        first=self.simple_open('500',key=key)
        replay=self.simple_open('500',key=key)
        self.assertEqual(replay['session_id'],first['session_id'])
        self.assertEqual(len([s for s in self.workspace()['sessions']]),1)
        self.simple_open('500',code=409)  # a different key while one is open: the cash point is busy
        self.assertEqual(len(self.workspace()['sessions']),1)
        self.simple_open('700',key=key,code=409)  # same key, other content
        self.assertEqual(self.movements(first['session_id']),[('opening_initial_fund',Decimal('500'))])

    def test_opening_rejects_negative_and_capital_mix(self):
        self.simple_open('-1',code=422)
        self.simple_open('10',capital=True,code=422)
        self.assertEqual(self.workspace()['active_sessions'],[])

    # -- closing ------------------------------------------------------------------------------------------------
    def test_exact_close(self):
        out=self.simple_open('1000')
        closed=self.simple_close('1000')
        s=closed['session']
        self.assertEqual((s['state'],Decimal(s['difference']),Decimal(s['counted']),Decimal(s['closing_expected'])),('closed',0,1000,1000))
        self.assertIsNone(s['handover'])
        self.assertEqual(self.differences(out['session_id']),[])
        self.assertEqual(self.movements(out['session_id']),[('opening_initial_fund',Decimal('1000'))])

    def test_close_with_shortage_is_allowed_and_recorded_without_adjustment(self):
        out=self.simple_open('1000')
        closed=self.simple_close('940.25')
        self.assertEqual((closed['session']['state'],Decimal(closed['session']['difference'])),('closed',Decimal('-59.75')))
        self.assertEqual(self.differences(out['session_id']),[('closing',Decimal('1000'),Decimal('940.25'),Decimal('-59.75'))])
        self.assertEqual(self.movements(out['session_id']),[('opening_initial_fund',Decimal('1000'))],'no balancing movement')

    def test_close_with_surplus_is_allowed_and_recorded(self):
        out=self.simple_open('1000')
        closed=self.simple_close('1100',notes='Sobró un billete')
        self.assertEqual((closed['session']['state'],Decimal(closed['session']['difference'])),('closed',Decimal('100')))
        self.assertEqual(self.differences(out['session_id']),[('closing',Decimal('1000'),Decimal('1100'),Decimal('100'))])

    def test_close_requires_counted_and_a_valid_amount(self):
        self.simple_open('300')
        version=self.workspace()['session']['version']
        self.req('/cash/commands',dict(action='close',branch_id=self.branch,idempotency_key=str(uuid.uuid4()),version=version),self.roles['cashier'],422)
        self.simple_close('-5',version=version,code=422)
        self.assertEqual(self.workspace()['session']['state'],'open')

    def test_close_is_idempotent_and_not_repeatable(self):
        out=self.simple_open('400')
        version=self.workspace()['session']['version']
        key=str(uuid.uuid4())
        first=self.simple_close('390',version=version,key=key)
        again=self.simple_close('390',version=version,key=key)
        self.assertEqual(again['session_id'],first['session_id'])
        self.assertEqual(len(self.differences(out['session_id'])),1,'a replay must not record the difference twice')
        self.simple_close('400',version=version,code=409)  # a new key on a closed session

    def test_reopen_after_close_is_independent(self):
        first=self.simple_open('800')
        self.simple_close('790')
        before=self.capital_state()
        second=self.simple_open('0')
        self.assertNotEqual(first['session_id'],second['session_id'])
        self.assertEqual(Decimal(second['session']['balance']),0)
        self.assertEqual(self.capital_state(),before)
        closed=self.req(f'/cash/workspace?branch_id={self.branch}',headers=self.roles['cashier'])['sessions']
        old=[s for s in closed if s['id']==first['session_id']][0]
        self.assertEqual((old['state'],Decimal(old['difference'])),('closed',Decimal('-10')))

    # -- expected = opening + inflows - outflows ------------------------------------------------------------------
    def test_expected_matches_operations_and_nothing_is_counted_twice(self):
        cap0=self.capital_state()
        out=self.simple_open('1000')
        sid=out['session_id']
        self.payment(amount='900')                                   # collector collects in the field
        did=self.delivery('900')
        self.receive(did,'900')                                      # the cashier receives it: +900
        w=self.workspace()
        self.cmd('movement',headers=self.roles['cashier'],kind='expense',amount='150',notes='Papelería',version=w['session']['version'])
        w=self.workspace()
        self.assertEqual(Decimal(w['session']['balance']),Decimal('1750'))
        closed=self.simple_close('1750')
        snap=self.snapshot(sid)
        self.assertEqual((snap['opening'],snap['incoming'],snap['outgoing'],snap['expected'],snap['counted'],snap['difference']),
                         ('1000.00','900.00','150.00','1750.00','1750.00','0.00'))
        flows=self.movements(sid)
        self.assertEqual(sum((a for _,a in flows),Decimal(0)),Decimal('1750'),'balance is exactly the sum of its movements')
        self.assertEqual(closed['session']['difference'],'0.00')
        self.assertEqual(self.capital_state(),cap0)

    def snapshot(self,session_id):
        with SessionLocal() as db:
            return dict(db.get(CashSession,session_id).snapshot)

    def test_capital_expense_flow_is_hidden_from_simple_sessions_but_withdrawal_without_capital_works(self):
        self.simple_open('1000')
        w=self.workspace()
        self.cmd('movement',headers=self.roles['cashier'],kind='withdrawal',amount='200',notes='Retiro del dueño',version=w['session']['version'])
        self.assertEqual(Decimal(self.workspace()['session']['balance']),800)

    # -- tickets --------------------------------------------------------------------------------------------------
    def test_opening_and_closing_tickets_and_reprint(self):
        out=self.simple_open('1200')
        sid=out['session_id']
        t1=self.ticket(sid,'opening')
        self.assertEqual((t1['brand'],t1['title'],t1['initial_amount'],t1['cashier'],t1['branch'],t1['ticket_number']),
                         ('FACT MONEY','APERTURA DE CAJA','1200.00','cashier QA','Central',f'J-{sid:06d}'))
        self.assertTrue(t1['opened_at'].endswith('-04:00'),'America/Santo_Domingo local time')
        self.ticket(sid,'closing',code=409)  # not closed yet
        self.simple_close('1150')
        t2=self.ticket(sid,'closing')
        self.assertEqual((t2['title'],t2['expected'],t2['counted'],t2['difference'],t2['result'],t2['initial_amount']),
                         ('CIERRE DE CAJA','1200.00','1150.00','-50.00','FALTANTE','1200.00'))
        self.assertEqual((t2['incoming'],t2['outgoing']),('0.00','0.00'))
        self.assertEqual(self.ticket(sid,'closing'),t2,'a reprint is identical')
        self.assertEqual(self.ticket(sid,'opening'),t1)

    def test_ticket_results_exact_and_surplus(self):
        a=self.simple_open('100'); self.simple_close('100')
        self.assertEqual(self.ticket(a['session_id'],'closing')['result'],'EXACTO')
        b=self.simple_open('100'); self.simple_close('130')
        self.assertEqual(self.ticket(b['session_id'],'closing')['result'],'SOBRANTE')

    # -- roles / scope ----------------------------------------------------------------------------------------------
    def test_roles_and_scope(self):
        out=self.simple_open('600')
        sid=out['session_id']
        self.simple_open('10',headers=self.roles['collector'],code=403)
        self.simple_open('10',headers=self.roles['manager'],code=403)
        # another cashier of the same branch: cannot see the ticket and cannot close someone else's session
        other=self.req('/users',dict(full_name='cajero dos',email='cajero2@example.com',password='workerpass123',role='cashier',branch_id=self.branch),code=201)
        grant_cash_permissions(other['company_id'],other['id'],CASHIER_PERMS,self.branch)
        h2=self.auth_headers(self.login('cajero2@example.com','workerpass123')['access_token'])
        self.ticket(sid,'opening',headers=h2,code=404)
        self.simple_close('600',version=1,headers=h2,session_id=sid,code=409)  # "no te pertenece": refused, nothing written
        self.assertEqual(self.workspace()['session']['state'],'open')
        # supervision sees it; the collector never does
        self.assertEqual(self.ticket(sid,'opening',headers=self.admin)['initial_amount'],'600.00')
        self.assertEqual(self.ticket(sid,'opening',headers=self.roles['manager'])['cashier'],'cashier QA')
        self.ticket(sid,'opening',headers=self.roles['collector'],code=403)
        # a cashier of ANOTHER branch cannot read it through its own branch scope
        far=self.req('/users',dict(full_name='cajero norte',email='norte@example.com',password='workerpass123',role='cashier',branch_id=self.other),code=201)
        grant_cash_permissions(far['company_id'],far['id'],CASHIER_PERMS,self.other)
        hf=self.auth_headers(self.login('norte@example.com','workerpass123')['access_token'])
        self.ticket(sid,'opening',headers=hf,code=404,branch=self.other)
        # the cashier without the explicit permission cannot open
        nop=self.req('/users',dict(full_name='sin permiso',email='sinperm@example.com',password='workerpass123',role='cashier',branch_id=self.other),code=201)
        hn=self.auth_headers(self.login('sinperm@example.com','workerpass123')['access_token'])
        self.req('/cash/commands',dict(action='open',branch_id=self.other,idempotency_key=str(uuid.uuid4()),opening_mode='initial',amount='5'),hn,403)
        self.assertEqual(nop['role'],'cashier')

    def test_ticket_reads_never_change_the_books(self):
        out=self.simple_open('250')
        sid=out['session_id']
        before=(self.movements(sid),self.capital_state(),self.workspace()['session']['version'])
        for _ in range(3): self.ticket(sid,'opening')
        self.ticket(sid,'closing',code=409)  # the "printer failed" path: nothing to roll back, nothing written
        self.assertEqual((self.movements(sid),self.capital_state(),self.workspace()['session']['version']),before)

    # -- legacy compatibility -----------------------------------------------------------------------------------------
    def test_legacy_capital_open_and_denomination_close_still_work(self):
        out=self.open('1000')  # legacy helper: injects capital, opens with capital=true and a denomination count
        self.assertEqual(out['session']['opening_source'],'capital')
        w=self.workspace()
        closed=self.cmd('close',version=w['session']['version'],denominations=denominations_for('1000'))
        self.assertEqual(closed['session']['state'],'closing')  # still hands the cash to a receiver (capital)
        self._finish_pending_close()
        self.assertEqual(self.workspace()['session'],None)

    def test_legacy_open_without_origin_is_still_refused(self):
        self.cmd('open',headers=self.roles['cashier'],amount='300',code=422)

    def test_legacy_close_of_simple_session_needs_counted_not_denominations(self):
        self.simple_open('100')
        w=self.workspace()
        self.cmd('close',headers=self.roles['cashier'],version=w['session']['version'],denominations=denominations_for('100'),code=422)
        self.assertEqual(self.workspace()['session']['state'],'open')

    # -- v2 HTTP API (the Web uses it) --------------------------------------------------------------------------------
    def v2(self,path,body,headers=None,code=200):
        r=self.client.post('/api/v2'+path,json=body,headers=headers or self.roles['cashier'])
        self.assertEqual(r.status_code,code,r.text)
        return r.json()

    def test_v2_open_initial_and_simple_close(self):
        with SessionLocal() as db:
            from app.models.cash import CashBox
            from app.modules.organization.models import CashPoint
            box=db.scalar(select(CashBox).where(CashBox.branch_id==self.branch))
            cp=db.scalar(select(CashPoint).where(CashPoint.box_id==box.id)).id
        before=self.capital_state()
        opened=self.v2('/cash/sessions/open',dict(idempotency_key=str(uuid.uuid4()),cash_point_id=cp,source='initial',amount='300'))
        self.assertEqual((opened['opening_source'],opened['balance'],opened['state']),('initial','300.00','open'))
        self.v2('/cash/sessions/%d/close'%opened['id'],dict(idempotency_key=str(uuid.uuid4()),counted='310.00',denominations={'100':3,'10':1}),code=422)
        self.v2('/cash/sessions/%d/close'%opened['id'],dict(idempotency_key=str(uuid.uuid4())),code=422)
        closed=self.v2('/cash/sessions/%d/close'%opened['id'],dict(idempotency_key=str(uuid.uuid4()),counted='310.00',observation_note='Sobró un billete de diez'))
        self.assertEqual((closed['state'],closed['difference'],closed['handover']),('closed','10.00',None))
        self.assertEqual(self.capital_state(),before)

    def test_v2_legacy_denomination_close_is_still_required_for_legacy_sessions(self):
        out=self.cmd('open',headers=self.roles['cashier'],amount='0')  # zero opening (legacy command)
        sid=out['session_id']
        self.v2('/cash/sessions/%d/close'%sid,dict(idempotency_key=str(uuid.uuid4()),counted='0'),code=422)  # not an initial session: the simple close is refused
        closed=self.v2('/cash/sessions/%d/close'%sid,dict(idempotency_key=str(uuid.uuid4()),denominations={'1':0}))
        self.assertEqual(closed['state'],'closed')

    # -- database backstops -----------------------------------------------------------------------------------------
    def test_database_refuses_forged_simple_states(self):
        out=self.simple_open('100')
        sid=out['session_id']
        with SessionLocal() as db:
            with self.assertRaises(DBAPIError):  # a simple session never enters "closing"
                db.execute(text("UPDATE cash_sessions SET state='closing', counted=100, closing_expected=100, difference=0, "
                                "close_contract='v2', closed_by=opened_by, closed_at=now(), close_idempotency_key='x-forged-1', "
                                "denominations='{}'::json WHERE id=:i"),{'i':sid})
            db.rollback()
            with self.assertRaises(DBAPIError):  # an initial fund that does not equal the declared amount
                db.execute(text("INSERT INTO cash_movements(box_id,session_id,kind,amount,actor_id,notes,reference,created_at) "
                                "SELECT box_id,id,'opening_initial_fund',5,opened_by,'x','x',now() FROM cash_sessions WHERE id=:i"),{'i':sid})
            db.rollback()

    def test_function_text_matches_migration_copy(self):
        import importlib.util, pathlib
        path=pathlib.Path(__file__).resolve().parents[1]/'alembic'/'versions'/'0023_simple_cash_day.py'
        spec=importlib.util.spec_from_file_location('m0023',path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        self.assertEqual(mod.SIMPLE_FUNCTIONS,simple_ddl.FUNCTIONS)
        from app.modules.cash import handover_ddl as h
        self.assertEqual(mod.PREVIOUS_FUNCTIONS,[h.SESSION_INSERT_FN,h.SESSION_UPDATE_FN,h.SESSION_CONSISTENCY_FN,h.MOVEMENT_INSERT_FN])


if __name__ == '__main__':
    unittest.main()
