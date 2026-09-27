"""Prepare a new or verified legacy MoneyFast database and migrate it.

The original Alembic root migration assumes tables created before Alembic
existed. Do not run the historical migration chain on an empty database.

Fresh database: create the *current* model schema, then stamp current head.
Verified unversioned legacy database: retain the existing baseline migration.
Versioned database: run pending Alembic migrations normally.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.models  # noqa: F401; register all tables before create_all
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.core.database import Base, engine


LEGACY_BASELINE = "e0f1a2b3c4d5"
# The legacy baseline includes Caja, so a pre-Alembic database missing these
# tables is NOT safely equivalent to that revision.
LEGACY_REQUIRED_TABLES = {
    "companies", "plans", "branches", "users", "customers", "loans",
    "payments", "cash_boxes", "cash_sessions", "cash_movements",
}


def main() -> None:
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())

    config = Config(str(ROOT / "alembic.ini"))
    if not tables:
        print("Empty database detected; creating the current MoneyFast model schema.")
        Base.metadata.create_all(bind=engine)
        # The new schema was created from the models currently shipped with
        # this release. Stamping records that fact; it does not replay history.
        command.stamp(config, "head")
        print("Fresh database initialized and stamped at the current Alembic head.")
        return

    if "alembic_version" not in tables:
        missing = LEGACY_REQUIRED_TABLES - tables
        if missing:
            raise RuntimeError(
                "An unversioned, partially initialized or unsupported legacy "
                "database was detected. Refusing an unsafe Alembic stamp. "
                "Missing expected baseline tables: " + ", ".join(sorted(missing))
            )
        print(f"Existing legacy schema detected; stamping baseline {LEGACY_BASELINE}.")
        command.stamp(config, LEGACY_BASELINE)
    elif tables == {"alembic_version"}:
        raise RuntimeError(
            "Only alembic_version exists; database is incomplete. "
            "Inspect and repair it before proceeding."
        )

    command.upgrade(config, "head")
    print("Database migrated to the current Alembic head.")


if __name__ == "__main__":
    main()
