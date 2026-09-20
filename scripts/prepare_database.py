"""Prepare a database created before Alembic was enabled, then migrate it.

Early MoneyFast deployments used SQLAlchemy ``create_all`` and therefore have
all their tables but no ``alembic_version`` row.  Stamping that database at
the last pre-cash migration avoids recreating ``companies`` while still
applying the additive route and customer-location migrations.
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.core.database import engine


LEGACY_BASELINE = "e0f1a2b3c4d5"


def main() -> None:
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        has_alembic_version = "alembic_version" in tables
        has_existing_schema = "companies" in tables

    config = Config("alembic.ini")
    if has_existing_schema and not has_alembic_version:
        print(f"Existing schema detected; stamping legacy baseline {LEGACY_BASELINE}.")
        command.stamp(config, LEGACY_BASELINE)

    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
