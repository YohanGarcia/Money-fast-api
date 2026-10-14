"""FM-PRODUCTION-PREPARATION-013: the operative cashier role ("Cajero") is created and kept in sync with the legacy
cashier role: own branch only, own cash points only, nothing tenant-wide."""
import uuid

from sqlalchemy import select

from app.core.database import SessionLocal
from app.modules.identity.models import Permission, Role, RolePermission, UserRoleAssignment
from tests import test_cash as _cash

WANTED = {"cash.sessions.open", "cash.sessions.close", "cash.sessions.read", "cash.points.read"}


class CashierRoleTests(_cash.CashTests):
    for _name in [n for n in dir(_cash.CashTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def new_cashier(self, email='cajero2@example.com', branch=None):
        u = self.req('/users', dict(full_name='Cajero Dos', email=email, password='workerpass123', role='cashier',
                                    branch_id=branch or self.branch), code=201)
        return u, self.auth_headers(self.login(email, 'workerpass123')['access_token'])

    def active(self, user_id):
        with SessionLocal() as db:
            rows = db.execute(select(UserRoleAssignment, Role).join(Role, Role.id == UserRoleAssignment.role_id).where(
                UserRoleAssignment.user_id == user_id, UserRoleAssignment.revoked_at.is_(None))).all()
            return [(r.name, a.scope_kind, a.branch_id) for a, r in rows]

    def role_codes(self, user_id):
        with SessionLocal() as db:
            return set(db.scalars(select(Permission.code).join(RolePermission, RolePermission.permission_id == Permission.id)
                                  .join(Role, Role.id == RolePermission.role_id)
                                  .join(UserRoleAssignment, UserRoleAssignment.role_id == Role.id)
                                  .where(UserRoleAssignment.user_id == user_id, UserRoleAssignment.revoked_at.is_(None))))

    def points(self, headers, branch):
        r = self.client.get(f'/api/v2/cash-points?branch_id={branch}', headers=headers)
        return r.status_code, (r.json() if r.status_code == 200 else None)

    def test_a_new_cashier_gets_exactly_the_operative_grants_on_its_branch(self):
        u, h = self.new_cashier()
        self.assertEqual(self.active(u['id']), [('Cajero', 'branch', self.branch)])
        self.assertEqual(self.role_codes(u['id']), WANTED)
        status, pts = self.points(h, self.branch)
        self.assertEqual(status, 200)
        self.assertEqual([p['branch_id'] for p in pts], [self.branch])

    def test_the_cashier_opens_and_closes_without_any_manual_grant(self):
        u, h = self.new_cashier()
        opened = self.req('/cash/commands', dict(action='open', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                                 opening_mode='initial', amount='100'), h)
        self.assertIn('session_id', opened)
        version = self.req(f'/cash/workspace?branch_id={self.branch}', headers=h)['session']['version']
        closed = self.req('/cash/commands', dict(action='close', branch_id=self.branch, idempotency_key=str(uuid.uuid4()),
                                                 version=version, counted='100', notes=''), h)
        self.assertEqual(closed['session']['difference'], '0.00')

    def test_no_access_to_cash_points_or_sessions_of_another_branch(self):
        u, h = self.new_cashier()
        status, pts = self.points(h, self.other)
        self.assertTrue(status == 403 or pts == [], (status, pts))
        self.req(f'/cash/workspace?branch_id={self.other}', headers=h, code=403)
        self.req('/cash/commands', dict(action='open', branch_id=self.other, idempotency_key=str(uuid.uuid4()),
                                        opening_mode='initial', amount='10'), h, 403)

    def test_collector_and_manager_receive_no_cash_grants(self):
        for role in ('collector', 'manager'):
            self.assertEqual(self.active(self.users[role]['id']), [])
            status, pts = self.points(self.roles[role], self.branch)
            self.assertTrue(status == 403 or pts == [], (role, status, pts))

    def test_moving_the_cashier_to_another_branch_moves_the_grant(self):
        u, h = self.new_cashier()
        self.req(f"/users/{u['id']}", dict(full_name=u['full_name'], email=u['email'], role='cashier', is_active=True,
                                           branch_id=self.other), method='PUT')
        self.assertEqual(self.active(u['id']), [('Cajero', 'branch', self.other)])
        h = self.auth_headers(self.login(u['email'], 'workerpass123')['access_token'])
        self.req(f'/cash/workspace?branch_id={self.branch}', headers=h, code=403)

    def test_changing_the_legacy_role_or_disabling_revokes_the_cashier_grant(self):
        u, _ = self.new_cashier()
        self.req(f"/users/{u['id']}", dict(full_name=u['full_name'], email=u['email'], role='collector', is_active=True,
                                           branch_id=self.branch), method='PUT')
        self.assertEqual(self.active(u['id']), [])
        v, _ = self.new_cashier(email='cajero3@example.com')
        self.req(f"/users/{v['id']}", dict(full_name=v['full_name'], email=v['email'], role='cashier', is_active=False,
                                           branch_id=self.branch), method='PUT')
        self.assertEqual(self.active(v['id']), [])

    def test_the_role_is_created_once_per_company_and_never_grants_more(self):
        self.new_cashier(email='c4@example.com')
        self.new_cashier(email='c5@example.com')
        with SessionLocal() as db:
            roles = db.scalars(select(Role).where(Role.name == 'Cajero')).all()
            self.assertEqual(len(roles), 1)
            self.assertTrue(roles[0].system_defined)
            codes = set(db.scalars(select(Permission.code).join(RolePermission, RolePermission.permission_id == Permission.id)
                                   .where(RolePermission.role_id == roles[0].id)))
            self.assertEqual(codes, WANTED)