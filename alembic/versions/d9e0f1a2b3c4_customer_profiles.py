"""Link application snapshots to versioned customer profiles."""
from alembic import op
import sqlalchemy as sa
import json

revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("customers")}
    columns = [sa.Column(name, sa.String(size), nullable=True) for name, size in (
        ("document_key", 30), ("email", 255), ("home_phone", 30), ("birth_date", 10),
        ("marital_status", 30), ("nationality", 160), ("city", 160))]
    columns += [sa.Column("references", sa.JSON(), nullable=False, server_default="[]"),
                sa.Column("version", sa.Integer(), nullable=False, server_default="1")]
    with op.batch_alter_table("customers") as batch:
        for column in columns:
            if column.name not in existing:
                batch.add_column(column)
        if not any(c["name"] == "uq_customer_company_document" for c in sa.inspect(bind).get_unique_constraints("customers")):
            batch.create_unique_constraint("uq_customer_company_document", ["company_id", "document_key"])
    if "customer_version" not in {c["name"] for c in sa.inspect(bind).get_columns("loan_applications")}:
        op.add_column("loan_applications", sa.Column("customer_version", sa.Integer(), nullable=True))
    customers = sa.table("customers", sa.column("id"), sa.column("company_id"), sa.column("document_id"),
                         sa.column("notes"), sa.column("references", sa.JSON()), sa.column("document_key"))
    rows = bind.execute(sa.select(customers)).mappings().all()
    keys = [(r["company_id"], "".join(c for c in (r["document_id"] or "").upper() if c.isalnum())) for r in rows]
    for row, key in zip(rows, keys):
        values = {"document_key": key[1] if key[1] and keys.count(key) == 1 else None}
        if not row["references"]:
            try:
                refs = json.loads(row["notes"] or "[]")
                if isinstance(refs, list):
                    values["references"] = [{k: str(r.get(k) or "") for k in ("nombre", "telefono", "cedula", "direccion")}
                                            for r in refs[:3] if isinstance(r, dict) and r.get("nombre")]
            except (ValueError, TypeError):
                pass
        bind.execute(customers.update().where(customers.c.id == row["id"]).values(**values))
    bind.execute(sa.text("UPDATE loan_applications SET customer_version = (SELECT version FROM customers WHERE customers.id = loan_applications.customer_id) WHERE customer_id IS NOT NULL"))


def downgrade():
    op.drop_column("loan_applications", "customer_version")
    with op.batch_alter_table("customers") as batch:
        batch.drop_constraint("uq_customer_company_document", type_="unique")
        for name in ("document_key", "email", "home_phone", "birth_date", "marital_status", "nationality", "city", "references", "version"):
            batch.drop_column(name)
