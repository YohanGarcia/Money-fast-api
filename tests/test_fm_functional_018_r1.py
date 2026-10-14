"""FM-PRODUCTION-FUNCTIONAL-018-R1 (security review before merge): delegation / privilege escalation, payroll from Caja
as "administrator authorizes, cashier executes", and every alternative route into a session balance."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.cash import CashAudit, CashBox
from app.models.payroll import PayrollPayment
from app.models.user import User
from app.modules.cash import port as cash_port
from app.modules.identity.authorization import build_principal
from app.modules.identity.catalog import ADMIN_FORBIDDEN_OPERATIVE_PERMISSIONS, CASHIER_ROLE
from app.modules.identity.errors import PermissionDenied
from app.modules.identity.models import Role
from tests import test_cash as _cash
from tests.cash_fixtures import grant_cash_permissions, open_v2_session

OPERATIVE = sorted(ADMIN_FORBIDDEN_OPERATIVE_PERMISSIONS)


class SecurityReviewTests(_cash.CashTests):
    for _name in [n for n in dir(_cash.CashTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    # -- helpers ------------------------------------------------------------------------------------------------
    def v2(self, method, path, headers, body=None, code=None):
        r = self.client.request(method, '/api/v2' + path, json=body, headers=headers)
        if code is not None:
            self.assertEqual(r.status_code, code, r.text)
        return r

    def open_v1(self, headers, amount='100', code=200, branch=None):
        return self.req('/cash/commands', dict(action='open', branch_id=branch or self.branch,
                                               idempotency_key=uuid.uuid4().hex, opening_mode='initial', amount=amount),
                        headers, code=code)

    def principal(self, user_id):
        with SessionLocal() as db:
            return build_principal(db, db.get(User, user_id), 0)

    def cashier_role_id(self):
        with SessionLocal() as db:
            return db.scalar(select(Role.id).where(Role.tenant_id == self.users['cashier']['company_id'],
                                                   Role.name == CASHIER_ROLE))

    def extra_role_id(self):
        r = self.v2('POST', '/roles', self.admin, dict(name='Cajero ampliado ' + uuid.uuid4().hex[:6], description='x',
                                                       permissions=['cash.sessions.open', 'cash.sessions.read']))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()['id']

    def balance(self):
        return Decimal(self.workspace(self.roles['cashier'])['session']['balance'])

    def second_cashier(self):
        u = self.req('/users', dict(full_name='cashier2 QA', email='cashier2@example.com', password='workerpass123',
                                    role='cashier', branch_id=self.branch), self.admin, code=201)
        grant_cash_permissions(u['company_id'], u['id'], _cash.CASHIER_PERMS, self.branch)
        return self.auth_headers(self.login('cashier2@example.com', 'workerpass123')['access_token'])

    def pay_cash(self, headers=None, code=200, **over):
        body = dict(user_id=self.users['collector']['id'], amount='40', source='cash', branch_id=self.branch,
                    idempotency_key=uuid.uuid4().hex, salary_part='40')
        body.update(over)
        return self.req('/payroll/payments', body, headers or self.admin, code=code)

    def execute_pay(self, pid, headers, code=200):
        r = self.client.post(f'/api/v1/payroll/payments/{pid}/execute', headers=headers)
        self.assertEqual(r.status_code, code, r.text)
        return r.json()

    # -- 1. delegation ceiling / privilege escalation -----------------------------------------------------------
    def test_admin_assigns_cashier_role_to_a_cashier_at_branch_scope(self):
        cid = self.users['cashier']['id']
        r = self.v2('POST', f'/users/{cid}/roles', self.admin,
                    dict(role_id=self.extra_role_id(), scope='branch', branch_id=self.branch))
        self.assertEqual(r.status_code, 201, r.text)
        self.assertFalse({g.permission for g in self.principal(self.admin_id).grants} & set(OPERATIVE))

    def test_operative_delegation_is_refused_to_non_cashiers_tenant_scope_and_non_admins(self):
        rid = self.extra_role_id()
        mid, cid = self.users['manager']['id'], self.users['cashier']['id']
        self.v2('POST', f'/users/{mid}/roles', self.admin, dict(role_id=rid, scope='branch', branch_id=self.branch),
                code=403)  # the target is a manager, not a cashier
        self.v2('POST', f'/users/{cid}/roles', self.admin, dict(role_id=rid, scope='tenant'), code=403)  # never tenant-wide
        # a non-administrator that DOES hold roles.assign cannot use the exception: it never held those powers
        grant_cash_permissions(self.users['manager']['company_id'], mid, ['roles.assign', 'roles.read', 'users.read'])
        manager_h = self.auth_headers(self.login('manager@example.com', 'workerpass123')['access_token'])
        r = self.v2('POST', f'/users/{cid}/roles', manager_h, dict(role_id=rid, scope='branch', branch_id=self.branch))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse({g.permission for g in self.principal(mid).grants} & set(OPERATIVE))

    def test_non_admin_cannot_create_a_role_richer_than_itself(self):
        mid = self.users['manager']['id']
        grant_cash_permissions(self.users['manager']['company_id'], mid, ['roles.create', 'roles.read'])
        manager_h = self.auth_headers(self.login('manager@example.com', 'workerpass123')['access_token'])
        r = self.v2('POST', '/roles', manager_h, dict(name='Escalada', description='x', permissions=OPERATIVE))
        self.assertEqual(r.status_code, 403, r.text)
        # the administrator may define a cashier role (inert until it is assigned to a cashier)
        self.extra_role_id()  # the administrator may define it

    def test_administrator_cannot_hand_operative_powers_to_itself_or_another_administrator(self):
        rid = self.extra_role_id()
        r = self.v2('POST', f'/users/{self.admin_id}/roles', self.admin,
                    dict(role_id=rid, scope='branch', branch_id=self.branch))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse({g.permission for g in self.principal(self.admin_id).grants} & set(OPERATIVE))

    def test_administrator_still_administers_cashiers(self):
        cid = self.users['cashier']['id']
        self.v2('POST', f'/users/{cid}/disable', self.admin, code=200)
        self.v2('POST', f'/users/{cid}/enable', self.admin, code=200)

    # -- 2. payroll from Caja: authorize (administrator) / execute (cashier) ------------------------------------
    def test_payroll_authorization_never_debits_and_only_the_session_owner_executes(self):
        sid = self.open_v1(self.roles['cashier'], amount='300')['session_id']
        auth = self.pay_cash()
        self.assertEqual((auth['source'], auth['status']), ('cash', 'authorized'))
        self.assertEqual(self.balance(), Decimal('300'))  # authorizing moves nothing
        for who in (self.admin, self.roles['manager'], self.roles['collector']):
            self.execute_pay(auth['id'], who, code=403)
        self.pay_cash(session_id=sid, code=422)  # the administrator cannot choose the session
        other = self.second_cashier()
        self.execute_pay(auth['id'], other, code=409)  # another cashier has no session of its own: nothing is debited
        self.assertEqual(self.balance(), Decimal('300'))
        done = self.execute_pay(auth['id'], self.roles['cashier'])
        self.assertEqual((done['status'], done['session_id']), ('paid', sid))
        self.assertEqual(self.balance(), Decimal('260'))
        again = self.execute_pay(auth['id'], self.roles['cashier'])  # idempotent
        self.assertEqual((again['movement_id'], again['replayed']), (done['movement_id'], True))
        self.assertEqual(self.balance(), Decimal('260'))
        self.execute_pay(auth['id'], other, code=409)  # already paid
        with SessionLocal() as db:
            row = db.get(PayrollPayment, auth['id'])
            self.assertEqual((row.status, row.actor_id, row.executed_by), ('paid', self.admin_id, self.users['cashier']['id']))
            actions = {a for a in db.scalars(select(CashAudit.action))}
        self.assertTrue({'payroll_payment_authorized', 'payroll_payment'} <= actions)

    def test_payroll_cash_needs_enough_balance_and_only_the_administrator_cancels(self):
        self.open_v1(self.roles['cashier'], amount='30')
        big = self.pay_cash(amount='40')
        self.execute_pay(big['id'], self.roles['cashier'], code=409)
        self.assertEqual(self.balance(), Decimal('30'))
        r = self.client.post(f"/api/v1/payroll/payments/{big['id']}/cancel", headers=self.roles['cashier'])
        self.assertEqual(r.status_code, 403, r.text)
        r = self.client.post(f"/api/v1/payroll/payments/{big['id']}/cancel", headers=self.admin)
        self.assertEqual((r.status_code, r.json()['status']), (200, 'cancelled'))
        self.execute_pay(big['id'], self.roles['cashier'], code=409)

    def test_payroll_authorization_is_idempotent_and_bank_stays_administrator_only(self):
        key = uuid.uuid4().hex
        a = self.pay_cash(idempotency_key=key)
        b = self.pay_cash(idempotency_key=key)
        self.assertEqual((a['id'], b['replayed']), (b['id'], True))
        out = self.pay_cash(source='bank', bank_account_id=self.bank_account['id'], reference='TRF-1', amount='25',
                            salary_part='25')
        self.assertEqual(out['source'], 'bank')  # the bank payment is direct and administrator-only
        self.pay_cash(headers=self.roles['cashier'], source='bank', bank_account_id=self.bank_account['id'],
                      reference='TRF-2', code=403)

    # -- 3. alternative routes into a balance -------------------------------------------------------------------
    def test_administrator_cannot_reverse_into_a_cashier_session(self):
        sid = self.open_v1(self.roles['cashier'], amount='500')['session_id']
        ver = self.workspace(self.roles['cashier'])['session']['version']
        add = self.req('/cash/commands', dict(action='cash_addition', branch_id=self.branch, idempotency_key=uuid.uuid4().hex,
                                              version=ver, amount='100', reference='R1', notes='Aporte'),
                       self.roles['cashier'])
        before = self.balance()
        ver = self.workspace(self.roles['cashier'])['session']['version']
        self.req('/cash/commands', dict(action='reverse', branch_id=self.branch, idempotency_key=uuid.uuid4().hex,
                                        target_id=add['movement_id'], session_id=sid, version=ver,
                                        notes='Reverso del administrador'), self.admin, code=403)
        self.assertEqual(self.balance(), before)

    def test_surplus_confirmation_lands_only_in_the_session_that_reported_it(self):
        self.open_v1(self.roles['cashier'], amount='200')
        rep = self.req('/cash/commands', dict(action='report_surplus', branch_id=self.branch,
                                              idempotency_key=uuid.uuid4().hex, target_id=self.users['collector']['id'],
                                              amount='15', notes='Billete sobrante del cobrador'), self.roles['cashier'])
        out = self.req('/cash/commands', dict(action='confirm_surplus', branch_id=self.branch,
                                              idempotency_key=uuid.uuid4().hex, target_id=rep['delivery_id'], version=1,
                                              session_id=999999, notes='Sobrante confirmado'), self.admin)
        self.assertEqual(out['state'], 'surplus_confirmed')
        self.assertEqual(self.balance(), Decimal('215'))  # the session the admin "picked" (999999) was ignored

    def test_legacy_session_owned_by_an_administrator_cannot_be_operated(self):
        with SessionLocal() as db:
            box = db.scalar(select(CashBox).where(CashBox.branch_id == self.other))
            sess = open_v2_session(db, box_id=box.id, cashier_id=self.admin_id, balance='500.00')
            db.commit()
            sid, company = sess.id, box.company_id
        self.v2('GET', f'/cash-points?branch_id={self.other}', self.admin, code=200)
        self.v2('POST', f'/cash/sessions/{sid}/close', self.admin,
                dict(idempotency_key=uuid.uuid4().hex, denominations={'500': 1}), code=403)
        with SessionLocal() as db:
            with self.assertRaises(PermissionDenied):
                cash_port.deposit(db, tenant_id=company, branch_id=self.other, session_id=sid, amount=Decimal('5.00'),
                                  currency='DOP', actor_user_id=self.admin_id, kind='credit_payment_receipt',
                                  reference='A', notes='a')
            db.rollback()
            with self.assertRaises(PermissionDenied):
                cash_port.withdraw(db, tenant_id=company, branch_id=self.other, session_id=sid, amount=Decimal('5.00'),
                                   currency='DOP', actor_user_id=self.admin_id, kind='credit_disbursement',
                                   reference='A', notes='a', require_cashier_id=self.admin_id)
            db.rollback()
        self.req('/cash/commands', dict(action='movement', branch_id=self.other, idempotency_key=uuid.uuid4().hex,
                                        kind='expense', amount='5', notes='no autorizado', session_id=sid, version=1),
                 self.admin, code=403)

    def test_general_loan_and_payment_permissions_do_not_let_the_administrator_touch_caja(self):
        # the administrator holds payments.create / loans.disburse (general permissions) but no operative cash power
        held = {g.permission for g in self.principal(self.admin_id).grants}
        self.assertTrue({'payments.create', 'loans.disburse'} <= held)
        sid = self.open_v1(self.roles['cashier'], amount='400')['session_id']
        before = self.balance()
        with SessionLocal() as db:
            company = db.scalar(select(CashBox.company_id).where(CashBox.branch_id == self.branch))
            for fn in (cash_port.deposit, cash_port.withdraw):
                with self.assertRaises(Exception):
                    fn(db, tenant_id=company, branch_id=self.branch, session_id=sid, amount=Decimal('9.00'),
                       currency='DOP', actor_user_id=self.admin_id, kind='credit_payment_receipt', reference='A', notes='a')
                db.rollback()
        self.assertEqual(self.balance(), before)
