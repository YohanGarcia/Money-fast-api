"""loan settings: remember whether the administrator saved the rules

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-15 12:00:00.000000

Additive over 0028. Until now opening Configuración → Préstamos created a row with the product defaults, so a row
alone does not prove anyone chose those values. ``configured`` is true only once the administrator saved them.
Existing rows count as configured only when they differ from the product defaults.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0029'
down_revision: Union[str, Sequence[str], None] = '0028'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('loan_settings', sa.Column('configured', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute(sa.text(
        "UPDATE loan_settings SET configured = true WHERE NOT ("
        "minimum_principal = 10000 AND maximum_principal = 50000 AND default_interest_rate = 10 AND minimum_term_count = 2 "
        "AND minimum_term_unit = 'Semanas' AND default_late_fee_rate = 3 AND default_grace_days = 5 "
        "AND require_route_assignment = false AND allow_payment_date_change = false)"
    ))


def downgrade() -> None:
    op.drop_column('loan_settings', 'configured')
