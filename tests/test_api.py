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

    def register_user(self, email: str = "owner@example.com", password: str = "superpass123") -> dict:
        response = self.client.post(
            "/api/v1/auth/register",
            json={
                "full_name": "Owner User",
                "email": email,
                "password": password,
                "role": "manager",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def login_user(self, email: str = "owner@example.com", password: str = "superpass123") -> dict:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password, "device_name": "test-suite"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def auth_headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    def test_auth_session_lifecycle(self) -> None:
        self.register_user()
        login_data = self.login_user()

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

    def test_password_reset_debug_code_hidden_outside_development(self) -> None:
        self.register_user(email="reset@example.com")

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

    def test_customer_loan_and_composed_payment_flow(self) -> None:
        self.register_user()
        login_data = self.login_user()
        headers = self.auth_headers(login_data["access_token"])

        customer_response = self.client.post(
            "/api/v1/customers",
            json={
                "full_name": "Juan Perez",
                "document_id": "001-0000000-1",
                "phone": "8095550001",
                "address": "Calle Primera #10",
                "notes": "Cliente de prueba",
            },
            headers=headers,
        )
        self.assertEqual(customer_response.status_code, 201, customer_response.text)
        customer = customer_response.json()

        loan_response = self.client.post(
            "/api/v1/loans",
            json={
                "customer_id": customer["id"],
                "principal_amount": "10000.00",
                "interest_rate": "12.00",
                "late_fee_rate": "3.00",
                "grace_days": 0,
                "installment_count": 4,
                "payment_frequency": "weekly",
                "start_date": "2026-03-18",
                "route_name": "Ruta Centro",
                "requires_promissory_note": False,
                "auto_approve": True,
            },
            headers=headers,
        )
        self.assertEqual(loan_response.status_code, 201, loan_response.text)
        loan = loan_response.json()
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

        loan_detail = self.client.get(f"/api/v1/loans/{loan['id']}", headers=headers)
        self.assertEqual(loan_detail.status_code, 200, loan_detail.text)
        refreshed_loan = loan_detail.json()
        self.assertEqual(refreshed_loan["principal_balance"], "7300.00")
        self.assertEqual(refreshed_loan["interest_balance"], "850.00")
        self.assertEqual(refreshed_loan["late_fee_balance"], "0.00")


if __name__ == "__main__":
    unittest.main()
