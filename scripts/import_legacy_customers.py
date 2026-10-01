"""Import legacy flat customers into the T-004 customer model (idempotent). Optional: the default policy for
current test data is RESET_AND_RESEED.

Usage: uv run python scripts/import_legacy_customers.py [tenant_id]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.core.db import session_scope  # noqa: E402
from app.modules.customers.legacy_import import import_legacy_customers  # noqa: E402

if __name__ == "__main__":
    tenant = int(sys.argv[1]) if len(sys.argv) > 1 else None
    with session_scope() as db:
        print(import_legacy_customers(db, tenant))
