"""F-I01: POST /api/v1/capital/movements is replay-safe per (tenant, actor, key).

Real concurrency: every worker thread owns its TestClient and therefore its own DB connection/transaction.
"""

import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.database import SessionLocal
from app.main import app
from app.models.capital import CapitalMovement
from app.models.cash import CashRequest
from tests import test_api as legacy
from tests import test_cash as cash_tests  # module import: pytest must not collect CashTests twice

URL = "/api/v1/capital/movements"


def key():
    return f"qa-cap-{uuid.uuid4().hex[:16]}"


class CapitalIdempotencyTests(unittest.TestCase):
    setUpClass = classmethod(legacy.MoneyFastApiTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.MoneyFastApiTests.tearDownClass.__func__)
    register_owner = legacy.MoneyFastApiTests.register_owner
    _set_company_unlimited = legacy.MoneyFastApiTests._set_company_unlimited
    login = legacy.MoneyFastApiTests.login
    auth_headers = legacy.MoneyFastApiTests.auth_headers
    owner_session = legacy.MoneyFastApiTests.owner_session
    create_customer = legacy.MoneyFastApiTests.create_customer
    create_loan = legacy.MoneyFastApiTests.create_loan
    req = cash_tests.CashTests.req
    setUp = cash_tests.CashTests.setUp

    # --- helpers ---------------------------------------------------------------------------------------------------
    def post(self, body, headers=None, client=None):
        return (client or self.client).post(URL, json=body, headers=self.admin if headers is None else headers)

    def inject(self, amount="1000.00", **extra):
        r = self.post(dict(kind="injection", amount=amount, notes="semilla QA", **extra))
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def balance(self):
        return Decimal(self.client.get("/api/v1/capital", headers=self.admin).json()["balance"])

    def rows(self, note=None):
        with SessionLocal() as db:
            q = db.query(CapitalMovement)
            if note:
                q = q.filter(CapitalMovement.notes == note)
            return [(r.kind, r.amount, r.actor_id) for r in q.all()]

    def requests(self, scoped_key):
        with SessionLocal() as db:
            return db.query(CashRequest).filter(CashRequest.key == scoped_key).count()

    # --- A, B, C, F: replay, conflict, independent keys, stable answer ---------------------------------------------
    def test_replay_conflict_and_independent_keys(self):
        k = key()
        body = dict(kind="injection", amount="250.10", notes="aporte A", idempotency_key=k)
        first = self.post(body)
        again = self.post(body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(first.json(), again.json())  # deterministic: original id AND original balance
        self.assertEqual(again.headers.get("Idempotent-Replayed"), "true")
        self.assertNotIn("Idempotent-Replayed", first.headers)
        self.assertEqual(len(self.rows("aporte A")), 1)
        self.assertEqual(self.balance(), Decimal("250.10"))
        # The replay reports the ORIGINAL balance even after other movements happened.
        self.inject("100.00", idempotency_key=key())
        self.assertEqual(self.post(body).json(), first.json())
        # Same key, other amount / kind / note: conflict, nothing written.
        before = len(self.rows())
        for change in (dict(amount="250.11"), dict(kind="withdrawal"), dict(notes="otra nota")):
            r = self.post({**body, **change})
            self.assertEqual(r.status_code, 409, change)
        self.assertEqual(len(self.rows()), before)
        # Equivalent canonical amount ("250.1" == "250.10") is the SAME operation.
        self.assertEqual(self.post({**body, "amount": "250.1"}).json(), first.json())
        # Different keys are independent legitimate operations.
        self.post({**body, "idempotency_key": key()})
        self.assertEqual(len(self.rows("aporte A")), 2)
        self.assertEqual(self.balance(), Decimal("250.10") * 2 + Decimal("100.00"))

    def test_legacy_clients_without_key_keep_working_unprotected(self):
        body = dict(kind="injection", amount="5.00", notes="sin clave")
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(len(self.rows("sin clave")), 2)  # documented residual: no key, no protection

    # --- D: real concurrency ----------------------------------------------------------------------------------------
    def _concurrent(self, bodies, workers=None):
        workers = workers or len(bodies)
        barrier = threading.Barrier(workers)

        def run(body):
            with TestClient(app) as client:
                barrier.wait(timeout=30)
                r = client.post(URL, json=body, headers=self.admin)
                return r.status_code, r.json()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(run, bodies))

    def test_simultaneous_requests_with_one_key_create_one_movement(self):
        self.inject()
        for n in (2, 3, 6):
            k = key()
            body = dict(kind="injection", amount="7.25", notes=f"conc-{n}", idempotency_key=k)
            results = self._concurrent([body] * n)
            self.assertTrue(all(code == 200 for code, _ in results), results)
            self.assertEqual(len({r["id"] for _, r in results}), 1, results)
            self.assertEqual(len({r["balance"] for _, r in results}), 1, results)
            self.assertEqual(len(self.rows(f"conc-{n}")), 1)
            self.assertEqual(self.requests(f"capital:{k}"), 1)
        self.assertEqual(self.balance(), Decimal("1000.00") + Decimal("7.25") * 3)

    def test_simultaneous_conflicting_payloads_with_one_key_write_at_most_one(self):
        k = key()
        bodies = [dict(kind="injection", amount=f"{10 + i}.00", notes="choque", idempotency_key=k) for i in range(4)]
        results = self._concurrent(bodies)
        codes = sorted(code for code, _ in results)
        self.assertEqual(codes, [200, 409, 409, 409], results)
        self.assertEqual(len(self.rows("choque")), 1)

    def test_concurrent_withdrawals_never_overdraw(self):
        self.inject("100.00")
        bodies = [dict(kind="withdrawal", amount="30.00", notes="retiro conc", idempotency_key=key()) for _ in range(5)]
        results = self._concurrent(bodies)
        ok = [r for code, r in results if code == 200]
        refused = [code for code, _ in results if code == 409]
        self.assertEqual((len(ok), len(refused)), (3, 2), results)
        self.assertEqual(self.balance(), Decimal("10.00"))
        self.assertGreaterEqual(self.balance(), 0)

    # --- E, H: failure before commit, DB failure, rollback ----------------------------------------------------------
    def test_failure_before_commit_does_not_consume_the_key(self):
        k = key()
        body = dict(kind="injection", amount="40.00", notes="falla previa", idempotency_key=k)
        from app.api.routes import capital as route

        original = route.capital_service.record

        def boom(*args, **kwargs):
            original(*args, **kwargs)  # the movement IS added to the session...
            raise RuntimeError("fallo simulado antes del commit")

        with mock.patch.object(route.capital_service, "record", boom):
            with TestClient(app, raise_server_exceptions=False) as quiet:
                r = quiet.post(URL, json=body, headers=self.admin)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(self.rows("falla previa"), [])
        self.assertEqual(self.requests(f"capital:{k}"), 0)  # ...and nothing persisted, key is free
        ok = self.post(body)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(self.rows("falla previa")), 1)

    def test_database_failure_on_commit_rolls_back_movement_and_key(self):
        k = key()
        body = dict(kind="injection", amount="41.00", notes="falla commit", idempotency_key=k)
        from sqlalchemy.orm import Session

        real_commit = Session.commit
        calls = {"n": 0}

        def flaky(session):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("COMMIT", {}, Exception("conexión perdida (simulada)"))
            return real_commit(session)

        with mock.patch.object(Session, "commit", flaky):
            with TestClient(app, raise_server_exceptions=False) as quiet:
                r = quiet.post(URL, json=body, headers=self.admin)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(self.rows("falla commit"), [])
        self.assertEqual(self.requests(f"capital:{k}"), 0)
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(len(self.rows("falla commit")), 1)

    # --- G, I: scope and authorization -------------------------------------------------------------------------------
    def test_scope_authorization_and_tenant_isolation(self):
        k = key()
        body = dict(kind="injection", amount="60.00", notes="ambito", idempotency_key=k)
        created = self.post(body)
        self.assertEqual(created.status_code, 200)
        before = (len(self.rows()), self.requests(f"capital:{k}"))
        for who in ("manager", "cashier", "collector"):
            r = self.post(body, headers=self.roles[who])
            self.assertEqual(r.status_code, 403, who)
            self.assertNotIn('"balance"', r.text)  # a denied caller never sees the stored result
        self.assertEqual(self.post(body, headers={}).status_code, 401)
        self.assertEqual((len(self.rows()), self.requests(f"capital:{k}")), before)
        # Another tenant with the SAME key is an independent operation (never a replay of someone else's result).
        owner = self.client.post(
            "/api/v1/auth/register",
            json=dict(
                full_name="Otro Dueno",
                email="owner-cap@example.com",
                password="superpass123",
                company_name="Otra Empresa",
            ),
        )
        self.assertEqual(owner.status_code, 201, owner.text)
        other = self.auth_headers(self.login("owner-cap@example.com", "superpass123")["access_token"])
        r = self.post(body, headers=other)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotEqual(r.json()["id"], created.json()["id"])
        self.assertEqual(Decimal(self.client.get("/api/v1/capital", headers=other).json()["balance"]), Decimal("60.00"))
        self.assertEqual(self.balance(), Decimal("60.00"))

    # --- key validation, decimals, balances ---------------------------------------------------------------------------
    def test_invalid_keys_amounts_and_negative_balance_are_refused(self):
        base = dict(kind="injection", amount="10.00", notes="valid")
        for bad in ("", "corta", "x" * 73):
            self.assertEqual(self.post({**base, "idempotency_key": bad}).status_code, 422, bad)
        for amount in ("0", "-1.00", "1.005", "abc", "1000000000000.00"):
            self.assertEqual(self.post({**base, "amount": amount, "idempotency_key": key()}).status_code, 422, amount)
        self.assertEqual(self.rows(), [])
        refused = self.post(dict(kind="withdrawal", amount="0.01", notes="sin saldo", idempotency_key=key()))
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(self.rows("sin saldo"), [])

    def test_decimal_amounts_accumulate_exactly(self):
        for amount in ("0.10", "0.20", "0.30"):
            self.inject(amount, idempotency_key=key())
        self.assertEqual(self.balance(), Decimal("0.60"))
        self.assertEqual(sum(r[1] for r in self.rows()), Decimal("0.60"))

    def test_a_refused_operation_does_not_burn_its_key(self):
        k = key()
        no_funds = dict(kind="withdrawal", amount="5.00", notes="reintento", idempotency_key=k)
        self.assertEqual(self.post(no_funds).status_code, 409)
        self.assertEqual(self.requests(f"capital:{k}"), 0)
        self.inject("10.00", idempotency_key=key())
        self.assertEqual(self.post(no_funds).status_code, 200)  # same key, now legitimately possible


if __name__ == "__main__":
    unittest.main()
