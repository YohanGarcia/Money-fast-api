"""
Seed script: crea empresa default, superadmin y asigna datos existentes.
Uso: uv run python scripts/seed_multitenant.py
"""
from datetime import UTC, datetime

from app.core.database import SessionLocal
from app.core.security import get_password_hash
from app.models import Company, User, UserRole
from app.models.customer import Customer
from app.models.branch import Branch
from app.models.company_settings import CompanySettings
from app.models.loan_settings import LoanSettings
from app.models.print_settings import PrintSettings


def seed() -> None:
    db = SessionLocal()
    try:
        # 1 — Empresa default
        company = db.query(Company).first()
        if not company:
            company = Company(
                name="Mi Empresa",
                tax_id="",
                address="",
                phone="",
                is_active=True,
                created_at=datetime.now(UTC),
            )
            db.add(company)
            db.flush()
            print(f"OK Empresa creada: {company.name} (ID {company.id})")
        else:
            print(f"INFO Empresa ya existe: {company.name} (ID {company.id})")

        # 2 — Superadmin
        superadmin_email = "yohangarcia056@gmail.com"
        superadmin = db.query(User).filter_by(email=superadmin_email).first()
        if not superadmin:
            superadmin = User(
                full_name="Yohan Garcia",
                email=superadmin_email,
                password_hash=get_password_hash("admin1234"),
                role=UserRole.superadmin,
                is_active=True,
                company_id=None,  # superadmin no pertenece a ninguna empresa
            )
            db.add(superadmin)
            db.flush()
            print(f"OK Superadmin creado: {superadmin.email}")
        else:
            superadmin.role = UserRole.superadmin
            superadmin.company_id = None
            print(f"OK Superadmin actualizado: {superadmin.email} -> role=superadmin, company_id=None")

        # 3 — Asignar company_id a usuarios existentes (excepto superadmin)
        users = db.query(User).filter(
            User.email != superadmin_email,
            User.company_id.is_(None)
        ).all()
        for u in users:
            u.company_id = company.id
        if users:
            print(f"OK {len(users)} usuario(s) asignados a empresa ID {company.id}")

        # 4 — Asignar company_id a clientes, sucursales, configuraciones
        for model, label in [
            (Customer, "clientes"),
            (Branch, "sucursales"),
        ]:
            rows = db.query(model).filter(model.company_id.is_(None)).all()
            for r in rows:
                r.company_id = company.id
            if rows:
                print(f"OK {len(rows)} {label} asignados a empresa ID {company.id}")

        # 5 — Configuraciones singleton → vincular a empresa
        cs = db.query(CompanySettings).first()
        if cs and cs.company_id is None:
            cs.company_id = company.id
            print("OK CompanySettings vinculado")
        elif not cs:
            db.add(CompanySettings(company_id=company.id))
            print("OK CompanySettings creado")

        ls = db.query(LoanSettings).first()
        if ls and ls.company_id is None:
            ls.company_id = company.id
            print("OK LoanSettings vinculado")
        elif not ls:
            db.add(LoanSettings(company_id=company.id))
            print("OK LoanSettings creado")

        ps = db.query(PrintSettings).first()
        if ps and ps.company_id is None:
            ps.company_id = company.id
            print("OK PrintSettings vinculado")
        elif not ps:
            db.add(PrintSettings(company_id=company.id))
            print("OK PrintSettings creado")

        db.commit()
        print("\nSeed completado.")
        print(f"   Superadmin : {superadmin_email} / admin1234")
        print(f"   Empresa ID : {company.id}")

    finally:
        db.close()


if __name__ == "__main__":
    seed()
