"""Acceptance tests for independent custody and mandatory capital transfer."""

import base64
import unittest
import uuid
from decimal import Decimal
from unittest.mock import patch

from tests import test_api as legacy
from app.core.database import SessionLocal
from app.models.cash import CashCustodyTransfer, CashMovement, CashSession
from app.models.capital import CapitalMovement


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
        # This test needs an existing loan before activating Caja; POST /loans
        # intentionally rejects legacy disbursements after activation.
        if self._testMethodName == 'test_composed_transfer_is_pending_and_preserves_exact_amount':
            customer = self.create_customer(self.admin)
            self.composed_loan = self.create_loan(self.admin, customer['id'])
        self.req('/cash/setup', dict(branch_id=self.branch, initial_balance='0', notes='Configuración histórica'), code=200)
        self.req('/cash/activate', {}, code=200)
        self.req('/capital/movements', dict(kind='injection', amount='5000', notes='Fondo de prueba'), code=200)

    def req(self, path, body=None, headers=None, code=200, method=None):
        response = self.client.request(method or ('POST' if body is not None else 'GET'), '/api/v1' + path, json=body, headers=headers or self.admin)
        self.assertEqual(response.status_code, code, response.text)
        return response.json() if response.content else None

    def cmd(self, action, headers=None, code=200, **fields):
        return self.req('/cash/commands', dict(action=action, branch_id=self.branch, idempotency_key=str(uuid.uuid4()), **fields), headers, code)

    def open_for(self, cashier='cashier', amount='1000'):
        created = self.cmd('open', target_id=self.users[cashier]['id'], amount=amount, notes='Fondo entregado')
        return self.cmd('confirm_opening', headers=self.roles[cashier], target_id=created['session_id'], version=1, transfer_version=1, amount=amount, acceptance_id='accept-' + uuid.uuid4().hex, acceptance_method='authenticated_confirmation', notes='Recibido conforme')

    def workspace(self, headers=None):
        return self.req(f'/cash/workspace?branch_id={self.branch}', headers=headers)

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
        # Both newly opened sessions are at version 2, so optimistic version
        # validation alone cannot distinguish them.
        self.cmd('movement', kind='expense', amount='100', notes='QA expense',
                 version=2, code=409)
        self.cmd('movement', kind='expense', amount='100', notes='QA expense',
                 version=2, session_id=first['session_id'])
        with SessionLocal() as db:
            first_row = db.get(CashSession, first['session_id'])
            second_row = db.get(CashSession, second['session_id'])
            self.assertEqual(first_row.balance, Decimal('700.00'))
            self.assertEqual(second_row.balance, Decimal('600.00'))

    def test_cashier_cannot_select_another_cashiers_session(self):
        first = self.open_for('cashier', '800')
        second = self.open_for('cashier2', '600')
        self.cmd('movement', headers=self.roles['cashier'], kind='expense',
                 amount='100', notes='Invalid cross-custody', version=2,
                 session_id=second['session_id'], code=409)
        with SessionLocal() as db:
            self.assertEqual(db.get(CashSession, first['session_id']).balance, Decimal('800.00'))
            self.assertEqual(db.get(CashSession, second['session_id']).balance, Decimal('600.00'))

    def test_resolving_shortfall_uses_explicit_physical_receiver(self):
        opened = self.open_for('cashier', '900')
        session_id = opened['session_id']
        pending = self.cmd('close', headers=self.roles['cashier'],
                           target_id=self.users['manager']['id'], version=2,
                           denominations={'500': 1, '200': 1},
                           notes='Shortfall under review')
        self.assertEqual(pending['state'], 'closing_review')
        with SessionLocal() as db:
            version = db.get(CashSession, session_id).version
        resolved = self.cmd('resolve', target_id=session_id, version=version,
                            resolution='approve', receiver_id=self.users['manager']['id'],
                            notes='Authorized shortfall')
        self.assertEqual(resolved['state'], 'closing_transfer_pending')
        with SessionLocal() as db:
            transfer = db.query(CashCustodyTransfer).filter(
                CashCustodyTransfer.session_id == session_id,
                CashCustodyTransfer.kind == 'closing_capital',
            ).one()
            self.assertEqual(transfer.to_user_id, self.users['manager']['id'])
            self.assertEqual(transfer.amount, Decimal('700.00'))

    def test_opening_is_independent_and_moves_capital_once(self):
        result = self.open_for(amount='0')
        self.assertEqual(result['state'], 'open')
        with SessionLocal() as db:
            self.assertEqual(db.query(CapitalMovement).filter(CapitalMovement.kind == 'to_cash').count(), 0)

        self.req('/capital/movements', dict(kind='injection', amount='2000', notes='Segundo fondo'), code=200)
        result = self.open_for(cashier='cashier2', amount='700')
        self.assertEqual(result['state'], 'open')
        with SessionLocal() as db:
            rows = db.query(CapitalMovement).filter(CapitalMovement.kind == 'to_cash').all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].amount, Decimal('700.00'))

    def test_two_cashiers_have_independent_sessions(self):
        first = self.open_for('cashier', '800')
        second = self.open_for('cashier2', '600')
        self.assertNotEqual(first['session_id'], second['session_id'])
        with SessionLocal() as db:
            sessions = db.query(CashSession).filter(CashSession.box_id == 1).all()
            self.assertEqual({s.cashier_id for s in sessions}, {self.users['cashier']['id'], self.users['cashier2']['id']})

    def test_close_requires_physical_confirmation_and_transfers_total(self):
        self.open_for('cashier', '1000')
        session = self.workspace()['session']
        pending = self.cmd('close', headers=self.roles['cashier'], target_id=self.users['manager']['id'], version=session['version'], denominations={'1000': 1}, notes='Conteo exacto')
        self.assertEqual(pending['state'], 'closing_transfer_pending')
        pending_session = self.workspace()['session']
        with SessionLocal() as db:
            self.assertEqual(db.query(CapitalMovement).filter(CapitalMovement.kind == 'from_cash').count(), 0)
        confirmed = self.cmd('confirm_closing_transfer', headers=self.roles['manager'], target_id=pending['transfer_id'], version=pending_session['version'], transfer_version=1, acceptance_id='close-' + uuid.uuid4().hex, acceptance_method='authenticated_confirmation', notes='Recibido para capital')
        self.assertEqual(confirmed['state'], 'closed')
        with SessionLocal() as db:
            capital = db.query(CapitalMovement).filter(CapitalMovement.kind == 'from_cash').one()
            self.assertEqual(capital.amount, Decimal('1000.00'))
            self.assertEqual(db.query(CashMovement).filter(CashMovement.kind == 'capital_transfer').count(), 1)
            transfer = db.query(CashCustodyTransfer).filter(CashCustodyTransfer.kind == 'closing_capital').one()
            self.assertEqual(transfer.state, 'confirmed')
            self.assertEqual(transfer.accepted_by, self.users['manager']['id'])

    def test_transfer_failure_keeps_pending_state_and_rolls_back_ledger(self):
        self.open_for('cashier', '900')
        session = self.workspace()['session']
        pending = self.cmd('close', headers=self.roles['cashier'], target_id=self.users['manager']['id'], version=session['version'], denominations={'500': 1, '200': 2}, notes='Conteo exacto')
        pending_session = self.workspace()['session']
        with patch('app.services.cash_service.capital_service.record', side_effect=RuntimeError('capital unavailable')):
            with self.assertRaises(RuntimeError):
                self.client.post('/api/v1/cash/commands', json=dict(action='confirm_closing_transfer', branch_id=self.branch, target_id=pending['transfer_id'], version=pending_session['version'], transfer_version=1, acceptance_id='fail-' + uuid.uuid4().hex, acceptance_method='authenticated_confirmation', idempotency_key=str(uuid.uuid4()), notes='Intento fallido'), headers=self.roles['manager'])
        with SessionLocal() as db:
            session_row = db.get(CashSession, pending['session_id'])
            transfer = db.get(CashCustodyTransfer, pending['transfer_id'])
            self.assertEqual(session_row.state, 'closing_transfer_pending')
            self.assertEqual(transfer.state, 'pending')
            self.assertEqual(db.query(CashMovement).filter(CashMovement.kind == 'capital_transfer').count(), 0)
            self.assertEqual(db.query(CapitalMovement).filter(CapitalMovement.kind == 'from_cash').count(), 0)
