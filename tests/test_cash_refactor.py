"""Acceptance tests for independent cash custody (Capital no longer funds nor receives a session).

T-021: one active session per CashPoint (a second cashier works on a second CashPoint of the branch), openings declare
an initial amount, closes record the counted cash (the difference is a record, never a resolve/return/adjustment) and
the cash permissions are explicit."""

import base64
import unittest
import uuid
from decimal import Decimal

from tests import test_api as legacy
from app.core.database import SessionLocal
from app.models.cash import CashCustodyTransfer, CashMovement, CashSession
from app.models.capital import CapitalMovement
from app.models.customer import Customer
from app.modules.organization.models import CashPoint
from tests.cash_fixtures import grant_cash_permissions

CASHIER_PERMS = ['cash.sessions.open', 'cash.sessions.close', 'cash.sessions.read']
RECEIVER_PERMS = ['cash.handovers.accept', 'cash.sessions.read']


class CashRefactorTests(unittest.TestCase):
    setUpClass = classmethod(legacy.MoneyFastApiTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.MoneyFastApiTests.tearDownClass.__func__)
    register_owner = legacy.MoneyFastApiTests.register_owner
    _set_company_unlimited = legacy.MoneyFastApiTests._set_company_unlimited
    login = legacy.MoneyFastApiTests.login
    auth_headers = legacy.MoneyFastApiTests.auth_headers
    owner_session = legacy.MoneyFastApiTests.owner_session
    create_customer = legacy.MoneyFastApiTests.create_customer
    create_loan = legacy.MoneyFastApiTests.create_loan

    def setUp(self):
        legacy.MoneyFastApiTests.setUp(self)
        self.admin = self.owner_session()
        self.branch = self.req('/branches', dict(name='Central', address='Calle Centro 1', manager_name='Gerente QA', notary_name='Notario QA', phone='8095555555'), code=201)['id']
        self.roles = {}
        self.users = {}
        for role in ('cashier', 'cashier2', 'manager'):
            api_role = 'cashier' if role.startswith('cashier') else role
            email = role + '@example.com'
            user = self.req('/users', dict(full_name=role + ' QA', email=email, password='workerpass123', role=api_role, branch_id=self.branch), code=201)
            self.users[role] = user
            self.roles[role] = self.auth_headers(self.login(email, 'workerpass123')['access_token'])
        self.admin_id = next(u['id'] for u in self.req('/users') if u['role'] == 'admin')  # name ordering is collation-dependent on PostgreSQL
        # This test needs an existing loan before activating Caja; POST /loans
        # intentionally rejects legacy disbursements after activation.
        if self._testMethodName in ('test_composed_transfer_is_pending_and_preserves_exact_amount',
                                    'test_admin_counter_payment_requires_selected_session'):
            customer = self.create_customer(self.admin)
            self.composed_loan = self.create_loan(self.admin, customer['id'])
            with SessionLocal() as db:
                db.get(Customer, customer['id']).cash_branch_id = self.branch
                db.commit()
        self.req('/cash/setup', dict(branch_id=self.branch, initial_balance='0', notes='Configuración histórica'), code=200)
        self.req('/cash/activate', {}, code=200)
        company_id = self.users['cashier']['company_id']
        for name, perms in (('cashier', CASHIER_PERMS), ('cashier2', CASHIER_PERMS), ('manager', RECEIVER_PERMS)):
            grant_cash_permissions(company_id, self.users[name]['id'], perms, self.branch)
        with SessionLocal() as db:  # D1: a second cashier needs a second cash position of the branch
            cp2 = CashPoint(tenant_id=company_id, branch_id=self.branch, code='QA-CAJA-2', name='Caja 2', status='active',
                            origin='manual')
            db.add(cp2)
            db.commit()
            self.cash_points = {'cashier': None, 'cashier2': cp2.id}

    def req(self, path, body=None, headers=None, code=200, method=None):
        response = self.client.request(method or ('POST' if body is not None else 'GET'), '/api/v1' + path, json=body, headers=headers or self.admin)
        self.assertEqual(response.status_code, code, response.text)
        return response.json() if response.content else None

    def cmd(self, action, headers=None, code=200, **fields):
        if action == 'close' and 'receiver_id' not in fields and 'target_id' in fields:
            fields['receiver_id'] = fields.pop('target_id')
        if action in ('movement', 'close', 'reverse') and fields.get('version') == 2 and 'session_id' not in fields:
            with SessionLocal() as db:
                open_sessions = db.query(CashSession).filter(CashSession.box_id == self.branch, CashSession.state == 'open').all()
                if len(open_sessions) == 1:
                    fields['version'] = open_sessions[0].version
        return self.req('/cash/commands', dict(action=action, branch_id=self.branch, idempotency_key=str(uuid.uuid4()), **fields), headers, code)

    def open_for(self, cashier='cashier', amount='1000'):
        # FM-SIMPLE-CASH-010: the cash that enters is the amount the cashier declares (Capital funds nothing)
        result = self.cmd('open', headers=self.roles[cashier], amount=amount, opening_mode='initial',
                          cash_point_id=self.cash_points[cashier], notes='Monto inicial declarado')
        with SessionLocal() as db:
            result['version'] = db.get(CashSession, result['session_id']).version
        return result

    def workspace(self, headers=None):
        return self.req(f'/cash/workspace?branch_id={self.branch}', headers=headers or self.roles['cashier'])

    def test_finance_report_sums_each_cashier_and_keeps_the_counted_cash_of_a_closed_day(self):
        """Open-session cash is summed per cashier; a closed day keeps its counted cash visible (it never vanishes)."""
        from app.services.cash_service import today

        self.open_for('cashier', '800')
        self.open_for('cashier2', '600')
        path = f'/reports/finance?start={today()}&end={today()}'
        report = self.req(path)
        self.assertEqual(Decimal(report['efectivo_caja']), Decimal('1400.00'))
        self.assertEqual(Decimal(report['efectivo_cerrado']), Decimal('0.00'))

        current = self.workspace(self.roles['cashier'])['session']
        closed = self.cmd('close', headers=self.roles['cashier'], version=current['version'], counted='800')
        self.assertEqual(closed['state'], 'closed')
        after = self.req(path)
        self.assertEqual(Decimal(after['efectivo_caja']), Decimal('600.00'))
        self.assertEqual(Decimal(after['efectivo_cerrado']), Decimal('800.00'))  # counted at the close, still reported

    def test_finance_report_uses_the_physical_count_for_a_shortfall(self):
        """A closing difference is a record: the report shows the PHYSICAL count, never the expected figure."""
        from app.services.cash_service import today

        self.open_for('cashier', '900')
        session = self.workspace()['session']
        closed = self.cmd('close', headers=self.roles['cashier'], version=session['version'], counted='700',
                          notes='Faltante de 200')
        self.assertEqual((closed['state'], Decimal(closed['difference'])), ('closed', Decimal('-200')))
        report = self.req(f'/reports/finance?start={today()}&end={today()}')
        self.assertEqual(Decimal(report['efectivo_caja']), Decimal('0.00'))
        self.assertEqual(Decimal(report['efectivo_cerrado']), Decimal('700.00'))

    def test_composed_transfer_is_pending_and_preserves_exact_amount(self):
        """A bank transfer with explicit split components does not credit the loan early."""
        loan = self.composed_loan
        account = self.req('/bank-accounts', dict(
            bank_name='Banco QA', account_number='1234567890',
            account_holder='Empresa QA',
        ), code=201)
        body = dict(
            loan_id=loan['id'], installment_amount='200.00',
            principal_amount='50.00', interest_amount='0.00',
            custom_amount='0.00', method='transfer', origin='field',
            branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
            reference_code='COMP-QA', bank_account_id=account['id'],
            proof=dict(filename='test.pdf', media_type='application/pdf',
                       content_base64=base64.b64encode(b'%PDF-1.4 test proof').decode()),
        )
        pending = self.req('/payments', body, code=201)
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(Decimal(pending['amount']), Decimal('250.00'))
        self.assertEqual(self.req('/payments'), [])
        # Replaying the same request must not create another transfer.
        self.assertEqual(self.req('/payments', body, code=201), pending)
        self.cmd('confirm_transfer', target_id=pending['transfer_id'], version=1)
        payments = self.req('/payments')
        self.assertEqual(len(payments), 1)
        self.assertEqual(Decimal(payments[0]['amount']), Decimal('250.00'))

    def test_admin_must_choose_cashier_session_when_two_are_open(self):
        """Admin movements must never silently target the newest cashier."""
        first = self.open_for('cashier', '800')
        second = self.open_for('cashier2', '600')
        # T-021: a capital opening leaves the session at version 2 (the fund movement); a stale version is refused
        # and nothing moves; the explicit session with its current version is the only accepted target.
        self.cmd('movement', headers=self.roles['cashier'], kind='expense', amount='100', notes='QA expense',
                 version=first['version'] - 1, code=409)
        self.cmd('movement', headers=self.roles['cashier'], kind='expense', amount='100', notes='QA expense',
                 version=first['version'], session_id=first['session_id'])
        with SessionLocal() as db:
            first_row = db.get(CashSession, first['session_id'])
            second_row = db.get(CashSession, second['session_id'])
            self.assertEqual(first_row.balance, Decimal('700.00'))
            self.assertEqual(second_row.balance, Decimal('600.00'))

    def test_admin_counter_payment_requires_selected_session(self):
        """Even counter payments cannot enter the wrong cashier's cash balance."""
        first = self.open_for('cashier', '800')
        second = self.open_for('cashier2', '600')
        request = dict(
            loan_id=self.composed_loan['id'], payment_type='interest_only',
            amount='100.00', method='cash', origin='counter',
            branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
        )
        self.req('/payments', request, headers=self.admin, code=403)
        request['idempotency_key'] = str(uuid.uuid4())
        request['session_id'] = first['session_id']
        confirmed = self.req('/payments', request, headers=self.roles['cashier'], code=201)
        with SessionLocal() as db:
            first_row = db.get(CashSession, first['session_id'])
            second_row = db.get(CashSession, second['session_id'])
            self.assertEqual(first_row.balance, Decimal('900.00'))
            self.assertEqual(second_row.balance, Decimal('600.00'))
            movement = db.query(CashMovement).filter(
                CashMovement.payment_id == confirmed['id'],
            ).one()
            self.assertEqual(movement.session_id, first['session_id'])

    def test_cashier_cannot_select_another_cashiers_session(self):
        first = self.open_for('cashier', '800')
        second = self.open_for('cashier2', '600')
        self.cmd('movement', headers=self.roles['cashier'], kind='expense',
                 amount='100', notes='Invalid cross-custody', version=2,
                 session_id=second['session_id'], code=409)
        with SessionLocal() as db:
            self.assertEqual(db.get(CashSession, first['session_id']).balance, Decimal('800.00'))
            self.assertEqual(db.get(CashSession, second['session_id']).balance, Decimal('600.00'))

    def test_simple_close_records_a_shortfall_without_any_handover(self):
        opened = self.open_for('cashier', '900')
        session_id = opened['session_id']
        closed = self.cmd('close', headers=self.roles['cashier'], version=opened['version'], counted='700',
                          notes='Faltante bajo revisión')
        self.assertEqual(closed['state'], 'closed')
        refused = self.cmd('resolve', target_id=session_id, version=opened['version'], resolution='approve',
                           notes='Faltante autorizado', code=409)
        self.assertEqual(refused['error']['code'], 'difference_review_not_available')
        with SessionLocal() as db:  # the counted cash is recorded on the session; nothing is handed over anywhere
            row = db.get(CashSession, session_id)
            self.assertEqual((row.counted, row.difference), (Decimal('700.00'), Decimal('-200.00')))
            self.assertEqual(db.query(CashCustodyTransfer).filter(CashCustodyTransfer.session_id == session_id).count(), 0)

    def test_opening_declares_an_initial_amount_and_never_touches_capital(self):
        anonymous = self.cmd('open', headers=self.roles['cashier'], amount='700', denominations={'500': 1, '200': 1},
                             notes='Efectivo sin origen', code=422)  # no anonymous opening cash
        self.assertIn('monto inicial', anonymous['detail'])
        self.cmd('open', headers=self.roles['cashier'], amount='700', capital=True, notes='Con capital', code=422)
        result = self.open_for(amount='700')
        self.assertEqual((result['state'], result['opening_mode']), ('open', 'initial'))
        self.assertIsNone(result['transfer_id'])
        with SessionLocal() as db:
            session = db.get(CashSession, result['session_id'])
            self.assertEqual(session.opening_counted, Decimal('700.00'))
            self.assertEqual(session.balance, Decimal('700.00'))
            fund = db.query(CashMovement).filter(CashMovement.session_id == session.id).one()
            self.assertEqual((fund.kind, fund.amount), ('opening_initial_fund', Decimal('700.00')))
            self.assertEqual(db.query(CapitalMovement).count(), 0)

    def test_admin_is_supervisor_not_cashier_operator(self):
        denied = self.cmd('open', amount='1000', notes='Admin should not own a drawer', code=403)
        self.assertEqual(denied['detail'], 'No tienes permiso para esta operación de caja.')
        first = self.open_for('cashier', '51000')
        second = self.open_for('cashier2', '100000')
        workspace = self.workspace(self.admin)
        self.assertIsNone(workspace['session'])
        active = workspace['active_sessions']
        self.assertEqual({row['session_id'] if 'session_id' in row else row['id'] for row in active}, {first['session_id'], second['session_id']})
        self.assertEqual(sum(Decimal(row['balance']) for row in active), Decimal('151000.00'))

    def test_two_cashiers_have_independent_sessions(self):
        first = self.open_for('cashier', '800')
        second = self.open_for('cashier2', '600')
        self.assertNotEqual(first['session_id'], second['session_id'])
        with SessionLocal() as db:
            sessions = db.query(CashSession).filter(CashSession.box_id == 1).all()
            self.assertEqual({s.cashier_id for s in sessions}, {self.users['cashier']['id'], self.users['cashier2']['id']})

    def test_admin_cannot_close_a_cashiers_session_and_the_cashier_closes_its_own(self):
        opened = self.open_for('cashier', '1000')
        session_id = opened['session_id']
        # the administration supervises: it never closes (nor counts) a cashier's drawer
        self.cmd('close', headers=self.admin, session_id=session_id, version=opened['version'], counted='1000', code=403)
        closed = self.cmd('close', headers=self.roles['cashier'], session_id=session_id, version=opened['version'],
                          counted='1000', notes='Cuadre exacto')
        self.assertEqual((closed['state'], Decimal(closed['difference'])), ('closed', Decimal('0')))
        with SessionLocal() as db:
            self.assertEqual(db.query(CashCustodyTransfer).filter(CashCustodyTransfer.session_id == session_id).count(), 0)

    def test_finance_report_expenses_result_branch_scope_and_period(self):
        from app.services.cash_service import today

        opened = self.open_for('cashier', '1000')
        self.cmd('movement', headers=self.roles['cashier'], kind='expense', amount='120', notes='Gasto de papelería',
                 version=opened['version'])
        report = self.req('/reports/finance?period=day')
        # the period is resolved by the SERVER in the company's calendar (never the browser's)
        self.assertEqual((report['start'], report['end']), (str(today()), str(today())))
        self.assertEqual(Decimal(report['gastos_caja']), Decimal('120.00'))
        self.assertEqual(Decimal(report['efectivo_caja']), Decimal('880.00'))
        self.assertEqual(
            Decimal(report['resultado_neto']),
            Decimal(report['ganancia_cobrada']) - Decimal(report['gastos_caja']) - Decimal(report['nomina_pagada']),
        )
        self.assertEqual(report['currency_symbol'], 'RD$')
        # the branch manager only ever sees its own branch; the administrator may narrow by branch
        mine = self.req('/reports/finance?period=day', headers=self.roles['manager'])
        self.assertEqual(mine['branch_id'], self.branch)
        self.assertEqual(self.req(f'/reports/finance?period=day&branch_id={self.branch}')['branch_id'], self.branch)
        # validation: unknown branch, unknown period and missing range
        self.req('/reports/finance?period=day&branch_id=999999', code=404)
        self.req('/reports/finance?period=bogus', code=422)
        self.req('/reports/finance', code=422)
