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
            "document_id": "001-0000000-1",
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

        # Free plan allows 1 user (the admin already exists), so adding one is blocked.
        blocked_user = self.client.post(
            "/api/v1/users",
            json={"full_name": "Empleado", "email": "emp@example.com",
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


if __name__ == "__main__":
    unittest.main()
