"""Driver loading checks without connecting to an external database."""

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DatabaseConfigTests(unittest.TestCase):
    def run_database_check(self, url: str, code: str) -> None:
        env = {
            **os.environ,
            "DATABASE_URL": url,
            "ENVIRONMENT": "test",
            "SECRET_KEY": "database-config-test-secret-only",
        }
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_postgresql_provider_urls_load_installed_driver(self):
        for scheme in ("postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"):
            with self.subTest(scheme=scheme):
                self.run_database_check(
                    f"{scheme}://user:p%40ss%25word@localhost:5432/moneyfast?sslmode=require",
                    """
from unittest.mock import patch
from alembic.config import Config
import sqlalchemy
with patch('sqlalchemy.create_engine', wraps=sqlalchemy.create_engine) as create:
    from app.core.database import engine
    from app.core.config import settings
assert create.call_args.kwargs['connect_args'] == {}
assert engine.dialect.driver == 'psycopg'
assert engine.dialect.dbapi.__name__ == 'psycopg'
assert engine.url.password == 'p@ss%word'
assert engine.url.query['sslmode'] == 'require'
config = Config()
config.set_main_option('sqlalchemy.url', settings.database_url.replace('%', '%%'))
assert config.get_main_option('sqlalchemy.url') == settings.database_url
engine.dispose()
""",
                )

    def test_sqlite_still_connects(self):
        self.run_database_check(
            "sqlite:///:memory:",
            """
from app.core.database import engine
from sqlalchemy import text
with engine.connect() as connection:
    assert connection.scalar(text('SELECT 1')) == 1
engine.dispose()
""",
        )


    def test_prepare_database_bootstraps_fresh_sqlite_and_is_idempotent(self):
        """Empty installations get the current schema and an Alembic version."""
        import tempfile
        import sqlalchemy as sa
        import app.models  # noqa: F401
        from app.core.database import Base

        with tempfile.TemporaryDirectory() as folder:
            url = "sqlite:///" + (Path(folder) / "new_install.db").as_posix()
            env = {**os.environ, "DATABASE_URL": url, "ENVIRONMENT": "test"}
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    result = subprocess.run(
                        [sys.executable, "scripts/prepare_database.py"],
                        cwd=ROOT, env=env, capture_output=True,
                        text=True, timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
            fresh_engine = sa.create_engine(url)
            try:
                actual = set(sa.inspect(fresh_engine).get_table_names())
                self.assertTrue(set(Base.metadata.tables).issubset(actual))
                self.assertIn("alembic_version", actual)
                with fresh_engine.connect() as conn:
                    rows = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
                self.assertEqual(rows, ["f1a2b3c4d5e6"])
                custody_cols = {
                    col["name"] for col in sa.inspect(fresh_engine).get_columns("cash_sessions")
                }
                self.assertIn("cashier_id", custody_cols)
                self.assertIn("cash_custody_transfers", actual)
            finally:
                fresh_engine.dispose()

    def test_prepare_database_refuses_partial_unversioned_schema(self):
        """Never silently stamp an unrelated or half-created database."""
        import tempfile
        import sqlalchemy as sa

        with tempfile.TemporaryDirectory() as folder:
            url = "sqlite:///" + (Path(folder) / "partial.db").as_posix()
            partial_engine = sa.create_engine(url)
            try:
                with partial_engine.begin() as conn:
                    conn.execute(sa.text("CREATE TABLE plans (id INTEGER PRIMARY KEY)"))
            finally:
                partial_engine.dispose()
            result = subprocess.run(
                [sys.executable, "scripts/prepare_database.py"],
                cwd=ROOT,
                env={**os.environ, "DATABASE_URL": url, "ENVIRONMENT": "test"},
                capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing an unsafe Alembic stamp", result.stderr)


if __name__ == "__main__":
    unittest.main()
