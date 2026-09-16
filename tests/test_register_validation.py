import unittest

from pydantic import ValidationError
from app.schemas.auth import RegisterInput


class RegisterValidationTests(unittest.TestCase):
    def test_company_name_is_required(self):
        base = dict(full_name="Test Owner", email="test@example.com", password="test-password")
        for extra in ({}, {"company_name": None}, {"company_name": ""}, {"company_name": "   "}, {"company_name": "x" * 161}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                RegisterInput(**base, **extra)

    def test_company_name_is_trimmed(self):
        payload = RegisterInput(full_name="Test Owner", email="test@example.com", password="test-password", company_name="  Mi Empresa  ")
        self.assertEqual(payload.company_name, "Mi Empresa")
