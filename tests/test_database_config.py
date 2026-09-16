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


if __name__ == "__main__":
    unittest.main()
