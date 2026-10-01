"""tenant branch cash point currency foundation

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-01 11:33:59.696070

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CURRENCIES = [("DOP", "Peso dominicano", "RD$", 2), ("USD", "Dolar estadounidense", "US$", 2), ("EUR", "Euro", "EUR", 2)]
# (code, scope_kind, description, is_sensitive)
NEW_PERMISSIONS = [
    ("tenant.settings.read", "tenant", "Consultar la configuracion de la agencia", False),
    ("tenant.settings.manage", "tenant", "Cambiar zona horaria y moneda base de la agencia", True),
    ("organization.branches.read", "tenant", "Consultar sucursales", False),
    ("organization.branches.manage", "tenant", "Crear y administrar sucursales", True),
    ("cash.points.read", "tenant", "Consultar cajas (puntos de caja)", False),
    ("cash.points.manage", "tenant", "Crear y administrar cajas", True),
    ("cash.points.suspend", "tenant", "Suspender y reanudar cajas de forma explicita", True),
    ("currencies.read", "tenant", "Consultar monedas de la agencia", False),
    ("currencies.manage", "tenant", "Habilitar y deshabilitar monedas", True),
]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('currencies',
    sa.Column('code', sa.String(length=3), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('symbol', sa.String(length=8), nullable=True),
    sa.Column('exponent', sa.SmallInteger(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.CheckConstraint("code ~ '^[A-Z]{3}$'", name=op.f('ck_currencies_code_format')),
    sa.CheckConstraint('exponent BETWEEN 0 AND 4', name=op.f('ck_currencies_exponent_range')),
    sa.PrimaryKeyConstraint('code', name=op.f('pk_currencies'))
    )
    currencies = sa.table("currencies", sa.column("code", sa.String), sa.column("name", sa.String),
                          sa.column("symbol", sa.String), sa.column("exponent", sa.SmallInteger),
                          sa.column("is_active", sa.Boolean))
    op.bulk_insert(currencies, [
        {"code": c, "name": n, "symbol": sy, "exponent": e, "is_active": True} for c, n, sy, e in CURRENCIES
    ])
    op.create_table('tenant_currencies',
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('enabled_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('enabled_by', sa.Integer(), nullable=True),
    sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('disabled_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['currency_code'], ['currencies.code'], name=op.f('fk_tenant_currencies_currency_code_currencies')),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_tenant_currencies_tenant_id_companies')),
    sa.PrimaryKeyConstraint('tenant_id', 'currency_code', name=op.f('pk_tenant_currencies'))
    )

    # --- companies (the Tenant): lifecycle status, base currency, default timezone ---------------------------
    op.add_column('companies', sa.Column('status', sa.String(length=20), nullable=False, server_default='active'))
    op.add_column('companies', sa.Column('base_currency_code', sa.String(length=3), nullable=False, server_default='DOP'))
    op.add_column('companies', sa.Column('default_timezone', sa.String(length=64), nullable=False,
                                         server_default='America/Santo_Domingo'))
    op.add_column('companies', sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                                         server_default=sa.func.now()))
    op.execute("UPDATE companies SET status = CASE WHEN is_active THEN 'active' ELSE 'inactive' END")
    op.execute("INSERT INTO tenant_currencies (tenant_id, currency_code, enabled_at) "
               "SELECT id, 'DOP', now() FROM companies")  # existing tenants: DOP enabled and base
    for column in ('status', 'base_currency_code', 'default_timezone', 'updated_at'):
        op.alter_column('companies', column, server_default=None)
    op.create_foreign_key('fk_companies_base_currency_enabled', 'companies', 'tenant_currencies',
                          ['id', 'base_currency_code'], ['tenant_id', 'currency_code'],
                          initially='DEFERRED', deferrable=True)
    op.create_check_constraint('status_valid', 'companies', "status IN ('active', 'inactive')")
    op.drop_column('companies', 'is_active')

    # --- branches ----------------------------------------------------------------------------------------------
    op.add_column('branches', sa.Column('code', sa.String(length=20), nullable=True))
    op.add_column('branches', sa.Column('status', sa.String(length=20), nullable=False, server_default='active'))
    op.add_column('branches', sa.Column('timezone_override', sa.String(length=64), nullable=True))
    op.add_column('branches', sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                                        server_default=sa.func.now()))
    op.execute("UPDATE branches SET code = 'SUC-' || id, status = CASE WHEN is_active THEN 'active' ELSE 'inactive' END")
    op.alter_column('branches', 'code', nullable=False)
    op.alter_column('branches', 'status', server_default=None)
    op.alter_column('branches', 'updated_at', server_default=None)
    op.create_unique_constraint('uq_branches_tenant_code', 'branches', ['company_id', 'code'])
    op.create_unique_constraint('uq_branches_tenant_id', 'branches', ['company_id', 'id'])
    op.create_check_constraint('status_valid', 'branches', "status IN ('active', 'inactive')")
    op.create_check_constraint('code_format', 'branches', "code ~ '^[A-Z0-9][A-Z0-9_-]{0,19}$'")
    op.drop_column('branches', 'is_active')

    # --- cash points (Branch 1:N CashPoints) -----------------------------------------------------------------
    op.create_table('cash_points',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('branch_id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=140), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('suspension_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(status = 'suspended') = (suspended_at IS NOT NULL)", name=op.f('ck_cash_points_suspension_consistent')),
    sa.CheckConstraint("status IN ('active', 'inactive', 'suspended')", name=op.f('ck_cash_points_status_valid')),
    sa.ForeignKeyConstraint(['tenant_id', 'branch_id'], ['branches.company_id', 'branches.id'], name='fk_cash_points_tenant_branch'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_cash_points_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cash_points')),
    sa.UniqueConstraint('tenant_id', 'code', name='uq_cash_points_tenant_code'),
    sa.UniqueConstraint('tenant_id', 'id', name='uq_cash_points_tenant_id')
    )
    op.create_index(op.f('ix_cash_points_branch_id'), 'cash_points', ['branch_id'], unique=False)
    op.create_index(op.f('ix_cash_points_tenant_id'), 'cash_points', ['tenant_id'], unique=False)
    op.create_table('cash_point_currencies',
    sa.Column('cash_point_id', sa.Integer(), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id', 'cash_point_id'], ['cash_points.tenant_id', 'cash_points.id'], name='fk_cash_point_currencies_cash_point'),
    sa.ForeignKeyConstraint(['tenant_id', 'currency_code'], ['tenant_currencies.tenant_id', 'tenant_currencies.currency_code'], name='fk_cash_point_currencies_tenant_currency'),
    sa.PrimaryKeyConstraint('cash_point_id', 'currency_code', name=op.f('pk_cash_point_currencies'))
    )
    # The legacy runtime keeps its cash_boxes; each existing box gets one modern cash point (code CAJA-<box id>).
    op.execute("INSERT INTO cash_points (tenant_id, branch_id, code, name, status, created_at, updated_at) "
               "SELECT b.company_id, b.id, 'CAJA-' || cb.id, 'Caja ' || b.name, 'active', now(), now() "
               "FROM cash_boxes cb JOIN branches b ON b.id = cb.branch_id")

    # --- role assignments: cash_point scope and tenant-safe scope references ---------------------------------
    op.drop_constraint(op.f('ck_user_role_assignments_scope_kind_valid'), 'user_role_assignments', type_='check')
    op.create_check_constraint('scope_kind_valid', 'user_role_assignments',
                               "scope_kind IN ('tenant', 'branch', 'cash_point', 'own')")
    op.add_column('user_role_assignments', sa.Column('cash_point_id', sa.Integer(), nullable=True))
    op.create_check_constraint('cash_point_scope_consistent', 'user_role_assignments',
                               "(scope_kind = 'cash_point') = (cash_point_id IS NOT NULL)")
    op.drop_index('uq_assignment_active_nonbranch', table_name='user_role_assignments')
    op.create_index('uq_assignment_active_nonbranch', 'user_role_assignments', ['user_id', 'role_id', 'scope_kind'],
                    unique=True,
                    postgresql_where=sa.text('revoked_at IS NULL AND branch_id IS NULL AND cash_point_id IS NULL'))
    op.create_index('uq_assignment_active_cash_point', 'user_role_assignments', ['user_id', 'role_id', 'cash_point_id'],
                    unique=True, postgresql_where=sa.text('revoked_at IS NULL AND cash_point_id IS NOT NULL'))
    op.create_foreign_key('fk_assignments_tenant_branch', 'user_role_assignments', 'branches',
                          ['tenant_id', 'branch_id'], ['company_id', 'id'])
    op.create_foreign_key('fk_assignments_tenant_cash_point', 'user_role_assignments', 'cash_points',
                          ['tenant_id', 'cash_point_id'], ['tenant_id', 'id'])
    op.create_foreign_key('fk_users_tenant_branch', 'users', 'branches', ['company_id', 'branch_id'],
                          ['company_id', 'id'])

    # --- permissions: new catalogue entries, granted to the existing tenant admin system roles ---------------
    permissions = sa.table("permissions", sa.column("code", sa.String), sa.column("description", sa.String),
                           sa.column("scope_kind", sa.String), sa.column("is_sensitive", sa.Boolean),
                           sa.column("created_at", sa.DateTime(timezone=True)))
    op.bulk_insert(permissions, [
        {"code": c, "description": d, "scope_kind": k, "is_sensitive": x, "created_at": datetime.now(timezone.utc)}
        for c, k, d, x in NEW_PERMISSIONS
    ])
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("INSERT INTO role_permissions (role_id, permission_id, granted_at) "
               "SELECT r.id, p.id, now() FROM roles r JOIN permissions p ON p.scope_kind = 'tenant' "
               "WHERE r.system_defined AND r.tenant_id IS NOT NULL AND p.code IN (" + codes + ") "
               "ON CONFLICT DO NOTHING")


def downgrade() -> None:
    """Downgrade schema (best effort; data created with the new structures is dropped)."""
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN (" + codes + "))")
    op.execute("DELETE FROM permissions WHERE code IN (" + codes + ")")
    op.execute("DELETE FROM user_role_assignments WHERE scope_kind = 'cash_point'")
    op.drop_constraint('fk_users_tenant_branch', 'users', type_='foreignkey')
    op.drop_constraint('fk_assignments_tenant_cash_point', 'user_role_assignments', type_='foreignkey')
    op.drop_constraint('fk_assignments_tenant_branch', 'user_role_assignments', type_='foreignkey')
    op.drop_index('uq_assignment_active_cash_point', table_name='user_role_assignments')
    op.drop_index('uq_assignment_active_nonbranch', table_name='user_role_assignments')
    op.create_index('uq_assignment_active_nonbranch', 'user_role_assignments', ['user_id', 'role_id', 'scope_kind'],
                    unique=True, postgresql_where=sa.text('revoked_at IS NULL AND branch_id IS NULL'))
    op.drop_constraint(op.f('ck_user_role_assignments_cash_point_scope_consistent'), 'user_role_assignments', type_='check')
    op.drop_column('user_role_assignments', 'cash_point_id')
    op.drop_constraint(op.f('ck_user_role_assignments_scope_kind_valid'), 'user_role_assignments', type_='check')
    op.create_check_constraint('scope_kind_valid', 'user_role_assignments', "scope_kind IN ('tenant', 'branch', 'own')")
    op.drop_table('cash_point_currencies')
    op.drop_index(op.f('ix_cash_points_tenant_id'), table_name='cash_points')
    op.drop_index(op.f('ix_cash_points_branch_id'), table_name='cash_points')
    op.drop_table('cash_points')
    op.add_column('branches', sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.execute("UPDATE branches SET is_active = (status = 'active')")
    op.alter_column('branches', 'is_active', server_default=None)
    op.drop_constraint(op.f('ck_branches_code_format'), 'branches', type_='check')
    op.drop_constraint(op.f('ck_branches_status_valid'), 'branches', type_='check')
    op.drop_constraint('uq_branches_tenant_id', 'branches', type_='unique')
    op.drop_constraint('uq_branches_tenant_code', 'branches', type_='unique')
    for column in ('updated_at', 'timezone_override', 'status', 'code'):
        op.drop_column('branches', column)
    op.add_column('companies', sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.execute("UPDATE companies SET is_active = (status = 'active')")
    op.alter_column('companies', 'is_active', server_default=None)
    op.drop_constraint(op.f('ck_companies_status_valid'), 'companies', type_='check')
    op.drop_constraint('fk_companies_base_currency_enabled', 'companies', type_='foreignkey')
    for column in ('updated_at', 'default_timezone', 'base_currency_code', 'status'):
        op.drop_column('companies', column)
    op.drop_table('tenant_currencies')
    op.drop_table('currencies')
