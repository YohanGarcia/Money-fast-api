"""Create the FIRST administrator (and its company) of a fresh installation.

Self-service registration of new companies is also available (/auth/register); this CLI is the way to bootstrap an\ninstallation without exposing a form (e.g. when PUBLIC_REGISTRATION=false):

    FM_ADMIN_PASSWORD='...' python scripts/create_first_admin.py --email admin@empresa.com \
        --name "Nombre Apellido" --company "Mi Empresa"

* The password is read from ``FM_ADMIN_PASSWORD`` or asked interactively (hidden); there is no default and it is
  never printed or accepted as a command-line argument (it would land in shell history / process lists).
* It refuses to run when a company already exists, unless ``--additional-company`` is given on purpose.
* It runs against ``DATABASE_URL`` after ``alembic upgrade head``; nothing else is created (no demo data).
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.security import password_policy_violation  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.modules.identity.onboarding import create_tenant_owner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True, help="Full name of the administrator")
    parser.add_argument("--company", required=True)
    parser.add_argument("--tenant-slug", default=None)
    parser.add_argument("--additional-company", action="store_true", help="allow creating a company when others exist")
    args = parser.parse_args()

    password = os.environ.get("FM_ADMIN_PASSWORD") or getpass.getpass("Contrasena del administrador: ")
    violation = password_policy_violation(password)
    if violation:
        print(f"Contrasena rechazada: {violation}", file=sys.stderr)
        return 2
    with SessionLocal() as db:
        existing = db.scalar(select(func.count()).select_from(Company))
        if existing and not args.additional_company:
            print(f"Ya existen {existing} empresa(s): no se crea otro administrador inicial.", file=sys.stderr)
            return 3
        user = create_tenant_owner(
            db,
            full_name=args.name,
            email=args.email,
            password=password,
            company_name=args.company,
            tenant_slug=args.tenant_slug,
            via="first_admin_cli",
        )
        print(f"Administrador creado: id={user.id} email={user.email} empresa_id={user.company_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())