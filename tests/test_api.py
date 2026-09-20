import os
import tempfile
import unittest
from pathlib import Path

temp_db = Path(tempfile.gettempdir()) / "moneyfast_test_suite.db"
if temp_db.exists():
    temp_db.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{temp_db.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ENVIRONMENT"] = "development"
# Keep tests hermetic: never hit real SMTP even if a .env configures it.
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_USER"] = ""
os.environ["SMTP_PASSWORD"] = ""

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import Base, engine
from app.main import app


class MoneyFastApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        if temp_db.exists():
            temp_db.unlink()

    def setUp(self) -> None:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    # ── helpers ────────────────────────────────────────────────────────────
    def register_owner(self, email: str = "owner@example.com", password: str = "superpass123") -> dict:
        response = self.client.post(
            "/api/v1/auth/register",
            json={
                "full_name": "Owner User",
                "email": email,
                "password": password,
                "company_name": "Prestamos MoneyFast",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        owner = response.json()
        # Tests create many users/customers/loans; put the company on an
        # unlimited plan so freemium limits don't constrain the fixtures.
        self._set_company_unlimited(owner["company_id"])
        return owner

    def _set_company_unlimited(self, company_id: int) -> None:
        from app.core.database import SessionLocal
        from app.models.company import Company
        from app.models.plan import Plan
        from app.services.plan_limits import seed_default_plans

        db = SessionLocal()
        try:
            seed_default_plans(db)
            unlimited = db.query(Plan).filter(Plan.customer_limit == 0).first()
            company = db.get(Company, company_id)
            if unlimited is not None and company is not None:
                company.plan_id = unlimited.id
                db.commit()
        finally:
            db.close()

    def login(self, email: str = "owner@example.com", password: str = "superpass123") -> dict:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password, "device_name": "test-suite"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def auth_headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    def owner_session(self) -> dict[str, str]:
        self.register_owner()
        data = self.login()
        return self.auth_headers(data["access_token"])

    def create_user(self, headers: dict, email: str, role: str, name: str = "Empleado Uno") -> dict:
        response = self.client.post(
            "/api/v1/users",
            json={"full_name": name, "email": email, "password": "workerpass123", "role": role},
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def create_customer(self, headers: dict, name: str = "Juan Perez", collector_id: int | None = None) -> dict:
        payload = {
            "full_name": name,
            "document_id": f"001-{len(self.client.get('/api/v1/customers', headers=headers).json()):07d}-1",
            "phone": "8095550001",
            "address": "Calle Primera #10",
            "notes": "Cliente de prueba",
        }
        if collector_id is not None:
            payload["assigned_collector_id"] = collector_id
        response = self.client.post("/api/v1/customers", json=payload, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def create_loan(self, headers: dict, customer_id: int, **overrides) -> dict:
        payload = {
            "customer_id": customer_id,
            "principal_amount": "10000.00",
            "interest_rate": "12.00",
            "late_fee_rate": "3.00",
            "grace_days": 0,
            "installment_count": 4,
            "payment_frequency": "weekly",
            # Future date so loans are not overdue by default (deterministic math).
            "start_date": "2026-12-01",
            "route_name": "Ruta Centro",
            "requires_promissory_note": False,
            "auto_approve": True,
        }
        payload.update(overrides)
        response = self.client.post("/api/v1/loans", json=payload, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    # ── auth / onboarding ──────────────────────────────────────────────────
    def test_auth_session_lifecycle(self) -> None:
        self.register_owner()
        login_data = self.login()

        me_response = self.client.get("/api/v1/auth/me", headers=self.auth_headers(login_data["access_token"]))
        self.assertEqual(me_response.status_code, 200, me_response.text)
        self.assertEqual(me_response.json()["email"], "owner@example.com")

        refresh_response = self.client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": login_data["refresh_token"]},
        )
        self.assertEqual(refresh_response.status_code, 200, refresh_response.text)
        refreshed = refresh_response.json()
        self.assertNotEqual(refreshed["refresh_token"], login_data["refresh_token"])

        logout_response = self.client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": refreshed["refresh_token"]},
        )
        self.assertEqual(logout_response.status_code, 204, logout_response.text)

        invalid_after_logout = self.client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refreshed["refresh_token"]},
        )
        self.assertEqual(invalid_after_logout.status_code, 401, invalid_after_logout.text)

    def test_register_creates_company_and_admin(self) -> None:
        owner = self.register_owner()
        self.assertEqual(owner["role"], "admin")
        self.assertIsNotNone(owner["company_id"])

    def test_freemium_default_plan_and_limits(self) -> None:
        # Register WITHOUT the unlimited upgrade so the company stays on the free plan.
        self.client.get("/api/v1/plans")  # ensure the plan catalog is seeded
        resp = self.client.post(
            "/api/v1/auth/register",
            json={
                "full_name": "Free Owner",
                "email": "free@example.com",
                "password": "freepass123",
                "company_name": "Empresa Free",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        headers = self.auth_headers(
            self.login("free@example.com", "freepass123")["access_token"]
        )

        # New company is on the free plan (Gratis) automatically.
        current = self.client.get("/api/v1/subscriptions/current", headers=headers).json()
        self.assertEqual(current["plan_name"], "Gratis")

        # Free plan allows 5 customers.
        for i in range(5):
            self.create_customer(headers, name=f"Cliente {i}")
        blocked = self.client.post(
            "/api/v1/customers",
            json={"full_name": "Uno de mas", "phone": "8095550009", "address": "Calle X"},
            headers=headers,
        )
        self.assertEqual(blocked.status_code, 402, blocked.text)

        # Free plan allows 3 users (the admin already counts as one), so two more
        # succeed and the next one is blocked.
        for i in range(2):
            ok_user = self.client.post(
                "/api/v1/users",
                json={"full_name": f"Empleado {i}", "email": f"emp{i}@example.com",
                      "password": "workerpass123", "role": "collector"},
                headers=headers,
            )
            self.assertEqual(ok_user.status_code, 201, ok_user.text)
        blocked_user = self.client.post(
            "/api/v1/users",
            json={"full_name": "Empleado extra", "email": "empextra@example.com",
                  "password": "workerpass123", "role": "collector"},
            headers=headers,
        )
        self.assertEqual(blocked_user.status_code, 402, blocked_user.text)

    def test_expired_paid_plan_downgrades_to_free_limits(self) -> None:
        # A company on a paid plan whose subscription has expired should fall
        # back to the free-tier limits (5 customers), not keep the paid limits.
        from datetime import UTC, datetime, timedelta

        from app.core.database import SessionLocal
        from app.models.company import Company
        from app.models.plan import Plan

        owner = self.register_owner(email="expired@example.com")
        headers = self.auth_headers(self.login("expired@example.com", "superpass123")["access_token"])
        self.client.get("/api/v1/plans", headers=headers)  # seed catalog

        db = SessionLocal()
        try:
            paid = db.query(Plan).filter(Plan.monthly_price_usd > 0).order_by(Plan.monthly_price_usd).first()
            company = db.get(Company, owner["company_id"])
            company.plan_id = paid.id  # paid plan …
            company.subscription_expires_at = datetime.now(UTC) - timedelta(days=1)  # … but expired
            db.commit()
        finally:
            db.close()

        # Free limits apply again: 5 customers allowed, 6th blocked.
        for i in range(5):
            self.create_customer(headers, name=f"Cliente {i}")
        blocked = self.client.post(
            "/api/v1/customers",
            json={"full_name": "Uno de mas", "phone": "8095550009", "address": "Calle X"},
            headers=headers,
        )
        self.assertEqual(blocked.status_code, 402, blocked.text)

        current = self.client.get("/api/v1/subscriptions/current", headers=headers).json()
        self.assertEqual(current["status"], "expired")

    def test_register_cannot_escalate_role(self) -> None:
        # Even if a role is supplied in the body, the server must ignore it.
        response = self.client.post(
            "/api/v1/auth/register",
            json={
                "full_name": "Attacker User",
                "email": "attacker@example.com",
                "password": "attackerpass123",
                "company_name": "Attacker Co",
                "role": "superadmin",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["role"], "admin")

        # And a self-registered admin must NOT reach superadmin-only endpoints.
        headers = self.auth_headers(self.login("attacker@example.com", "attackerpass123")["access_token"])
        companies_response = self.client.get("/api/v1/companies", headers=headers)
        self.assertEqual(companies_response.status_code, 403, companies_response.text)

    def test_password_reset_debug_code_hidden_outside_development(self) -> None:
        self.register_owner(email="reset@example.com")

        previous_environment = settings.environment
        settings.environment = "production"
        try:
            response = self.client.post(
                "/api/v1/auth/request-password-reset",
                json={"email": "reset@example.com"},
            )
        finally:
            settings.environment = previous_environment

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["debug_code"])
        self.assertEqual(body["email"], "reset@example.com")

    def test_password_reset_debug_code_present_in_development(self) -> None:
        self.register_owner(email="devreset@example.com")
        response = self.client.post(
            "/api/v1/auth/request-password-reset",
            json={"email": "devreset@example.com"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(response.json()["debug_code"])

    # ── loan / payment math ────────────────────────────────────────────────
    def test_customer_loan_and_composed_payment_flow(self) -> None:
        headers = self.owner_session()
        customer = self.create_customer(headers)
        loan = self.create_loan(headers, customer["id"])
        self.assertEqual(loan["status"], "active")

        payment_response = self.client.post(
            "/api/v1/payments",
            json={
                "loan_id": loan["id"],
                "installment_amount": "2800.00",
                "principal_amount": "200.00",
                "interest_amount": "50.00",
                "custom_amount": "0.00",
                "notes": "Cobro mixto",
            },
            headers=headers,
        )
        self.assertEqual(payment_response.status_code, 201, payment_response.text)
        payment = payment_response.json()
        self.assertEqual(payment["payment_type"], "custom")
        self.assertEqual(payment["amount"], "3050.00")
        self.assertEqual(payment["collector_name"], "Owner User")

        loan_detail = self.client.get(f"/api/v1/loans/{loan['id']}", headers=headers).json()
        self.assertEqual(loan_detail["principal_balance"], "7300.00")
        self.assertEqual(loan_detail["interest_balance"], "850.00")
        self.assertEqual(loan_detail["late_fee_balance"], "0.00")

    def test_interest_only_payment(self) -> None:
        headers = self.owner_session()
        customer = self.create_customer(headers)
        loan = self.create_loan(headers, customer["id"])

        response = self.client.post(
            "/api/v1/payments",
            json={"loan_id": loan["id"], "payment_type": "interest_only", "amount": "500.00"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        payment = response.json()
        self.assertEqual(payment["interest_applied"], "500.00")
        self.assertEqual(payment["principal_applied"], "0.00")

        loan_detail = self.client.get(f"/api/v1/loans/{loan['id']}", headers=headers).json()
        self.assertEqual(loan_detail["principal_balance"], "10000.00")
        self.assertEqual(loan_detail["interest_balance"], "700.00")

    def test_principal_only_payment(self) -> None:
        headers = self.owner_session()
        customer = self.create_customer(headers)
        loan = self.create_loan(headers, customer["id"])

        response = self.client.post(
            "/api/v1/payments",
            json={"loan_id": loan["id"], "payment_type": "principal_only", "amount": "1000.00"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        payment = response.json()
        self.assertEqual(payment["principal_applied"], "1000.00")
        self.assertEqual(payment["interest_applied"], "0.00")

        loan_detail = self.client.get(f"/api/v1/loans/{loan['id']}", headers=headers).json()
        self.assertEqual(loan_detail["principal_balance"], "9000.00")
        self.assertEqual(loan_detail["interest_balance"], "1200.00")

    def test_late_fee_accrual_marks_loan_late(self) -> None:
        headers = self.owner_session()
        customer = self.create_customer(headers)
        # start far in the past so installments are overdue → late fees accrue.
        loan = self.create_loan(
            headers, customer["id"], start_date="2026-01-01", late_fee_rate="5.00", grace_days=0
        )

        loan_detail = self.client.get(f"/api/v1/loans/{loan['id']}", headers=headers).json()
        self.assertEqual(loan_detail["status"], "late")
        self.assertGreater(float(loan_detail["late_fee_balance"]), 0.0)

    # ── collector (asesor) scoping ─────────────────────────────────────────
    def test_collector_only_sees_assigned_portfolio(self) -> None:
        headers = self.owner_session()
        col1 = self.create_user(headers, "col1@example.com", "collector", "Cobrador Uno")
        col2 = self.create_user(headers, "col2@example.com", "collector", "Cobrador Dos")

        cust1 = self.create_customer(headers, "Cliente Uno", collector_id=col1["id"])
        cust2 = self.create_customer(headers, "Cliente Dos", collector_id=col2["id"])
        loan1 = self.create_loan(headers, cust1["id"])
        loan2 = self.create_loan(headers, cust2["id"])

        col1_headers = self.auth_headers(self.login("col1@example.com", "workerpass123")["access_token"])

        # Collector 1 sees only their own customer and loan.
        customers = self.client.get("/api/v1/customers", headers=col1_headers).json()
        self.assertEqual([c["id"] for c in customers], [cust1["id"]])

        loans = self.client.get("/api/v1/loans", headers=col1_headers).json()
        self.assertEqual([l["id"] for l in loans], [loan1["id"]])

        # Collector 1 cannot fetch collector 2's loan.
        self.assertEqual(
            self.client.get(f"/api/v1/loans/{loan2['id']}", headers=col1_headers).status_code, 404
        )

        # Collector 1 can pay their own loan.
        ok = self.client.post(
            "/api/v1/payments",
            json={"loan_id": loan1["id"], "payment_type": "interest_only", "amount": "100.00"},
            headers=col1_headers,
        )
        self.assertEqual(ok.status_code, 201, ok.text)

        # ...but cannot pay collector 2's loan.
        denied = self.client.post(
            "/api/v1/payments",
            json={"loan_id": loan2["id"], "payment_type": "interest_only", "amount": "100.00"},
            headers=col1_headers,
        )
        self.assertEqual(denied.status_code, 404, denied.text)

    def test_collector_cannot_create_customers_or_loans(self) -> None:
        headers = self.owner_session()
        self.create_user(headers, "col@example.com", "collector", "Cobrador")
        col_headers = self.auth_headers(self.login("col@example.com", "workerpass123")["access_token"])

        response = self.client.post(
            "/api/v1/customers",
            json={"full_name": "X Y", "phone": "8090000000", "address": "Calle X #1"},
            headers=col_headers,
        )
        self.assertEqual(response.status_code, 403, response.text)

    # ── owner / superadmin panel ───────────────────────────────────────────
    def create_superadmin_session(self, email: str = "root@platform.com", password: str = "rootpass123") -> dict[str, str]:
        from app.core.database import SessionLocal
        from app.core.security import get_password_hash
        from app.models.user import User, UserRole

        db = SessionLocal()
        try:
            if db.query(User).filter_by(email=email).first() is None:
                db.add(User(
                    full_name="Platform Owner",
                    email=email,
                    password_hash=get_password_hash(password),
                    role=UserRole.superadmin,
                    company_id=None,
                ))
                db.commit()
        finally:
            db.close()
        return self.auth_headers(self.login(email, password)["access_token"])

    def create_company_via_api(self, headers: dict, name: str, admin_email: str) -> dict:
        response = self.client.post(
            "/api/v1/companies",
            json={
                "name": name,
                "admin_full_name": "Company Admin",
                "admin_email": admin_email,
                "admin_password": "adminpass123",
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_record_subscription_activates_plan_and_lists(self) -> None:
        sa_headers = self.create_superadmin_session()
        company = self.create_company_via_api(sa_headers, "Empresa A", "admin-a@example.com")

        from app.core.database import SessionLocal
        from app.models.company import Company
        from app.models.plan import Plan
        from app.services.subscription_service import record_subscription

        # Ensure plans are seeded via the API, then record a subscription directly.
        self.client.get("/api/v1/plans", headers=sa_headers)
        db = SessionLocal()
        try:
            plan = db.query(Plan).filter(Plan.monthly_price_usd > 0).order_by(Plan.monthly_price_usd).first()
            company_obj = db.get(Company, company["id"])
            record_subscription(db, company=company_obj, plan=plan, order_id="TEST-ORDER-1")
            db.commit()
            refreshed = db.get(Company, company["id"])
            self.assertEqual(refreshed.plan_id, plan.id)
            self.assertIsNotNone(refreshed.subscription_expires_at)
            plan_price = str(plan.monthly_price_usd)
        finally:
            db.close()

        subs = self.client.get("/api/v1/subscriptions", headers=sa_headers)
        self.assertEqual(subs.status_code, 200, subs.text)
        body = subs.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["company_name"], "Empresa A")
        self.assertEqual(body[0]["amount_usd"], plan_price)

        # Regression: the overview must not 500 once a company has a
        # subscription_expires_at (naive-vs-aware datetime comparison), and the
        # payment must be reflected in the monthly revenue.
        overview = self.client.get("/api/v1/companies/overview", headers=sa_headers)
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertEqual(overview.json()["revenue_this_month_usd"], plan_price)

    def test_subscriptions_list_is_superadmin_only(self) -> None:
        # A company admin must not see the platform-wide revenue log.
        sa_headers = self.create_superadmin_session()
        self.create_company_via_api(sa_headers, "Empresa B", "admin-b@example.com")
        admin_headers = self.auth_headers(self.login("admin-b@example.com", "adminpass123")["access_token"])
        response = self.client.get("/api/v1/subscriptions", headers=admin_headers)
        self.assertEqual(response.status_code, 403, response.text)

    def test_platform_overview_shape(self) -> None:
        sa_headers = self.create_superadmin_session()
        self.create_company_via_api(sa_headers, "Empresa C", "admin-c@example.com")
        response = self.client.get("/api/v1/companies/overview", headers=sa_headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        for key in ("total_companies", "active_companies", "total_users", "mrr_usd", "revenue_this_month_usd", "expiring_soon"):
            self.assertIn(key, body)
        self.assertGreaterEqual(body["total_companies"], 1)

    def test_delete_company_guard(self) -> None:
        sa_headers = self.create_superadmin_session()
        with_data = self.create_company_via_api(sa_headers, "Con Datos", "admin-d@example.com")
        empty = self.create_company_via_api(sa_headers, "Vacia", "admin-e@example.com")

        # Give the first company a customer, so it can no longer be hard-deleted.
        admin_headers = self.auth_headers(self.login("admin-d@example.com", "adminpass123")["access_token"])
        self.create_customer(admin_headers, "Cliente X")

        blocked = self.client.delete(f"/api/v1/companies/{with_data['id']}", headers=sa_headers)
        self.assertEqual(blocked.status_code, 400, blocked.text)

        ok = self.client.delete(f"/api/v1/companies/{empty['id']}", headers=sa_headers)
        self.assertEqual(ok.status_code, 204, ok.text)

    def test_delete_plan_guard(self) -> None:
        sa_headers = self.create_superadmin_session()
        company = self.create_company_via_api(sa_headers, "Empresa F", "admin-f@example.com")
        plans = self.client.get("/api/v1/plans", headers=sa_headers).json()
        used_plan, free_plan = plans[0], plans[-1]

        # Assign a plan to the company → it can no longer be deleted.
        self.client.put(
            f"/api/v1/companies/{company['id']}",
            json={"name": "Empresa F", "plan_id": used_plan["id"]},
            headers=sa_headers,
        )
        blocked = self.client.delete(f"/api/v1/plans/{used_plan['id']}", headers=sa_headers)
        self.assertEqual(blocked.status_code, 400, blocked.text)

        # Create a fresh unused plan and delete it.
        created = self.client.post(
            "/api/v1/plans",
            json={"name": "Plan Temporal", "customer_limit": 1, "loan_limit": 1, "user_limit": 1, "monthly_price_usd": "9.99"},
            headers=sa_headers,
        ).json()
        ok = self.client.delete(f"/api/v1/plans/{created['id']}", headers=sa_headers)
        self.assertEqual(ok.status_code, 204, ok.text)

    # ── rutas de cobro ──────────────────────────────────────────────────────
    def create_route(self, headers: dict, name: str, collector_id: int | None = None) -> dict:
        response = self.client.post(
            "/api/v1/routes",
            json={"name": name, "zone": "Centro", "assigned_collector_id": collector_id},
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_customer_inherits_collector_from_route(self) -> None:
        headers = self.owner_session()
        collector = self.create_user(headers, "colr@example.com", "collector", "Cobrador Ruta")
        route = self.create_route(headers, "Ruta Centro", collector_id=collector["id"])
        self.assertEqual(route["collector_name"], "Cobrador Ruta")

        # Creating a customer on the route derives the collector from the route.
        response = self.client.post(
            "/api/v1/customers",
            json={"full_name": "Cliente Ruta", "phone": "8090000000", "address": "Calle 1 #2", "route_id": route["id"]},
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        customer = response.json()
        self.assertEqual(customer["route_id"], route["id"])
        self.assertEqual(customer["assigned_collector_id"], collector["id"])
        self.assertEqual(customer["route_name"], "Ruta Centro")

    def test_changing_route_collector_reassigns_portfolio(self) -> None:
        headers = self.owner_session()
        col1 = self.create_user(headers, "c1@example.com", "collector", "Cobrador Uno")
        col2 = self.create_user(headers, "c2@example.com", "collector", "Cobrador Dos")
        route = self.create_route(headers, "Ruta Norte", collector_id=col1["id"])

        cust = self.client.post(
            "/api/v1/customers",
            json={"full_name": "Cliente N", "phone": "8090000001", "address": "Calle 3 #4", "route_id": route["id"]},
            headers=headers,
        ).json()
        self.assertEqual(cust["assigned_collector_id"], col1["id"])

        # Reassign the route to another collector → the whole portfolio moves.
        upd = self.client.put(
            f"/api/v1/routes/{route['id']}",
            json={"name": "Ruta Norte", "zone": "Norte", "assigned_collector_id": col2["id"]},
            headers=headers,
        )
        self.assertEqual(upd.status_code, 200, upd.text)

        refreshed = self.client.get(f"/api/v1/customers/{cust['id']}", headers=headers).json()
        self.assertEqual(refreshed["assigned_collector_id"], col2["id"])

        # The new collector now sees the customer; the old one does not.
        c2_headers = self.auth_headers(self.login("c2@example.com", "workerpass123")["access_token"])
        c2_list = self.client.get("/api/v1/customers", headers=c2_headers).json()
        self.assertEqual([c["id"] for c in c2_list], [cust["id"]])

    def test_delete_route_guard(self) -> None:
        headers = self.owner_session()
        collector = self.create_user(headers, "cg@example.com", "collector", "Cobrador G")
        with_customers = self.create_route(headers, "Ruta Con Clientes", collector_id=collector["id"])
        empty = self.create_route(headers, "Ruta Vacia")

        self.client.post(
            "/api/v1/customers",
            json={"full_name": "Cliente G", "phone": "8090000002", "address": "Calle 5 #6", "route_id": with_customers["id"]},
            headers=headers,
        )
        blocked = self.client.delete(f"/api/v1/routes/{with_customers['id']}", headers=headers)
        self.assertEqual(blocked.status_code, 400, blocked.text)

        ok = self.client.delete(f"/api/v1/routes/{empty['id']}", headers=headers)
        self.assertEqual(ok.status_code, 204, ok.text)

    def test_route_stops_ordered_and_split_by_gps(self) -> None:
        headers = self.owner_session()
        collector = self.create_user(headers, "cs@example.com", "collector", "Cobrador S")
        route = self.create_route(headers, "Ruta Mapa", collector_id=collector["id"])

        # Three located customers (increasing longitude) + one without GPS.
        def add(name: str, lat: float | None, lng: float | None) -> None:
            body = {"full_name": name, "phone": "8090000000", "address": "Calle de prueba", "route_id": route["id"]}
            if lat is not None:
                body["latitude"] = str(lat)
                body["longitude"] = str(lng)
            r = self.client.post("/api/v1/customers", json=body, headers=headers)
            self.assertEqual(r.status_code, 201, r.text)

        add("Lejos", 19.0, -70.0)
        add("Medio", 19.0, -70.5)
        add("Cerca", 19.0, -71.0)
        add("Sin GPS", None, None)

        resp = self.client.get(f"/api/v1/routes/{route['id']}/stops", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body["stops"]), 3)
        self.assertEqual(len(body["unlocated"]), 1)
        self.assertEqual(body["unlocated"][0]["full_name"], "Sin GPS")
        # Sequence numbers are 1..N and each stop is located.
        self.assertEqual([s["sequence"] for s in body["stops"]], [1, 2, 3])

    def test_route_areas_created_updated_and_suggested(self) -> None:
        headers = self.owner_session()
        collector = self.create_user(headers, "areas@example.com", "collector", "Cobrador Areas")
        route = self.client.post(
            "/api/v1/routes",
            json={
                "name": "Ruta Este",
                "zone": "Este",
                "assigned_collector_id": collector["id"],
                "areas": [
                    {"area_type": "sector", "name": "Sector A"},
                    {"area_type": "calle", "name": "Calle 3"},
                ],
            },
            headers=headers,
        )
        self.assertEqual(route.status_code, 201, route.text)
        body = route.json()
        self.assertEqual(len(body["areas"]), 2)
        self.assertEqual({a["name"] for a in body["areas"]}, {"Sector A", "Calle 3"})

        # Suggest-route matches case/whitespace-insensitively against a customer's
        # sector/calle/barrio, without assigning anything by itself.
        suggestion = self.client.get(
            "/api/v1/routes/suggest",
            params={"sector": "  sector a  ", "calle": "otra calle", "barrio": "Barrio X"},
            headers=headers,
        )
        self.assertEqual(suggestion.status_code, 200, suggestion.text)
        suggestions = suggestion.json()
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["route_id"], body["id"])
        self.assertEqual(suggestions[0]["score"], 1)

        # No match at all → empty suggestion list, never a guess.
        none_matched = self.client.get(
            "/api/v1/routes/suggest",
            params={"sector": "Sector Z"},
            headers=headers,
        )
        self.assertEqual(none_matched.json(), [])

        # Updating a route's areas replaces the previous set entirely.
        updated = self.client.put(
            f"/api/v1/routes/{body['id']}",
            json={
                "name": "Ruta Este",
                "zone": "Este",
                "assigned_collector_id": collector["id"],
                "areas": [{"area_type": "barrio", "name": "Barrio Nuevo"}],
            },
            headers=headers,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual([a["name"] for a in updated.json()["areas"]], ["Barrio Nuevo"])

    def test_route_stops_expose_loan_status(self) -> None:
        headers = self.owner_session()
        collector = self.create_user(headers, "stopsloan@example.com", "collector", "Cobrador Loan")
        route = self.create_route(headers, "Ruta Cobro", collector_id=collector["id"])

        with_loan = self.create_customer(headers, "Con Prestamo")
        upd1 = self.client.put(
            f"/api/v1/customers/{with_loan['id']}",
            json={**with_loan, "route_id": route["id"], "version": with_loan["version"]},
            headers=headers,
        )
        self.assertEqual(upd1.status_code, 200, upd1.text)
        without_loan = self.create_customer(headers, "Sin Prestamo")
        upd2 = self.client.put(
            f"/api/v1/customers/{without_loan['id']}",
            json={**without_loan, "route_id": route["id"], "version": without_loan["version"]},
            headers=headers,
        )
        self.assertEqual(upd2.status_code, 200, upd2.text)
        self.create_loan(headers, with_loan["id"])

        resp = self.client.get(f"/api/v1/routes/{route['id']}/stops", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        unlocated_by_name = {u["full_name"]: u["loan_status"] for u in resp.json()["unlocated"]}
        self.assertEqual(unlocated_by_name["Con Prestamo"], "active")
        self.assertIsNone(unlocated_by_name["Sin Prestamo"])

    def test_live_location_update_and_list(self) -> None:
        headers = self.owner_session()
        collector = self.create_user(headers, "loc@example.com", "collector", "Cobrador Loc")
        col_headers = self.auth_headers(self.login("loc@example.com", "workerpass123")["access_token"])

        # Collector posts its position.
        upd = self.client.post(
            "/api/v1/users/me/location",
            json={"latitude": "19.451000", "longitude": "-70.697000"},
            headers=col_headers,
        )
        self.assertEqual(upd.status_code, 204, upd.text)

        # Admin sees the collector with its last known position.
        resp = self.client.get("/api/v1/users/locations", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        me = next(c for c in body if c["id"] == collector["id"])
        self.assertEqual(me["last_lat"], "19.451000")
        self.assertIsNotNone(me["last_location_at"])

    def test_location_history_track(self) -> None:
        from datetime import UTC, datetime

        headers = self.owner_session()
        collector = self.create_user(headers, "trk@example.com", "collector", "Cobrador Track")
        col_headers = self.auth_headers(self.login("trk@example.com", "workerpass123")["access_token"])

        # Three positions that are far apart → three breadcrumbs.
        for lat, lng in [("19.450000", "-70.700000"), ("19.455000", "-70.695000"), ("19.460000", "-70.690000")]:
            r = self.client.post("/api/v1/users/me/location", json={"latitude": lat, "longitude": lng}, headers=col_headers)
            self.assertEqual(r.status_code, 204, r.text)
        # A duplicate of the last position must NOT add a breadcrumb (throttled).
        self.client.post("/api/v1/users/me/location", json={"latitude": "19.460000", "longitude": "-70.690000"}, headers=col_headers)

        today = datetime.now(UTC).date().isoformat()
        resp = self.client.get(f"/api/v1/users/{collector['id']}/track", params={"date": today}, headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body["points"]), 3)
        self.assertGreater(body["distance_km"], 0.0)
        self.assertIsNotNone(body["started_at"])
        self.assertIsNotNone(body["ended_at"])

    def test_track_requires_admin_manager(self) -> None:
        from datetime import UTC, datetime

        headers = self.owner_session()
        collector = self.create_user(headers, "trk2@example.com", "collector", "Cobrador Track2")
        col_headers = self.auth_headers(self.login("trk2@example.com", "workerpass123")["access_token"])
        today = datetime.now(UTC).date().isoformat()
        resp = self.client.get(f"/api/v1/users/{collector['id']}/track", params={"date": today}, headers=col_headers)
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_locations_list_requires_admin_manager(self) -> None:
        headers = self.owner_session()
        self.create_user(headers, "loc2@example.com", "collector", "Cobrador Loc2")
        col_headers = self.auth_headers(self.login("loc2@example.com", "workerpass123")["access_token"])
        # A collector cannot list everyone's positions.
        resp = self.client.get("/api/v1/users/locations", headers=col_headers)
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_branch_assignment_and_delete_guard(self) -> None:
        headers = self.owner_session()
        collector = self.create_user(headers, "colb@example.com", "collector", "Cobrador B")

        # Create a branch.
        b = self.client.post(
            "/api/v1/branches",
            json={"name": "Sucursal Centro", "address": "Calle 1", "manager_name": "Ana", "notary_name": "Lic", "phone": "8095550001"},
            headers=headers,
        )
        self.assertEqual(b.status_code, 201, b.text)
        branch = b.json()

        # Route belongs to the branch.
        route = self.client.post(
            "/api/v1/routes",
            json={"name": "Ruta Suc", "assigned_collector_id": collector["id"], "branch_id": branch["id"]},
            headers=headers,
        )
        self.assertEqual(route.status_code, 201, route.text)
        self.assertEqual(route.json()["branch_id"], branch["id"])
        self.assertEqual(route.json()["branch_name"], "Sucursal Centro")

        # Cannot delete a branch that has a route.
        blocked = self.client.delete(f"/api/v1/branches/{branch['id']}", headers=headers)
        self.assertEqual(blocked.status_code, 400, blocked.text)

        # An empty branch can be deleted.
        empty = self.client.post(
            "/api/v1/branches",
            json={"name": "Sucursal Vacia", "address": "Calle vacia", "manager_name": "Yon", "notary_name": "Zoe", "phone": "8095550002"},
            headers=headers,
        ).json()
        ok = self.client.delete(f"/api/v1/branches/{empty['id']}", headers=headers)
        self.assertEqual(ok.status_code, 204, ok.text)

    def test_user_branch_assignment(self) -> None:
        headers = self.owner_session()
        b = self.client.post(
            "/api/v1/branches",
            json={"name": "Sucursal Norte", "address": "Calle norte", "manager_name": "Mimi", "notary_name": "Nino", "phone": "8095550003"},
            headers=headers,
        ).json()
        resp = self.client.post(
            "/api/v1/users",
            json={"full_name": "Cobrador Suc", "email": "cs@example.com", "password": "workerpass123", "role": "collector", "branch_id": b["id"]},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["branch_id"], b["id"])
        self.assertEqual(resp.json()["branch_name"], "Sucursal Norte")

    def test_customer_gps_is_stored(self) -> None:
        headers = self.owner_session()
        response = self.client.post(
            "/api/v1/customers",
            json={
                "full_name": "Cliente GPS", "phone": "8090000003", "address": "Calle 7 #8",
                "latitude": "19.451230", "longitude": "-70.697100",
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["latitude"], "19.451230")
        self.assertEqual(body["longitude"], "-70.697100")

    def test_assign_invalid_collector_is_rejected(self) -> None:
        headers = self.owner_session()
        manager = self.create_user(headers, "mgr@example.com", "manager", "Gerente")
        # Assigning a non-collector (the manager) as collector must fail.
        response = self.client.post(
            "/api/v1/customers",
            json={
                "full_name": "Cliente Z",
                "phone": "8090000000",
                "address": "Calle Z #1",
                "assigned_collector_id": manager["id"],
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 400, response.text)


    def application_fixture(self, headers, modality="unsecured"):
        from app.schemas.loan_application import SECTIONS
        values = {}
        for section in SECTIONS:
            for field in section["fields"]:
                kind = field["kind"]
                values[field["key"]] = (True if kind == "checkbox" else "2000-01-01" if kind == "date"
                    else "1000" if kind == "money" else "6" if kind == "integer"
                    else field["options"][0] if kind == "select" else "persona@example.com" if kind == "email" else "Ejemplo")
        values.update(full_name="Solicitante de prueba", document_id="000-0000000-1", monthly_income="30000",
            other_income="0", total_income="30000", requested_amount="12000", has_other_loans="No")
        existing = self.client.get("/api/v1/customers?q=00000000001", headers=headers).json()
        link = {"customer_id": existing[0]["id"], "customer_version": existing[0]["version"]} if existing else {"create_customer": True}
        result = self.client.post("/api/v1/loan-applications", json={"modality": modality, "data": values, **link}, headers=headers)
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def app_action(self, headers, application, action, **extra):
        return self.client.post(f'/api/v1/loan-applications/{application["id"]}/transition',
            headers=headers, json={"version": application["version"], "action": action, **extra})

    def app_document(self, headers, application, category):
        import base64
        result = self.client.post(f'/api/v1/loan-applications/{application["id"]}/documents/{category}',
            headers=headers, json={"version": application["version"], "filename": "documento.pdf",
                "content_base64": base64.b64encode(b"%PDF-1.4\nTest fixture\n%%EOF").decode()})
        self.assertEqual(result.status_code, 200, result.text)
        application = result.json()
        doc = next(d for d in application["documents"] if d["category"] == category)
        result = self.client.post(f'/api/v1/loan-applications/{application["id"]}/documents/{doc["id"]}/review',
            headers=headers, json={"version": application["version"], "verified": True})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_application_lifecycle_both_modalities(self):
        from app.schemas.loan_application import DOCUMENTS
        headers = self.owner_session()
        for modality in ("unsecured", "secured"):
            application = self.application_fixture(headers, modality)
            response = self.app_action(headers, application, "disburse")
            self.assertEqual(response.status_code, 409)
            application = self.app_action(headers, application, "submit").json()
            self.assertEqual(self.app_action(headers, application, "evaluate").status_code, 422)
            for document in DOCUMENTS:
                if document["required"] and (not document.get("secured") or modality == "secured"):
                    application = self.app_document(headers, application, document["key"])
            response = self.app_action(headers, application, "evaluate")
            self.assertEqual(response.status_code, 200, response.text)
            application = response.json()
            self.assertIsNone(application["loan_id"])
            response = self.app_action(headers, application, "approve", notes="Evaluación manual completada",
                terms={"interest_rate": "10", "installment_count": 6})
            self.assertEqual(response.status_code, 200, response.text)
            application = response.json()
            self.assertEqual(self.app_action(headers, application, "sign").status_code, 422)
            application = self.app_document(headers, application, "contract")
            application = self.app_action(headers, application, "sign").json()
            from datetime import date, timedelta
            response = self.app_action(headers, application, "disburse", first_payment_date=str(date.today() + timedelta(days=30)), disbursement_reference="REC-TEST")
            self.assertEqual(response.status_code, 200, response.text)
            paid_out = response.json()
            self.assertEqual(paid_out["status"], "disbursed")
            loan = self.client.get(f'/api/v1/loans/{paid_out["loan_id"]}', headers=headers).json()
            self.assertEqual(loan["status"], "active")
            self.assertEqual(len(loan["installments"]), 6)
            self.assertEqual(self.app_action(headers, application, "disburse").status_code, 409)

    def test_application_validation_and_edit_lock(self):
        headers = self.owner_session()
        self.assertEqual(self.client.post('/api/v1/loan-applications', headers=headers, json={"modality": "unsecured"}).status_code, 422)
        self.assertEqual(self.client.post('/api/v1/loan-applications', headers=headers,
            json={"modality": "fiador"}).status_code, 422)
        application = self.application_fixture(headers, "secured")
        self.assertEqual(self.client.put(f'/api/v1/loan-applications/{application["id"]}', headers=headers,
            json={"modality": "secured", "version": application["version"], "data": {"requested_amount": "NaN"}}).status_code, 422)
        application = self.app_action(headers, application, "submit").json()
        self.assertEqual(self.client.put(f'/api/v1/loan-applications/{application["id"]}', headers=headers,
            json={"modality": "secured", "version": application["version"], "data": application["data"]}).status_code, 409)
        self.assertEqual(self.app_action(headers, application, "reject").status_code, 409)
        result = self.app_action(headers, application, "return", notes="Corregir domicilio")
        self.assertEqual(result.json()["status"], "draft")
        edited = self.client.put(f'/api/v1/loan-applications/{application["id"]}', headers=headers,
            json={"modality": "unsecured", "version": result.json()["version"], "data": application["data"], "customer_id": application["customer_id"], "customer_version": application["customer_version"]})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertNotIn("collateral_value", edited.json()["data"])

    def test_application_tenant_and_role_isolation(self):
        headers = self.owner_session()
        application = self.application_fixture(headers)
        application = self.app_document(headers, application, "identity")
        doc = application["documents"][0]
        self.register_owner(email="other@example.com")
        other = self.auth_headers(self.login(email="other@example.com")["access_token"])
        path = f'/api/v1/loan-applications/{application["id"]}'
        self.assertEqual(self.client.get(path, headers=other).status_code, 404)
        self.assertEqual(self.client.get(path + f'/documents/{doc["id"]}/content', headers=other).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/loan-applications', headers=other).json(), [])
        self.create_user(headers, "collector@example.com", "collector")
        collector = self.auth_headers(self.login("collector@example.com", "workerpass123")["access_token"])
        self.assertEqual(self.client.get(path, headers=collector).status_code, 403)
        self.assertEqual(self.client.get('/api/v1/loan-applications/form', headers=collector).status_code, 403)

    def test_application_file_validation_and_stale_update(self):
        headers = self.owner_session()
        application = self.application_fixture(headers)
        path = f'/api/v1/loan-applications/{application["id"]}'
        bad = self.client.post(path + '/documents/identity', headers=headers,
            json={"version": application["version"], "filename": "archivo.html", "content_base64": "PGh0bWw+"})
        self.assertEqual(bad.status_code, 422)
        updated = self.app_document(headers, application, "identity")
        stale = self.client.put(path, headers=headers, json={"version": application["version"], "modality": "unsecured", "data": application["data"]})
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(updated["documents"][0]["verified"])

    def test_application_print_and_conditional_validation(self):
        headers = self.owner_session()
        response = self.client.post('/api/v1/loan-applications', headers=headers,
            json={"modality":"secured", "create_customer": True, "data":{"full_name":"<script>alert(1)</script>", "phone":"8095555555", "address":"Calle de prueba",
                "collateral_owner":"Tercero", "has_other_loans":"Sí"}})
        self.assertEqual(response.status_code, 201, response.text)
        application = response.json()
        result = self.app_action(headers, application, "submit")
        self.assertEqual(result.status_code, 422)
        self.assertIn("Nombre del propietario tercero", result.json()["detail"])
        self.assertIn("Pago mensual de otros créditos", result.json()["detail"])
        printed = self.client.get(f'/api/v1/loan-applications/{application["id"]}/print', headers=headers)
        self.assertEqual(printed.status_code, 200, printed.text)
        self.assertNotIn("<script>", printed.json()["html"])
        self.assertIn("&lt;script&gt;", printed.json()["html"])
        self.assertIn("Firma del solicitante", printed.json()["html"])
        self.assertIn("CON GARANTÍA", printed.json()["html"])


    def test_customer_profile_snapshot_and_atomic_sync(self):
        headers = self.owner_session()
        first = self.application_fixture(headers)
        first = self.app_action(headers, first, "submit").json()
        second = self.application_fixture(headers, "secured")
        customer_id = first["customer_id"]
        self.assertEqual(second["customer_id"], customer_id)
        customer = self.client.get(f"/api/v1/customers/{customer_id}", headers=headers).json()
        body = {**customer, "notes": "Notas que se conservan", "latitude": "18.500000", "longitude": "-69.900000"}
        result = self.client.put(f"/api/v1/customers/{customer_id}", headers=headers, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        customer = result.json()
        values = {**second["data"], "phone": "8095559999", "email": "nuevo@example.com", "home_phone": "8095558888", "reference_name": "Referencia nueva"}
        payload = dict(modality="secured", data=values, customer_id=customer_id, customer_version=customer["version"], version=second["version"])
        updated = self.client.put(f'/api/v1/loan-applications/{second["id"]}', headers=headers, json=payload)
        self.assertEqual(updated.status_code, 200, updated.text)
        profile = self.client.get(f"/api/v1/customers/{customer_id}", headers=headers).json()
        self.assertEqual(profile["phone"], "8095559999")
        self.assertEqual(profile["email"], "nuevo@example.com")
        self.assertEqual(profile["home_phone"], "8095558888")
        self.assertEqual(profile["references"][0]["nombre"], "Referencia nueva")
        self.assertEqual(profile["notes"], body["notes"])
        self.assertEqual(profile["latitude"], "18.500000")
        self.assertEqual(updated.json()["customer_version"], profile["version"])
        self.assertEqual(self.client.get(f'/api/v1/loan-applications/{first["id"]}', headers=headers).json()["data"], first["data"])
        self.assertEqual(len(self.client.get(f"/api/v1/loan-applications?customer_id={customer_id}", headers=headers).json()), 2)
        self.assertEqual(len(self.client.get("/api/v1/customers", headers=headers).json()), 1)

    def test_customer_profile_stale_draft_and_failed_commit_roll_back(self):
        from unittest.mock import patch
        from sqlalchemy.orm.exc import StaleDataError
        headers = self.owner_session()
        a = self.application_fixture(headers)
        customer = self.client.get(f'/api/v1/customers/{a["customer_id"]}', headers=headers).json()
        path = f'/api/v1/loan-applications/{a["id"]}'
        payload = dict(modality=a["modality"], data={**a["data"], "phone": "8095557777"}, customer_id=a["customer_id"], customer_version=a["customer_version"], version=a["version"])
        with patch("sqlalchemy.orm.Session.commit", side_effect=StaleDataError("simulated concurrent application write")):
            self.assertEqual(self.client.put(path, headers=headers, json=payload).status_code, 409)
        self.assertEqual(self.client.get(f'/api/v1/customers/{a["customer_id"]}', headers=headers).json(), customer)
        self.assertEqual(self.client.get(path, headers=headers).json(), a)
        fresh = self.client.put(f'/api/v1/customers/{a["customer_id"]}', headers=headers, json={**customer, "phone": "8095556666"})
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertEqual(self.client.put(path, headers=headers, json=payload).status_code, 409)
        self.assertEqual(self.client.put(f'/api/v1/customers/{a["customer_id"]}', headers=headers, json=customer).status_code, 409)
        self.assertEqual(self.client.get(path, headers=headers).json(), a)
        payload["customer_version"] = fresh.json()["version"]
        self.assertEqual(self.client.put(path, headers=headers, json=payload).status_code, 200)

    def test_customer_duplicate_identity_and_cross_company_links(self):
        headers = self.owner_session()
        a = self.application_fixture(headers)
        duplicate = {**a["data"], "document_id": "000 0000000 1"}
        result = self.client.post('/api/v1/loan-applications', headers=headers, json={"modality": "unsecured", "data": duplicate, "create_customer": True})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(len(self.client.get('/api/v1/loan-applications', headers=headers).json()), 1)
        profile = self.client.get('/api/v1/customers?q=00000000001', headers=headers).json()[0]
        self.assertEqual(profile["id"], a["customer_id"])
        self.assertEqual(self.client.post('/api/v1/customers', headers=headers, json={**profile, "document_id": "00000000001"}).status_code, 409)
        self.register_owner(email="second@example.com")
        other = self.auth_headers(self.login(email="second@example.com")["access_token"])
        result = self.client.post('/api/v1/loan-applications', headers=other, json={"modality": "unsecured", "data": a["data"], "customer_id": a["customer_id"], "customer_version": profile["version"]})
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.client.get(f'/api/v1/loan-applications?customer_id={a["customer_id"]}', headers=other).status_code, 404)
        self.assertEqual(self.client.post('/api/v1/loan-applications', headers=other, json={"modality": "unsecured", "data": duplicate, "create_customer": True}).status_code, 201)

    def test_application_customer_limit_and_invalid_create_leave_no_records(self):
        from app.core.database import SessionLocal
        from app.models.company import Company
        from app.models.plan import Plan
        headers = self.owner_session()
        a = self.application_fixture(headers)
        with SessionLocal() as db:
            company = db.query(Company).first()
            plan = Plan(name="Un cliente", customer_limit=1, loan_limit=0, user_limit=0, monthly_price_usd=0)
            db.add(plan); db.flush(); company.plan_id = plan.id; db.commit()
        response = self.client.post('/api/v1/loan-applications', headers=headers, json={"modality": "unsecured", "data": {**a["data"], "document_id": "00000000002"}, "create_customer": True})
        self.assertEqual(response.status_code, 402, response.text)
        self.assertEqual(len(self.client.get('/api/v1/customers', headers=headers).json()), 1)
        self.assertEqual(len(self.client.get('/api/v1/loan-applications', headers=headers).json()), 1)
        self.assertEqual(self.client.post('/api/v1/loan-applications', headers=headers, json={"modality":"unsecured", "create_customer": True, "data":{"full_name":"Incompleto"}}).status_code, 422)
        # Reusing the existing customer does not consume a new customer slot.
        self.application_fixture(headers, "secured")

    def test_legacy_unlinked_draft_requires_link_but_inflight_can_disburse(self):
        from app.core.database import SessionLocal
        from app.models.loan_application import LoanApplication
        from datetime import date, timedelta
        headers = self.owner_session()
        a = self.application_fixture(headers)
        with SessionLocal() as db:
            item = db.get(LoanApplication, a["id"])
            item.customer_id = None; item.customer_version = None; db.commit()
        legacy = self.client.get(f'/api/v1/loan-applications/{a["id"]}', headers=headers).json()
        self.assertEqual(self.app_action(headers, legacy, "submit").status_code, 422)
        linked = self.client.put(f'/api/v1/loan-applications/{a["id"]}', headers=headers, json={"modality":a["modality"], "data":a["data"], "version":legacy["version"], "customer_id":a["customer_id"], "customer_version":a["customer_version"]})
        self.assertEqual(linked.status_code, 200, linked.text)
        with SessionLocal() as db:
            item = db.get(LoanApplication, a["id"])
            item.customer_id = None; item.customer_version = None; item.status = "signed"
            item.terms = {"interest_rate":"10", "installment_count":6, "late_fee_rate":"0", "grace_days":0}
            db.commit()
        legacy = self.client.get(f'/api/v1/loan-applications/{a["id"]}', headers=headers).json()
        result = self.app_action(headers, legacy, "disburse", first_payment_date=str(date.today()+timedelta(days=30)), disbursement_reference="LEGACY-TEST")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["customer_id"], a["customer_id"])
        self.assertEqual(len(self.client.get('/api/v1/customers', headers=headers).json()), 1)

    def test_customer_legacy_references_and_optional_contact_fields(self):
        import json
        headers = self.owner_session()
        refs = [{"nombre":"Ana", "telefono":"8095555555", "cedula":"REF", "direccion":"Calle referencia"}]
        values = {"full_name":"Cliente histórico", "phone":"8095552222", "address":"Calle Histórica", "notes":json.dumps(refs), "email":"cliente@example.com", "home_phone":"8095553333"}
        result = self.client.post('/api/v1/customers', headers=headers, json=values)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["references"], refs)
        self.assertEqual(result.json()["notes"], values["notes"])
        self.assertEqual(result.json()["email"], values["email"])
        self.assertEqual(result.json()["home_phone"], values["home_phone"])

    def test_legacy_signed_customer_link_preserves_snapshot(self):
        from app.core.database import SessionLocal
        from app.models.loan_application import LoanApplication
        headers = self.owner_session()
        a = self.application_fixture(headers)
        with SessionLocal() as db:
            item = db.get(LoanApplication, a["id"])
            item.customer_id = None; item.customer_version = None; item.status = "signed"
            db.commit()
        path = f'/api/v1/loan-applications/{a["id"]}'
        legacy = self.client.get(path, headers=headers).json()
        wrong = self.create_customer(headers)
        response = self.client.post(path + '/customer', headers=headers, json={"version":legacy["version"], "customer_id":wrong["id"], "customer_version":wrong["version"]})
        self.assertEqual(response.status_code, 422)
        response = self.client.post(path + '/customer', headers=headers, json={"version":legacy["version"], "customer_id":a["customer_id"], "customer_version":a["customer_version"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], a["data"])
        self.assertEqual(response.json()["status"], "signed")

    def test_customer_profile_migration_preserves_legacy_records(self):
        import importlib.util
        import json
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from unittest.mock import patch
        path = Path(__file__).resolve().parents[1] / "alembic/versions/d9e0f1a2b3c4_customer_profiles.py"
        spec = importlib.util.spec_from_file_location("customer_profile_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        legacy_engine = sa.create_engine("sqlite://")
        notes = json.dumps([{"nombre":"Referencia histórica", "telefono":"8095554444"}])
        with legacy_engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE customers (id INTEGER PRIMARY KEY, company_id INTEGER, document_id TEXT, notes TEXT)"))
            connection.execute(sa.text("CREATE TABLE loan_applications (id INTEGER PRIMARY KEY, customer_id INTEGER, data TEXT)"))
            connection.execute(sa.text("INSERT INTO customers VALUES (1, 1, '001-1', :notes), (2, 1, '0011', 'Notas libres'), (3, 2, '0011', NULL)"), {"notes":notes})
            connection.execute(sa.text("INSERT INTO loan_applications VALUES (1, 1, 'snapshot original')"))
            with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
                migration.upgrade()
            rows = connection.execute(sa.text('SELECT id, document_key, notes, "references", version FROM customers ORDER BY id')).mappings().all()
            self.assertEqual(len(rows), 3)
            self.assertEqual([r["document_key"] for r in rows], [None, None, "0011"])
            self.assertEqual(rows[0]["notes"], notes)
            self.assertEqual(json.loads(rows[0]["references"])[0]["nombre"], "Referencia histórica")
            self.assertEqual(rows[1]["notes"], "Notas libres")
            app_row = connection.execute(sa.text("SELECT data, customer_version FROM loan_applications")).one()
            self.assertEqual(tuple(app_row), ("snapshot original", 1))
        legacy_engine.dispose()

    def test_selected_legacy_duplicate_remains_usable_without_merging(self):
        from app.core.database import SessionLocal
        from app.models.customer import Customer
        headers = self.owner_session()
        a = self.application_fixture(headers)
        with SessionLocal() as db:
            original = db.get(Customer, a["customer_id"])
            original.document_key = None
            duplicate = Customer(company_id=original.company_id, created_by_id=original.created_by_id,
                full_name="Otro registro histórico", document_id=original.document_id,
                phone="8095550000", address="Otra dirección", document_key=None)
            db.add(duplicate); db.commit()
        profile = self.client.get(f'/api/v1/customers/{a["customer_id"]}', headers=headers).json()
        response = self.client.put(f'/api/v1/loan-applications/{a["id"]}', headers=headers,
            json={"modality":a["modality"], "data":{**a["data"], "phone":"8095551111"}, "version":a["version"], "customer_id":a["customer_id"], "customer_version":profile["version"]})
        self.assertEqual(response.status_code, 200, response.text)
        rows = self.client.get('/api/v1/customers?q=00000000001', headers=headers).json()
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(r for r in rows if r["id"] != a["customer_id"])["phone"], "8095550000")
        self.assertEqual(self.client.post('/api/v1/customers', headers=headers, json=profile).status_code, 409)


    def test_settings_incomplete_profile_and_validation(self):
        headers = self.owner_session()
        response = self.client.get('/api/v1/company-settings', headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        original = response.json()
        self.assertEqual(original['tax_id'], '')
        invalid = {key: original[key] for key in ('name', 'tax_id', 'address', 'phone', 'currency_symbol')}
        self.assertEqual(self.client.put('/api/v1/company-settings', headers=headers, json=invalid).status_code, 422)
        valid = dict(name='  Empresa QA  ', tax_id='QA-123', address='Dirección de prueba', phone='8095550100', currency_symbol='RD$')
        saved = self.client.put('/api/v1/company-settings', headers=headers, json=valid)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['name'], 'Empresa QA')
        self.assertEqual(self.client.put('/api/v1/company-settings', headers=headers, json={**valid, 'name': '   '}).status_code, 422)
        self.assertEqual(self.client.get('/api/v1/company-settings', headers=headers).json()['name'], 'Empresa QA')

    def test_settings_loan_print_and_branch_constraints(self):
        headers = self.owner_session()
        loan = self.client.get('/api/v1/loan-settings', headers=headers).json()
        self.assertEqual(self.client.put('/api/v1/loan-settings', headers=headers, json=loan).status_code, 200)
        self.assertEqual(self.client.put('/api/v1/loan-settings', headers=headers, json={**loan, 'maximum_principal': 0}).status_code, 422)
        printed = self.client.get('/api/v1/print-settings', headers=headers).json()
        self.assertEqual(self.client.put('/api/v1/print-settings', headers=headers, json={**printed, 'receipt_footer_text': '   '}).status_code, 422)
        branch = dict(name='Sucursal QA', address='Calle de prueba', manager_name='Gerente QA', notary_name='Notario QA', phone='8095550100')
        self.assertEqual(self.client.post('/api/v1/branches', headers=headers, json={**branch, 'manager_name': '   '}).status_code, 422)
        self.assertEqual(self.client.post('/api/v1/branches', headers=headers, json=branch).status_code, 201)
        self.assertEqual(self.client.post('/api/v1/branches', headers=headers, json=branch).status_code, 409)

    def test_cash_activation_allows_unassigned_staff_but_blocks_operations(self):
        headers = self.owner_session()
        users = [self.create_user(headers, role+'@example.com', 'collector') for role in ('collector', 'cashier')]
        # Reproduce existing unassigned staff from before cashier onboarding rules.
        from app.core.database import SessionLocal
        from app.models.user import User
        with SessionLocal() as db:
            db.get(User, users[1]['id']).role = 'cashier'
            db.commit()
        branch = self.client.post('/api/v1/branches', headers=headers, json=dict(name='Caja Central', address='Calle prueba 1', manager_name='Gerente QA', notary_name='Notario QA', phone='8095550100')).json()
        self.assertEqual(self.client.post('/api/v1/cash/activate', headers=headers, json={}).status_code, 422)
        self.assertEqual(self.client.post('/api/v1/cash/setup', headers=headers, json=dict(branch_id=branch['id'], initial_balance='0', notes='Sin efectivo inicial')).status_code, 200)
        pending = self.client.get('/api/v1/cash/config', headers=headers).json()['pending_users']
        self.assertEqual({u['id'] for u in pending}, {u['id'] for u in users})
        self.assertEqual(self.client.post('/api/v1/cash/activate', headers=headers, json={}).status_code, 200)
        for role in ('collector', 'cashier'):
            staff = self.auth_headers(self.login(role+'@example.com', 'workerpass123')['access_token'])
            self.assertEqual(self.client.get('/api/v1/cash/config', headers=staff).json()['pending_users'], [])
            result = self.client.post('/api/v1/cash/commands', headers=staff, json=dict(action='declare' if role=='collector' else 'open', branch_id=branch['id'], amount='0', idempotency_key='unassigned-'+role))
            self.assertEqual(result.status_code, 403, result.text)

if __name__ == "__main__":
    unittest.main()
