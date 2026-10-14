"""Seed the demo organization (2 tenants, 2 branches each, cash points, DOP/USD). Test data only.

Usage: uv run python scripts/seed_organization.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.core.db import session_scope  # noqa: E402
from app.modules.organization.seed import seed_test_organization  # noqa: E402

if __name__ == "__main__":
    with session_scope() as db:
        print(seed_test_organization(db))
