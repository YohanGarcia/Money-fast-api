"""F17: the custody receipt of a closing hand-over is readable only by supervision and by the parties of that hand-over.

Integration tests against the isolated PostgreSQL test database (same fixtures as ``tests.test_cash``) plus a pure test
of the visibility rule. Denied callers must receive exactly the answer of a missing receipt and nothing may be written.
"""

import unittest
import uuid
from types import SimpleNamespace

from app.api.routes.cash import custody_receipt_visible
from app.core.database import SessionLocal
from app.models.capital import CapitalMovement
from app.models.cash import CashAudit, CashCustodyTransfer, CashMovement
from tests import test_api as legacy
from tests import test_cash as cash_tests  # module import: pytest must not collect CashTests twice
from tests.cash_fixtures import grant_cash_permissions


class CustodyReceiptVisibilityRule(unittest.TestCase):
    def user(self, role, uid):
        return SimpleNamespace(role=role, id=uid)

    def test_supervision_is_always_allowed_inside_its_scope(self):
        transfer = SimpleNamespace(from_user_id=10, to_user_id=11)
        for role in ("admin", "manager"):
            self.assertTrue(custody_receipt_visible(self.user(role, 99), transfer), role)

    def test_only_the_parties_are_allowed_whatever_their_role(self):
        transfer = SimpleNamespace(from_user_id=10, to_user_id=11)
        for role in ("cashier", "collector"):
            self.assertTrue(custody_receipt_visible(self.user(role, 10), transfer), f"{role} giver")
            self.assertTrue(custody_receipt_visible(self.user(role, 11), transfer), f"{role} receiver")
            self.assertFalse(custody_receipt_visible(self.user(role, 12), transfer), f"{role} stranger")

    def test_legacy_transfers_without_a_named_receiver_only_expose_the_giver(self):
        transfer = SimpleNamespace(from_user_id=10, to_user_id=None)
        self.assertTrue(custody_receipt_visible(self.user("cashier", 10), transfer))
        self.assertFalse(custody_receipt_visible(self.user("collector", 12), transfer))
        self.assertFalse(custody_receipt_visible(self.user("cashier", 12), transfer))


class CustodyReceiptAccessTests(unittest.TestCase):
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
    workspace = cash_tests.CashTests.workspace
    cmd = cash_tests.CashTests.cmd
    open = cash_tests.CashTests.open
    _finish_pending_close = cash_tests.CashTests._finish_pending_close

    # --- helpers ---------------------------------------------------------------------------------------------------
    def new_user(self, role, name, branch):
        email = f"{name}@example.com"
        self.req(
            "/users", dict(full_name=name, email=email, password="workerpass123", role=role, branch_id=branch), code=201
        )
        return self.auth_headers(self.login(email, "workerpass123")["access_token"])

    def get(self, path, headers=None):
        return self.client.get("/api/v1" + path, headers=headers or {})

    @staticmethod
    def shape(response):
        # Only the per-request correlation id may differ between a denial and a missing receipt.
        body = response.json()
        error = {k: v for k, v in (body.get("error") or {}).items() if k != "correlation_id"}
        return {"status": response.status_code, "error": error, "detail": body.get("detail")}

    def ledger_counts(self):
        with SessionLocal() as db:
            return tuple(
                db.query(model).count() for model in (CashMovement, CashCustodyTransfer, CashAudit, CapitalMovement)
            )

    def close_without_confirming(self):
        self.open()
        w = self.workspace()
        self.cmd("close", version=w["session"]["version"], denominations={"1000": 1})  # receiver: the administrator

    def pending_transfer_id(self):
        with SessionLocal() as db:
            row = (
                db.query(CashCustodyTransfer)
                .filter(CashCustodyTransfer.kind == "closing_capital")
                .order_by(CashCustodyTransfer.id.desc())
                .first()
            )
            return row.id, row.state

    # --- the contract ----------------------------------------------------------------------------------------------
    def test_receipt_is_limited_to_parties_and_supervision_and_never_mutates(self):
        self.close_without_confirming()
        transfer_id, state = self.pending_transfer_id()
        self.assertEqual(state, "pending")
        path = f"/cash/custody-receipt/{transfer_id}?branch_id={self.branch}"

        # A pending hand-over has no receipt for anybody (parties and supervision included).
        for headers in (self.roles["cashier"], self.admin, self.roles["manager"]):
            self.assertEqual(self.get(path, headers).status_code, 404)

        self._finish_pending_close()
        transfer_id, state = self.pending_transfer_id()
        self.assertEqual(state, "confirmed")
        path = f"/cash/custody-receipt/{transfer_id}?branch_id={self.branch}"
        before = self.ledger_counts()

        # Allowed: the giver, the named receiver (the administrator) and the branch manager.
        for who, headers in (
            ("giver", self.roles["cashier"]),
            ("receiver", self.admin),
            ("manager", self.roles["manager"]),
        ):
            response = self.get(path, headers)
            self.assertEqual(response.status_code, 200, who)
            html = response.json()["html"]
            self.assertIn(f"CUST-{transfer_id}", html, who)
            self.assertIn("1000", html, who)

        # Denied: a stranger cashier and a collector of the SAME branch get exactly the answer of a missing receipt.
        stranger_cashier = self.new_user("cashier", "cashier2", self.branch)
        missing = self.get(f"/cash/custody-receipt/99999999?branch_id={self.branch}", self.roles["collector"])
        self.assertEqual(missing.status_code, 404)
        for who, headers in (("collector", self.roles["collector"]), ("stranger cashier", stranger_cashier)):
            for variant in (path, f"/cash/custody-receipt/{transfer_id}"):
                response = self.get(variant, headers)
                self.assertEqual(response.status_code, 404, f"{who} {variant}")
                self.assertEqual(
                    self.shape(response), self.shape(missing), f"{who}: distinguishable from a missing receipt"
                )
                self.assertNotIn("CUST-", response.text)
                self.assertNotIn("1000", response.text)

        # Denied: other branch (explicit or implicit scope), other tenant, anonymous.
        foreign_collector = self.new_user("collector", "collector.north", self.other)
        foreign_cashier = self.new_user("cashier", "cashier.north", self.other)
        for who, headers in (("foreign collector", foreign_collector), ("foreign cashier", foreign_cashier)):
            for variant in (path, f"/cash/custody-receipt/{transfer_id}"):
                response = self.get(variant, headers)
                self.assertIn(response.status_code, (403, 404), f"{who} {variant}")
                self.assertNotIn("CUST-", response.text)
                self.assertNotIn("1000", response.text)
        other_owner = self.client.post(
            "/api/v1/auth/register",
            json=dict(
                full_name="Otro Dueno", email="owner2@example.com", password="superpass123", company_name="Otra Empresa"
            ),
        )
        self.assertEqual(other_owner.status_code, 201, other_owner.text)
        other_headers = self.auth_headers(self.login("owner2@example.com", "superpass123")["access_token"])
        response = self.get(path, other_headers)
        # 403/404 for a scoped denial, 409 when that tenant has not enabled Cash: none of them carries data.
        self.assertIn(response.status_code, (403, 404, 409))
        self.assertNotIn("CUST-", response.text)
        self.assertEqual(self.get(path).status_code, 401)

        # Reading never writes: no movement, transfer, audit or capital row changed.
        self.assertEqual(self.ledger_counts(), before)

    def test_receiver_that_is_a_collector_by_role_is_a_party_not_a_stranger(self):
        # Receiver validity is GRANT based (sessions._valid_receiver): any active tenant user other than the maker
        # who holds cash.handovers.accept at the CashPoint. A collector holding it is therefore a legitimate party
        # of the hand-over and keeps access; every other collector stays a stranger.
        collector = self.users["collector"]
        grant_cash_permissions(collector["company_id"], collector["id"], ["cash.handovers.accept"], self.branch)
        self.open()
        w = self.workspace()
        self.cmd("close", version=w["session"]["version"], denominations={"1000": 1}, receiver_id=collector["id"])
        transfer_id, state = self.pending_transfer_id()
        self.assertEqual(state, "pending")
        accepted = self.client.post(
            f"/api/v2/cash/handovers/{transfer_id}/accept",
            headers=self.roles["collector"],
            json=dict(idempotency_key=str(uuid.uuid4())),
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        path = f"/cash/custody-receipt/{transfer_id}?branch_id={self.branch}"
        self.assertEqual(self.get(path, self.roles["collector"]).status_code, 200)
        self.assertEqual(self.get(path, self.roles["cashier"]).status_code, 200)  # the giver
        stranger = self.new_user("collector", "collector.other", self.branch)
        self.assertEqual(self.get(path, stranger).status_code, 404)


if __name__ == "__main__":
    unittest.main()
