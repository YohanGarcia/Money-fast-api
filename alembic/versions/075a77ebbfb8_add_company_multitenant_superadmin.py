"""add_company_multitenant_superadmin

Revision ID: 075a77ebbfb8
Revises:
Create Date: 2026-06-12 18:16:09.386697

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '075a77ebbfb8'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('companies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('tax_id', sa.String(length=40), nullable=False),
        sa.Column('address', sa.String(length=255), nullable=False),
        sa.Column('phone', sa.String(length=30), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=True),
        sa.Column('subscription_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], name='fk_companies_plan_id'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_companies_name'), ['name'], unique=False)

    with op.batch_alter_table('branches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('company_id', sa.Integer(), nullable=True))
        batch_op.drop_index(batch_op.f('ix_branches_name'))
        batch_op.create_index(batch_op.f('ix_branches_name'), ['name'], unique=False)
        batch_op.create_foreign_key('fk_branches_company_id', 'companies', ['company_id'], ['id'])

    with op.batch_alter_table('company_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('company_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_company_settings_company_id'), ['company_id'], unique=True)
        batch_op.create_foreign_key('fk_company_settings_company_id', 'companies', ['company_id'], ['id'])

    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('company_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_customers_company_id', 'companies', ['company_id'], ['id'])

    with op.batch_alter_table('loan_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('company_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_loan_settings_company_id'), ['company_id'], unique=True)
        batch_op.create_foreign_key('fk_loan_settings_company_id', 'companies', ['company_id'], ['id'])

    with op.batch_alter_table('print_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('company_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_print_settings_company_id'), ['company_id'], unique=True)
        batch_op.create_foreign_key('fk_print_settings_company_id', 'companies', ['company_id'], ['id'])

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('company_id', sa.Integer(), nullable=True))
        batch_op.alter_column('role',
               existing_type=sa.VARCHAR(length=9),
               type_=sa.Enum('superadmin', 'admin', 'manager', 'collector', name='userrole'),
               existing_nullable=False)
        batch_op.create_foreign_key('fk_users_company_id', 'companies', ['company_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('fk_users_company_id', type_='foreignkey')
        batch_op.alter_column('role',
               existing_type=sa.Enum('superadmin', 'admin', 'manager', 'collector', name='userrole'),
               type_=sa.VARCHAR(length=9),
               existing_nullable=False)
        batch_op.drop_column('company_id')

    with op.batch_alter_table('print_settings', schema=None) as batch_op:
        batch_op.drop_constraint('fk_print_settings_company_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_print_settings_company_id'))
        batch_op.drop_column('company_id')

    with op.batch_alter_table('loan_settings', schema=None) as batch_op:
        batch_op.drop_constraint('fk_loan_settings_company_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_loan_settings_company_id'))
        batch_op.drop_column('company_id')

    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_constraint('fk_customers_company_id', type_='foreignkey')
        batch_op.drop_column('company_id')

    with op.batch_alter_table('company_settings', schema=None) as batch_op:
        batch_op.drop_constraint('fk_company_settings_company_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_company_settings_company_id'))
        batch_op.drop_column('company_id')

    with op.batch_alter_table('branches', schema=None) as batch_op:
        batch_op.drop_constraint('fk_branches_company_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_branches_name'))
        batch_op.create_index(batch_op.f('ix_branches_name'), ['name'], unique=True)
        batch_op.drop_column('company_id')

    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_companies_name'))

    op.drop_table('companies')
