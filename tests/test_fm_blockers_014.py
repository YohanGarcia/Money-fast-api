"""FM-PRODUCTION-BLOCKERS-014: legacy 6-digit password recovery contract (Web + mobile), closed public registration
and the first-administrator CLI. Same disposable-PostgreSQL harness as tests/test_api.py."""
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, update

from app.core.config import settings
from app.core.database import SessionLocal
from app.main import app
from app.modules.identity.models import RecoveryToken
from app.modules.identity.notifications import get_notifier
from tests import test_api as legacy

ROOT = Path(__file__).resolve().parents[1]


class Sink:
    def __init__(self):
        self.sent = []

    def send(self, *, email, secret, purpose, expires_at):
        self.sent.append((email, secret, purpose))
        return True


class RecoveryAndRegistrationTests(legacy.MoneyFastApiTests):
    for _name in [n for n in dir(legacy.MoneyFastApiTests) if n.startswith('test_')]:
        locals()[_name] = None
    del _name

    def setUp(self):
        super().setUp()
        self.sink = Sink()
        app.dependency_overrides[get_notifier] = lambda: self.sink
        self.addCleanup(app.dependency_overrides.pop, get_notifier, None)
        self.register_owner()  # owner@example.com / superpass123

    def post(self, path, body, code=200):
        r = self.client.post('/api/v1/auth' + path, json=body)
        self.assertEqual(r.status_code, code, r.text)
        return r

    def request_code(self, email='owner@example.com'):
        r = self.post('/request-password-reset', dict(email=email))
        return r.json()

    def last_code(self):
        self.assertTrue(self.sink.sent, 'no code was sent')
        code = self.sink.sent[-1][1]
        self.assertRegex(code, r'^\d{6}$')
        return code

    def verify(self, code, email='owner@example.com', status=200):
        return self.post('/verify-reset-code', dict(email=email, code=code), status)

    # -- the contract the clients use -------------------------------------------------------------------------
    def test_full_flow_changes_the_password_and_revokes_sessions(self):
        old = self.login()
        out = self.request_code()
        self.assertEqual(set(out), {'message', 'email'})
        self.assertNotIn('debug_code', out)
        token = self.verify(self.last_code()).json()['reset_token']
        self.post('/reset-password', dict(reset_token=token, new_password='NuevaClave-2026!'), 204)
        self.post('/login', dict(email='owner@example.com', password='superpass123', device_name='t'), 401)
        self.post('/login', dict(email='owner@example.com', password='NuevaClave-2026!', device_name='t'), 200)
        self.post('/refresh', dict(refresh_token=old['refresh_token']), 401)  # earlier sessions are gone

    def test_known_and_unknown_email_get_the_same_answer_and_only_known_gets_a_code(self):
        known = self.request_code('owner@example.com')
        unknown = self.request_code('nadie@example.com')
        self.assertEqual(known['message'], unknown['message'])
        self.assertEqual(len(self.sink.sent), 1)
        self.assertEqual(self.sink.sent[0][0], 'owner@example.com')

    def test_the_code_is_stored_only_as_a_keyed_digest(self):
        self.request_code()
        code = self.last_code()
        with SessionLocal() as db:
            hashes = db.scalars(select(RecoveryToken.token_hash)).all()
        self.assertEqual(len(hashes), 1)
        self.assertNotEqual(hashes[0], code)
        self.assertRegex(hashes[0], r'^[0-9a-f]{64}$')

    def test_wrong_code_is_the_same_error_for_known_and_unknown_accounts(self):
        self.request_code()
        a = self.verify('000000' if self.last_code() != '000000' else '000001', status=400).json()
        b = self.verify('123456', email='nadie@example.com', status=400).json()
        self.assertEqual(a['detail'], b['detail'])

    def test_a_code_is_single_use(self):
        self.request_code()
        code = self.last_code()
        self.verify(code)
        self.verify(code, status=400)

    def test_the_reset_token_is_single_use(self):
        self.request_code()
        token = self.verify(self.last_code()).json()['reset_token']
        self.post('/reset-password', dict(reset_token=token, new_password='NuevaClave-2026!'), 204)
        self.post('/reset-password', dict(reset_token=token, new_password='OtraClave-2026!!'), 400)

    def test_an_expired_code_is_refused(self):
        self.request_code()
        code = self.last_code()
        with SessionLocal() as db:
            db.execute(update(RecoveryToken).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
            db.commit()
        self.verify(code, status=400)

    def test_wrong_attempts_are_limited_and_kill_the_live_code(self):
        self.request_code()
        code = self.last_code()
        wrong = '000000' if code != '000000' else '000001'
        for _ in range(settings.recovery_code_max_attempts):
            self.verify(wrong, status=400)
        self.client.post('/api/v1/auth/verify-reset-code', json=dict(email='owner@example.com', code=code))
        r = self.client.post('/api/v1/auth/verify-reset-code', json=dict(email='owner@example.com', code=code))
        self.assertIn(r.status_code, (400, 429), r.text)  # even the right code no longer works
        with SessionLocal() as db:
            live = db.scalars(select(RecoveryToken).where(RecoveryToken.used_at.is_(None), RecoveryToken.revoked_at.is_(None))).all()
        self.assertEqual(live, [])

    def test_a_new_request_revokes_the_previous_code(self):
        self.request_code()
        first = self.last_code()
        self.request_code()
        second = self.last_code()
        if first != second:
            self.verify(first, status=400)
        self.verify(second)

    def test_disabled_users_receive_nothing(self):
        with SessionLocal() as db:
            from app.models.user import User
            for u in db.scalars(select(User)).all():
                u.is_active = False
            db.commit()
        self.request_code()
        self.assertEqual(self.sink.sent, [])

    def test_weak_new_password_is_refused_and_does_not_burn_a_valid_flow_silently(self):
        self.request_code()
        token = self.verify(self.last_code()).json()['reset_token']
        r = self.client.post('/api/v1/auth/reset-password', json=dict(reset_token=token, new_password='12345678'))
        self.assertIn(r.status_code, (400, 422), r.text)
        self.post('/login', dict(email='owner@example.com', password='superpass123', device_name='t'), 200)

    # -- public registration ------------------------------------------------------------------------------------
    def test_registration_is_closed_when_the_flag_is_off(self):
        body = dict(full_name='Otra Persona', email='otra@example.com', password='superpass123', company_name='Otra SRL')
        with patch.object(settings, 'public_registration', False):
            r = self.client.post('/api/v1/auth/register', json=body)
        self.assertEqual(r.status_code, 403, r.text)
        with patch.object(settings, 'public_registration', True):
            self.assertEqual(self.client.post('/api/v1/auth/register', json=body).status_code, 201)

    def test_registration_default_follows_the_environment(self):
        with patch.object(settings, 'public_registration', None):
            for env, expected in (('development', True), ('test', True), ('staging', False), ('production', False)):
                with patch.object(settings, 'environment', env):
                    self.assertEqual(settings.registration_open, expected, env)

    # -- first administrator CLI --------------------------------------------------------------------------------
    def run_cli(self, *args, password='ClaveInicial-2026!'):
        env = {**os.environ, 'FM_ADMIN_PASSWORD': password}
        return subprocess.run([sys.executable, 'scripts/create_first_admin.py', *args], cwd=ROOT, env=env,
                              capture_output=True, text=True)

    def test_first_admin_cli_refuses_an_installation_that_already_has_a_company(self):
        r = self.run_cli('--email', 'admin@nueva.com', '--name', 'Admin Nuevo', '--company', 'Nueva SRL')
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertNotIn('ClaveInicial', r.stdout + r.stderr)

    def test_first_admin_cli_creates_the_admin_on_an_empty_installation(self):
        from app.core.database import Base, engine
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        weak = self.run_cli('--email', 'admin@nueva.com', '--name', 'Admin Nuevo', '--company', 'Nueva SRL', password='123')
        self.assertEqual(weak.returncode, 2, weak.stderr)
        ok = self.run_cli('--email', 'admin@nueva.com', '--name', 'Admin Nuevo', '--company', 'Nueva SRL')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertNotIn('ClaveInicial', ok.stdout + ok.stderr)
        self.post('/login', dict(email='admin@nueva.com', password='ClaveInicial-2026!', device_name='t'), 200)
        me = self.client.get('/api/v1/auth/me', headers=self.auth_headers(
            self.login('admin@nueva.com', 'ClaveInicial-2026!')['access_token'])).json()
        self.assertEqual(me['role'], 'admin')
        self.assertIsNone(re.search(r'password', str(me)))