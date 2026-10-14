"""Test environment bootstrap (PostgreSQL only, ADR-003).

Import this module before any ``app`` module. It refuses to run against anything
that is not a PostgreSQL database whose name ends in ``_test``, because the legacy
suites call ``drop_all`` on the configured database.
"""

import os

from sqlalchemy.engine import make_url

url = os.environ.get("TEST_DATABASE_URL")
if not url:
    raise RuntimeError(
        "Define TEST_DATABASE_URL con una base PostgreSQL dedicada a pruebas, "
        "p. ej. postgresql+psycopg://moneyfast:moneyfast@127.0.0.1:5432/moneyfast_test"
    )
parsed = make_url(url)
if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").endswith("_test"):
    raise RuntimeError("TEST_DATABASE_URL debe ser PostgreSQL y el nombre de la base debe terminar en '_test'.")

os.environ["DATABASE_URL"] = url
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ENVIRONMENT"] = "development"
# Keep tests hermetic: never hit real SMTP even if a .env configures it.
# Cheap Argon2 parameters keep the suites fast; production uses the configured defaults.
os.environ["PASSWORD_HASH_TIME_COST"] = "1"
os.environ["PASSWORD_HASH_MEMORY_KIB"] = "8192"
os.environ["PASSWORD_HASH_PARALLELISM"] = "1"
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_USER"] = ""
os.environ["SMTP_PASSWORD"] = ""
TEST_DATABASE_URL = url

# The suites recreate tables and enum types between tests (``drop_all``/``create_all``). With psycopg's server-side
# prepared statements a pooled connection could keep a cached plan of the OLD type and fail with "cached plan must not
# change result type" in whichever test ran next (a pre-existing, order dependent flake). Tests never need them.
from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402


@event.listens_for(Engine, "connect")
def _no_server_side_prepared_statements(dbapi_connection, _record):  # pragma: no cover - harness only
    try:
        dbapi_connection.prepare_threshold = None
    except Exception:
        pass
