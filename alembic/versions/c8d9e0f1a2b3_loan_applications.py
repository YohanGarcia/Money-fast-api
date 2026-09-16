"""Add loan application dossiers and private documents.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
"""
from alembic import op
import sqlalchemy as sa

revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade():
    # Development startup may already have created new tables via create_all.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("loan_applications"):
        op.create_table("loan_applications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False, index=True),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id")),
            sa.Column("loan_id", sa.Integer(), sa.ForeignKey("loans.id"), unique=True),
            sa.Column("modality", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("terms", sa.JSON(), nullable=False),
            sa.Column("history", sa.JSON(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    if not inspector.has_table("application_documents"):
        op.create_table("application_documents",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("application_id", sa.Integer(), sa.ForeignKey("loan_applications.id"), nullable=False, index=True),
            sa.Column("category", sa.String(40), nullable=False),
            sa.Column("filename", sa.String(160), nullable=False),
            sa.Column("media_type", sa.String(40), nullable=False),
            sa.Column("content", sa.LargeBinary(), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("verified", sa.Boolean(), nullable=False))


def downgrade():
    op.drop_table("application_documents")
    op.drop_table("loan_applications")
