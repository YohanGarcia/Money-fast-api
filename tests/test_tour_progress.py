import unittest

from tests import test_api as legacy


class TourProgressTests(unittest.TestCase):
    setUpClass = classmethod(legacy.MoneyFastApiTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.MoneyFastApiTests.tearDownClass.__func__)
    setUp = legacy.MoneyFastApiTests.setUp
    register_owner = legacy.MoneyFastApiTests.register_owner
    _set_company_unlimited = legacy.MoneyFastApiTests._set_company_unlimited
    login = legacy.MoneyFastApiTests.login
    auth_headers = legacy.MoneyFastApiTests.auth_headers
    owner_session = legacy.MoneyFastApiTests.owner_session

    def test_list_empty_when_no_progress(self) -> None:
        headers = self.owner_session()
        response = self.client.get("/api/v1/tour-progress", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [])

    def test_upsert_creates_then_updates_same_row(self) -> None:
        headers = self.owner_session()
        response = self.client.put(
            "/api/v1/tour-progress/general-welcome",
            json={"status": "in_progress", "current_step": 2, "tour_version": 1},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["completed_at"])

        response = self.client.put(
            "/api/v1/tour-progress/general-welcome",
            json={"status": "completed", "current_step": 7, "tour_version": 1},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertIsNotNone(body["completed_at"])

        response = self.client.get("/api/v1/tour-progress", headers=headers)
        rows = response.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tour_id"], "general-welcome")

    def test_reset_deletes_row(self) -> None:
        headers = self.owner_session()
        self.client.put(
            "/api/v1/tour-progress/general-welcome",
            json={"status": "skipped", "current_step": 0, "tour_version": 1},
            headers=headers,
        )
        response = self.client.delete("/api/v1/tour-progress/general-welcome", headers=headers)
        self.assertEqual(response.status_code, 204, response.text)

        response = self.client.get("/api/v1/tour-progress", headers=headers)
        self.assertEqual(response.json(), [])

    def test_requires_auth(self) -> None:
        response = self.client.get("/api/v1/tour-progress")
        self.assertEqual(response.status_code, 401)
