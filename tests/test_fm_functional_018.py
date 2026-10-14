"""FM-PRODUCTION-FUNCTIONAL-018 (owner decisions): the company administrator configures and supervises Caja but never
operates a session, through any endpoint (/api/v1 legacy and /api/v2), whatever grants it inherited; the cashier operates
only its own session; loan applications need no attached file until evaluation."""
import base64
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.user import User
from app.modules.cash import port as cash_port
from app.modules.cash.port import CashSessionNotOwned
from app.modules.identity.authorization import Principal, build_principal
from app.modules.identity.catalog import ADMIN_FORBIDDEN_OPERATIVE_PERMISSIONS
from tests import test_api as legacy
from tests import test_cash as _cash
from tests.cash_fixtures import grant_cash_permissions

OPERATIVE = sorted(ADMIN_FORBIDDEN_OPERATIVE_PERMISSIONS)
SUPERVISION = ["cash.points.read", "cash.points.manage", "cash.sessions.read", "cash.differences.read",
               "cash.differences.resolve", "cash.handovers.redirect", "organization.branches.manage"]


class AdminCashSeparationTests(_cash.CashTests):
    for _name in [n for n in dir(_cash.CashTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def v2(self, method, path, headers, body=None, code=None):
        r = self.client.request(method, '/api/v2' + path, json=body, headers=headers)
        if code is not None:
            self.assertEqual(r.status_code, code, r.text)
        return r

    def point(self):
        rows = self.v2('GET', f'/cash-points?branch_id={self.branch}', self.admin, code=200).json()
        return rows[0]['id']

    def open_v2(self, headers, amount='100', code=None):
        return self.v2('POST', '/cash/sessions/open', headers, dict(
            idempotency_key=uuid.uuid4().hex, cash_point_id=self.point(), source='initial', amount=amount), code)

    def open_v1(self, headers, amount='100', code=200):
        return self.req('/cash/commands', dict(action='open', branch_id=self.branch, idempotency_key=uuid.uuid4().hex,
                                               opening_mode='initial', amount=amount), headers, code=code)

    def principal(self, user_id):
        with SessionLocal() as db:
            return build_principal(db, db.get(User, user_id), 0)

    # -- effective authorization ------------------------------------------------------------------------------
    def test_admin_effective_grants_keep_supervision_and_drop_operation(self):
        held = {g.permission for g in self.principal(self.admin_id).grants}
        for code in OPERATIVE:
            self.assertNotIn(code, held, code)
        for code in SUPERVISION:
            self.assertIn(code, held, code)

    def test_inherited_or_legacy_operative_grants_are_ignored_for_the_admin(self):
        company = self.users['cashier']['company_id']
        grant_cash_permissions(company, self.admin_id, OPERATIVE)  # an explicit tenant-wide role with every operative code
        held = {g.permission for g in self.principal(self.admin_id).grants}
        self.assertFalse(held & set(OPERATIVE))
        self.open_v2(self.admin, code=403)

    def test_new_tenant_admin_role_has_no_operative_permission(self):
        from app.modules.identity.models import Permission, Role, RolePermission
        with SessionLocal() as db:
            role = db.scalar(select(Role).where(Role.tenant_id == self.users['cashier']['company_id'],
                                                Role.name == 'Administrador de agencia'))
            codes = set(db.scalars(select(Permission.code).join(RolePermission, RolePermission.permission_id == Permission.id)
                                   .where(RolePermission.role_id == role.id)))
        self.assertFalse(codes & set(OPERATIVE))
        self.assertTrue(set(SUPERVISION) <= codes)

    # -- administrator: forbidden operation, v1 and v2 ---------------------------------------------------------
    def test_admin_cannot_open_or_close_through_v1_or_v2(self):
        self.open_v2(self.admin, code=403)
        self.open_v1(self.admin, amount='0', code=403)
        out = self.open_v1(self.roles['cashier'], amount='300')
        sid = out['session_id']
        self.v2('POST', f'/cash/sessions/{sid}/close', self.admin,
                dict(idempotency_key=uuid.uuid4().hex, counted='300'), code=403)
        self.req('/cash/commands', dict(action='close', branch_id=self.branch, idempotency_key=uuid.uuid4().hex,
                                        version=self.workspace(self.roles['cashier'])['session']['version'],
                                        counted='300', session_id=sid),
                 self.admin, code=403)
        self.assertEqual(self.workspace(self.roles['cashier'])['session']['state'], 'open')  # untouched

    def test_admin_cannot_register_cashier_only_operations_on_old_endpoints(self):
        out = self.open_v1(self.roles['cashier'], amount='500')
        ver = self.workspace(self.roles['cashier'])['session']['version']
        for action, extra in (('movement', dict(kind='expense', amount='10', notes='Gasto no autorizado')),
                              ('cash_addition', dict(amount='10', reference='ADM-1', notes='Aporte no autorizado')),
                              ('receive_collector', dict(target_id=self.users['collector']['id'], amount='1')),
                              ('disburse', dict(target_id=1))):
            self.req('/cash/commands', dict(action=action, branch_id=self.branch, idempotency_key=uuid.uuid4().hex,
                                            version=ver, session_id=out['session_id'], **extra), self.admin, code=403)
        self.payment(origin='counter', headers=self.admin, code=403)  # window collection belongs to the cashier
        self.assertEqual(Decimal(self.workspace(self.roles['cashier'])['session']['balance']), Decimal('500'))

    def test_admin_keeps_configuration_and_supervision(self):
        self.req('/cash/config', headers=self.admin)
        self.req(f'/cash/workspace?branch_id={self.branch}', headers=self.admin)  # supervision view
        self.v2('GET', f'/cash-points?branch_id={self.branch}', self.admin, code=200)
        self.v2('GET', f'/cash/differences?branch_id={self.branch}', self.admin, code=200)
        self.req('/branches', dict(name='Sur', address='Calle Sur 1', manager_name='Gerente Sur', notary_name='Notario Sur',
                                   phone='8095555555'), self.admin, code=201)
        out = self.open_v1(self.roles['cashier'], amount='50')
        ws = self.req(f'/cash/workspace?branch_id={self.branch}', headers=self.admin)
        self.assertTrue(any(s['id'] == out['session_id'] for s in ws['sessions']))
        self.req(f'/cash/sessions/{out["session_id"]}/ticket?kind=opening&branch_id={self.branch}', headers=self.admin)

    # -- cashier -----------------------------------------------------------------------------------------------
    def test_authorized_cashier_opens_operates_and_closes_own_session(self):
        out = self.open_v2(self.roles['cashier'], amount='0', code=200).json()
        sid = out['session_id'] if 'session_id' in out else out['id']
        close = self.v2('POST', f'/cash/sessions/{sid}/close', self.roles['cashier'],
                        dict(idempotency_key=uuid.uuid4().hex, counted='0'))
        self.assertEqual(close.status_code, 200, close.text)

    def test_cashier_cannot_operate_another_cashiers_session(self):
        u = self.req('/users', dict(full_name='cashier2 QA', email='cashier2@example.com', password='workerpass123',
                                    role='cashier', branch_id=self.branch), self.admin, code=201)
        grant_cash_permissions(u['company_id'], u['id'], _cash.CASHIER_PERMS, self.branch)
        other = self.auth_headers(self.login('cashier2@example.com', 'workerpass123')['access_token'])
        out = self.open_v1(self.roles['cashier'], amount='200')
        sid = out['session_id']
        r = self.v2('POST', f'/cash/sessions/{sid}/close', other, dict(idempotency_key=uuid.uuid4().hex, counted='200'))
        self.assertIn(r.status_code, (403, 409), r.text)
        with SessionLocal() as db:
            with self.assertRaises(CashSessionNotOwned):
                cash_port.deposit(db, tenant_id=u['company_id'], branch_id=self.branch, session_id=sid,
                                  amount=Decimal('5.00'), currency='DOP', actor_user_id=u['id'],
                                  kind='credit_payment_receipt', reference='X', notes='x')
            db.rollback()
        self.assertEqual(Decimal(self.workspace(self.roles['cashier'])['session']['balance']), Decimal('200'))

    def test_manager_and_collector_cannot_operate_without_express_authorization(self):
        for who in ('manager', 'collector'):
            self.open_v2(self.roles[who], code=403)
            self.open_v1(self.roles[who], amount='10', code=403)
        held = {g.permission for g in self.principal(self.users['manager']['id']).grants}
        self.assertFalse(held & set(OPERATIVE))

    def test_admin_still_administers_and_assigns_cashiers_through_v2(self):
        from app.modules.identity.catalog import CASHIER_ROLE
        from app.modules.identity.models import Role
        cid = self.users['cashier']['id']
        self.v2('POST', f'/users/{cid}/disable', self.admin, code=200)
        self.v2('POST', f'/users/{cid}/enable', self.admin, code=200)
        with SessionLocal() as db:
            role_id = db.scalar(select(Role.id).where(Role.tenant_id == self.users['cashier']['company_id'],
                                                      Role.name == CASHIER_ROLE))
        self.assertIsNotNone(role_id)
        r = self.v2('POST', f"/users/{self.users['manager']['id']}/roles", self.admin,
                    dict(role_id=role_id, scope='branch', branch_id=self.branch))
        self.assertEqual(r.status_code, 201, r.text)  # assigning a cashier is allowed; exercising is not
        self.assertFalse({g.permission for g in self.principal(self.admin_id).grants} & set(OPERATIVE))

    def test_platform_principal_has_no_company_cash_operation(self):
        p = Principal(user_id=1, tenant_id=None, person_id=None, session_id=0, grants=())
        for code in OPERATIVE:
            self.assertFalse(p.allows(code))


class LoanDocumentsOptionalTests(legacy.MoneyFastApiTests):
    for _name in [n for n in dir(legacy.MoneyFastApiTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def test_create_save_submit_without_files_then_attach_and_evaluate(self):
        from app.schemas.loan_application import DOCUMENTS
        headers = self.owner_session()
        for modality in ('unsecured', 'secured'):
            app = self.application_fixture(headers, modality)            # created WITHOUT any document: draft
            self.assertEqual((app['status'], app['documents']), ('draft', []))
            cust = self.client.get(f'/api/v1/customers/{app["customer_id"]}', headers=headers).json()
            edit = self.client.put(f'/api/v1/loan-applications/{app["id"]}', headers=headers,
                                   json={'modality': modality, 'data': app['data'], 'version': app['version'],
                                         'customer_id': cust['id'], 'customer_version': cust['version']})
            self.assertEqual(edit.status_code, 200, edit.text)       # saving the draft needs no file
            app = edit.json()
            sub = self.app_action(headers, app, 'submit')            # formally presented with no attachments
            self.assertEqual(sub.status_code, 200, sub.text)
            app = sub.json()
            self.assertEqual((app['status'], app['documents']), ('receiving', []))
            blocked = self.app_action(headers, app, 'evaluate')      # the evaluation stage DOES need them
            self.assertEqual(blocked.status_code, 422, blocked.text)
            self.assertIn('Faltan documentos', blocked.json()['detail'])
            required = [d for d in DOCUMENTS if d['required'] and (not d.get('secured') or modality == 'secured')]
            for d in required[:-1]:
                app = self.app_document(headers, app, d['key'])
            self.assertEqual(self.app_action(headers, app, 'evaluate').status_code, 422)  # one still missing
            app = self.app_document(headers, app, required[-1]['key'])
            ok = self.app_action(headers, app, 'evaluate')
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(ok.json()['status'], 'evaluation')

    def test_unverified_document_does_not_satisfy_evaluation_and_form_fields_remain_mandatory(self):
        from app.schemas.loan_application import DOCUMENTS
        headers = self.owner_session()
        app = self.app_action(headers, self.application_fixture(headers), 'submit').json()
        for d in [d for d in DOCUMENTS if d['required'] and not d.get('secured')]:  # attached but NOT verified
            r = self.client.post(f'/api/v1/loan-applications/{app["id"]}/documents/{d["key"]}', headers=headers,
                                 json={'version': app['version'], 'filename': 'd.pdf',
                                       'content_base64': base64.b64encode(b'%PDF-1.4\nx\n%%EOF').decode()})
            self.assertEqual(r.status_code, 200, r.text)
            app = r.json()
        self.assertEqual(self.app_action(headers, app, 'evaluate').status_code, 422)
        incomplete = self.client.post('/api/v1/loan-applications', headers=headers,
                                      json={'modality': 'unsecured', 'data': {'full_name': 'Sin datos'},
                                            'create_customer': True})
        self.assertEqual(incomplete.status_code, 422)  # form fields (not files) remain mandatory
