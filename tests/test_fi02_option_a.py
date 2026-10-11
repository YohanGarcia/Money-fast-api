"""F-I02 option A (EXPERIMENTAL): the v2 close is refused while a surplus is in review; resolving it unblocks."""
import unittest
import uuid
from decimal import Decimal

from tests import test_api as legacy
from tests import test_cash as cash_tests
from tests.cash_fixtures import denominations_for


class OptionATests(unittest.TestCase):
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

    def close(self, counted):
        s = self.workspace()["session"]
        return self.client.post(
            f"/api/v2/cash/sessions/{s['id']}/close",
            json=dict(idempotency_key=str(uuid.uuid4()), denominations=denominations_for(str(counted)), receiver_user_id=self.admin_id),
            headers=self.roles["cashier"],
        )

    def test_close_is_blocked_until_the_surplus_is_resolved(self):
        self.open("1000")
        w = self.workspace()
        delivery = self.cmd("report_surplus", headers=self.roles["cashier"], target_id=self.users["collector"]["id"], amount="40.00", notes="s", version=w["session"]["version"])["delivery_id"]
        balance = Decimal(self.workspace()["session"]["balance"])
        blocked = self.close(balance)
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["error"]["code"], "cash_pending_surplus_review")
        self.cmd("confirm_surplus", headers=self.admin, target_id=delivery, version=1, notes="ok")
        balance = Decimal(self.workspace()["session"]["balance"])
        self.assertEqual(balance, Decimal("1040.00"))
        self.assertEqual(self.close(balance).status_code, 200)


if __name__ == "__main__":
    unittest.main()
