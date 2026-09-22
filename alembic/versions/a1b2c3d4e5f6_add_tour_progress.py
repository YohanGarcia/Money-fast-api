"""add tour_progress table

Revision ID: a1b2c3d4e5f6
Revises: 6f7a8b9c0d1e
Create Date: 2026-09-22

Tracks per-user onboarding tour progress (started/completed/skipped) so the
guided-tour system knows what to show without depending only on localStorage.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "6f7a8b9c0d1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "tour_progress" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "tour_progress",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tour_id", sa.String(length=60), nullable=False),
        sa.Column("status", sa.Enum("in_progress", "completed", "skipped", name="tourstatus"), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("tour_version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "tour_id", name="uq_tour_progress_user_tour"),
    )
    op.create_index("ix_tour_progress_user_id", "tour_progress", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tour_progress_user_id", table_name="tour_progress")
    op.drop_table("tour_progress")
