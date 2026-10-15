"""plan features: which optional capabilities each plan includes

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-15 09:00:00.000000

Additive over 0027. ``plans.features`` is a JSON list of feature keys (see ``app.services.plan_limits.FEATURES``).
Existing paid plans keep working as advertised (they get every feature); free plans get none.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0028'
down_revision: Union[str, Sequence[str], None] = '0027'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ALL = '["branches", "routes", "promissory_note", "print_settings", "priority_support"]'


def upgrade() -> None:
    op.add_column('plans', sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                                     server_default=sa.text("'[]'::jsonb")))
    op.execute(sa.text(f"UPDATE plans SET features = '{ALL}'::jsonb WHERE monthly_price_usd > 0"))


def downgrade() -> None:
    op.drop_column('plans', 'features')
