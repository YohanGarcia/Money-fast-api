"""FM-PRE-DEPLOY-FINAL-016: public registration creates a NEW company with its own administrator; employees are only
created by an administrator; complete isolation between companies; abuse controls."""
from unittest.mock import patch

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.company import Company
from app.models.user import User
from tests import test_api as legacy

BODY = dict(full_name='Dueno Uno', email='uno@example.com', password='Clave-segura-2026', company_name='Financiera Uno')


class RegistrationTests(legacy.MoneyFastApiTests):
    for _name in [n for n in dir(legacy.MoneyFastApiTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def reg(self, code=201, **over):
        r = self.client.post('/api/v1/auth/register', json={**BODY, **over})
        self.assertEqual(r.status_code, code, r.text)
        return r.json()

    def headers(self, email, password='Clave-segura-2026'):
        return self.auth_headers(self.login(email, password)['access_token'])

    def counts(self):
        with SessionLocal() as db:
            return db.scalar(select(func.count()).select_from(Company)), db.scalar(select(func.count()).select_from(User))

    # -- the contract: a registration is ALWAYS a new company + its administrator ----------------------------------
    def test_registration_creates_a_new_company_and_its_admin_in_production_too(self):
        with patch.object(settings, 'environment', 'production'), patch.object(settings, 'public_registration', None):
            out = self.reg()
        self.assertEqual(out['role'], 'admin')
        self.assertEqual(self.counts(), (1, 1))
        me = self.client.get('/api/v1/auth/me', headers=self.headers('uno@example.com')).json()
        self.assertEqual((me['role'], me['company_id']), ('admin', out['company_id']))

    def test_a_second_registration_is_a_separate_company_even_with_the_same_company_name(self):
        a = self.reg()
        b = self.reg(email='dos@example.com', full_name='Dueno Dos')  # same company_name on purpose
        self.assertNotEqual(a['company_id'], b['company_id'])
        self.assertEqual(self.counts(), (2, 2))
        with SessionLocal() as db:
            slugs = [c.slug for c in db.scalars(select(Company)).all()]
        self.assertEqual(len(set(slugs)), 2)

    def test_it_is_impossible_to_join_an_existing_company_or_choose_a_role(self):
        a = self.reg()
        out = self.reg(email='intruso@example.com', company_id=a['company_id'], role='cashier', is_superuser=True,
                       branch_id=1, tenant_id=a['company_id'])
        self.assertNotEqual(out['company_id'], a['company_id'])
        self.assertEqual(out['role'], 'admin')
        with SessionLocal() as db:
            users_a = db.scalars(select(User).where(User.company_id == a['company_id'])).all()
        self.assertEqual([u.email for u in users_a], ['uno@example.com'])  # company A did not gain anyone
        # an already used company identifier is refused, never merged
        self.reg(code=409, email='tres@example.com', tenant_slug='financiera-uno')

    # -- duplicates and input validation ---------------------------------------------------------------------------
    def test_duplicate_email_is_refused_case_insensitively(self):
        self.reg()
        self.reg(code=409, company_name='Otra SRL', email='UNO@Example.COM')
        self.assertEqual(self.counts(), (1, 1))

    def test_invalid_inputs_are_rejected_without_creating_anything(self):
        for over in (dict(email='no-es-correo'), dict(password='corta'), dict(company_name='   '),
                     dict(full_name='ab'), dict(company_name='x' * 161), dict(tenant_slug='Mal Slug!!'),
                     dict(password='x' * 200)):
            r = self.client.post('/api/v1/auth/register', json={**BODY, **over})
            self.assertIn(r.status_code, (409, 422), (over, r.text))
        self.assertEqual(self.counts(), (0, 0))

    # -- isolation between companies ---------------------------------------------------------------------------------
    def test_companies_are_fully_isolated_for_users_customers_and_loans(self):
        self.reg(); self.reg(email='dos@example.com', company_name='Financiera Dos')
        ha, hb = self.headers('uno@example.com'), self.headers('dos@example.com')
        cust = self.create_customer(ha, name='Cliente de Uno')
        loan = self.create_loan(ha, cust['id'])
        # B cannot see or read A's customer / loan / users
        self.assertEqual([c['id'] for c in self.client.get('/api/v1/customers', headers=hb).json()], [])
        self.assertEqual(self.client.get(f"/api/v1/customers/{cust['id']}", headers=hb).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/loans/{loan['id']}", headers=hb).status_code, 404)
        emails_b = {u['email'] for u in self.client.get('/api/v1/users', headers=hb).json()}
        self.assertEqual(emails_b, {'dos@example.com'})
        # and cannot reach A's resources by id on mutating routes either
        self.assertIn(self.client.put(f"/api/v1/customers/{cust['id']}", headers=hb, json=dict(full_name='X', document_id='1', phone='1', address='x')).status_code, (403, 404, 422))

    def test_employees_are_created_only_by_an_admin_inside_their_company(self):
        a = self.reg(); b = self.reg(email='dos@example.com', company_name='Financiera Dos')
        self._set_company_unlimited(a['company_id']); self._set_company_unlimited(b['company_id'])
        ha, hb = self.headers('uno@example.com'), self.headers('dos@example.com')
        branch = self.client.post('/api/v1/branches', headers=ha, json=dict(name='Central', address='Calle Centro 1',
                                  manager_name='Gerente QA', notary_name='Notario QA', phone='8095555555')).json()['id']
        emp = self.client.post('/api/v1/users', headers=ha, json=dict(full_name='Cajero Uno', email='caja@example.com',
                               password='Empleado-2026!', role='cashier', branch_id=branch))
        self.assertEqual(emp.status_code, 201, emp.text)
        self.assertEqual(emp.json()['company_id'], self.client.get('/api/v1/auth/me', headers=ha).json()['company_id'])
        # the employee cannot create users; another company's admin cannot place a user in company A's branch
        he = self.headers('caja@example.com', 'Empleado-2026!')
        self.assertEqual(self.client.post('/api/v1/users', headers=he, json=dict(full_name='Otro Emp', email='o@example.com',
                         password='Empleado-2026!', role='collector', branch_id=branch)).status_code, 403)
        r = self.client.post('/api/v1/users', headers=hb, json=dict(full_name='Ajeno', email='aj@example.com',
                             password='Empleado-2026!', role='cashier', branch_id=branch))
        self.assertIn(r.status_code, (404, 409, 422), r.text)
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(User).where(User.email == 'aj@example.com')))

    # -- abuse controls -------------------------------------------------------------------------------------------------
    def test_registrations_are_throttled_per_ip(self):
        with patch.object(settings, 'register_max_per_ip', 3):
            codes = [self.client.post('/api/v1/auth/register', json={**BODY, 'email': f'u{i}@example.com',
                     'company_name': f'Empresa {i}'}).status_code for i in range(6)]
        self.assertEqual(codes[:3], [201, 201, 201], codes)
        self.assertIn(429, codes[3:], codes)
        self.assertEqual(self.counts()[0], 3)

    def test_the_kill_switch_closes_registration_without_touching_login(self):
        self.reg()
        with patch.object(settings, 'public_registration', False):
            self.reg(code=403, email='dos@example.com')
            self.assertEqual(self.client.post('/api/v1/auth/login', json=dict(email='uno@example.com',
                             password='Clave-segura-2026', device_name='t')).status_code, 200)