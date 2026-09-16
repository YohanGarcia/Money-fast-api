"""Add branch cash custody and preserve existing payments as historical."""
from alembic import op
import sqlalchemy as sa
revision = 'e0f1a2b3c4d5'
down_revision = 'd9e0f1a2b3c4'
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns('customers')}
    with op.batch_alter_table('customers') as batch:
        if 'cash_branch_id' not in existing:
            batch.add_column(sa.Column('cash_branch_id', sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_customers_cash_branch_id", "branches", ['cash_branch_id'], ["id"])
    existing = {c["name"] for c in sa.inspect(bind).get_columns('loans')}
    with op.batch_alter_table('loans') as batch:
        if 'cash_branch_id' not in existing:
            batch.add_column(sa.Column('cash_branch_id', sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_loans_cash_branch_id", "branches", ['cash_branch_id'], ["id"])
    existing = {c["name"] for c in sa.inspect(bind).get_columns('payments')}
    with op.batch_alter_table('payments') as batch:
        if 'branch_id' not in existing:
            batch.add_column(sa.Column('branch_id', sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_payments_branch_id", "branches", ['branch_id'], ["id"])
        if 'method' not in existing:
            batch.add_column(sa.Column('method', sa.String(20), nullable=True))
        if 'origin' not in existing:
            batch.add_column(sa.Column('origin', sa.String(20), nullable=True))
        if 'cash_state' not in existing:
            batch.add_column(sa.Column('cash_state', sa.String(20), nullable=False, server_default='historical'))
    if not sa.inspect(bind).has_table('cash_boxes'):
        op.create_table('cash_boxes',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), primary_key=False, nullable=False),
            sa.Column('branch_id', sa.Integer(), sa.ForeignKey('branches.id'), primary_key=False, nullable=False),
            sa.Column('initial_balance', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=False),
            sa.Column('version', sa.Integer(), primary_key=False, nullable=False),
            sa.UniqueConstraint('branch_id'),
        )
        op.create_index('ix_cash_boxes_company_id', 'cash_boxes', ['company_id'], unique=False)
    if not sa.inspect(bind).has_table('cash_audit'):
        op.create_table('cash_audit',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('box_id', sa.Integer(), sa.ForeignKey('cash_boxes.id'), primary_key=False, nullable=False),
            sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
            sa.Column('action', sa.String(length=40), primary_key=False, nullable=False),
            sa.Column('details', sa.JSON(), primary_key=False, nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
        )
        op.create_index('ix_cash_audit_box_id', 'cash_audit', ['box_id'], unique=False)
    if not sa.inspect(bind).has_table('cash_configs'):
        op.create_table('cash_configs',
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), primary_key=True, nullable=False),
            sa.Column('activated_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
            sa.Column('activated_by', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
        )
    if not sa.inspect(bind).has_table('cash_requests'):
        op.create_table('cash_requests',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), primary_key=False, nullable=False),
            sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
            sa.Column('key', sa.String(length=80), primary_key=False, nullable=False),
            sa.Column('digest', sa.String(length=64), primary_key=False, nullable=False),
            sa.Column('result', sa.JSON(), primary_key=False, nullable=False),
            sa.UniqueConstraint('company_id','actor_id','key'),
        )
    if not sa.inspect(bind).has_table('cash_sessions'):
        op.create_table('cash_sessions',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('box_id', sa.Integer(), sa.ForeignKey('cash_boxes.id'), primary_key=False, nullable=False),
            sa.Column('business_date', sa.Date(), primary_key=False, nullable=False),
            sa.Column('state', sa.String(length=30), primary_key=False, nullable=False),
            sa.Column('opening_expected', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=False),
            sa.Column('opening_counted', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=False),
            sa.Column('balance', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=False),
            sa.Column('counted', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=True),
            sa.Column('difference', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=True),
            sa.Column('snapshot', sa.JSON(), primary_key=False, nullable=False),
            sa.Column('denominations', sa.JSON(), primary_key=False, nullable=False),
            sa.Column('notes', sa.Text(), primary_key=False, nullable=False),
            sa.Column('opened_by', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
            sa.Column('closed_by', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=True),
            sa.Column('resolved_by', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=True),
            sa.Column('opened_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
            sa.Column('closed_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
            sa.Column('version', sa.Integer(), primary_key=False, nullable=False),
        )
        op.create_index('ix_cash_sessions_box_id', 'cash_sessions', ['box_id'], unique=False)
    if not sa.inspect(bind).has_table('cash_deliveries'):
        op.create_table('cash_deliveries',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('box_id', sa.Integer(), sa.ForeignKey('cash_boxes.id'), primary_key=False, nullable=False),
            sa.Column('collector_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
            sa.Column('declared', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=False),
            sa.Column('received', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=True),
            sa.Column('remaining', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=True),
            sa.Column('state', sa.String(length=20), primary_key=False, nullable=False),
            sa.Column('cashier_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=True),
            sa.Column('session_id', sa.Integer(), sa.ForeignKey('cash_sessions.id'), primary_key=False, nullable=True),
            sa.Column('notes', sa.Text(), primary_key=False, nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
            sa.Column('confirmed_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
            sa.Column('version', sa.Integer(), primary_key=False, nullable=False),
        )
        op.create_index('ix_cash_deliveries_collector_id', 'cash_deliveries', ['collector_id'], unique=False)
        op.create_index('ix_cash_deliveries_box_id', 'cash_deliveries', ['box_id'], unique=False)
    if not sa.inspect(bind).has_table('cash_allocations'):
        op.create_table('cash_allocations',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('delivery_id', sa.Integer(), sa.ForeignKey('cash_deliveries.id'), primary_key=False, nullable=False),
            sa.Column('payment_id', sa.Integer(), sa.ForeignKey('payments.id'), primary_key=False, nullable=False),
            sa.Column('amount', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=False),
            sa.UniqueConstraint('delivery_id','payment_id'),
        )
        op.create_index('ix_cash_allocations_payment_id', 'cash_allocations', ['payment_id'], unique=False)
    if not sa.inspect(bind).has_table('cash_movements'):
        op.create_table('cash_movements',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('box_id', sa.Integer(), sa.ForeignKey('cash_boxes.id'), primary_key=False, nullable=False),
            sa.Column('session_id', sa.Integer(), sa.ForeignKey('cash_sessions.id'), primary_key=False, nullable=False),
            sa.Column('kind', sa.String(length=30), primary_key=False, nullable=False),
            sa.Column('amount', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=False),
            sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
            sa.Column('collector_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=True),
            sa.Column('payment_id', sa.Integer(), sa.ForeignKey('payments.id'), primary_key=False, nullable=True),
            sa.Column('loan_id', sa.Integer(), sa.ForeignKey('loans.id'), primary_key=False, nullable=True),
            sa.Column('delivery_id', sa.Integer(), sa.ForeignKey('cash_deliveries.id'), primary_key=False, nullable=True),
            sa.Column('reverses_id', sa.Integer(), sa.ForeignKey('cash_movements.id'), primary_key=False, nullable=True),
            sa.Column('notes', sa.Text(), primary_key=False, nullable=False),
            sa.Column('reference', sa.String(length=160), primary_key=False, nullable=False),
            sa.Column('proof', sa.JSON(), primary_key=False, nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
            sa.UniqueConstraint('reverses_id'),
        )
        op.create_index('ix_cash_movements_box_id', 'cash_movements', ['box_id'], unique=False)
    if not sa.inspect(bind).has_table('cash_transfers'):
        op.create_table('cash_transfers',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('box_id', sa.Integer(), sa.ForeignKey('cash_boxes.id'), primary_key=False, nullable=False),
            sa.Column('collector_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
            sa.Column('loan_id', sa.Integer(), sa.ForeignKey('loans.id'), primary_key=False, nullable=False),
            sa.Column('amount', sa.Numeric(precision=12, scale=2), primary_key=False, nullable=False),
            sa.Column('reference', sa.String(length=160), primary_key=False, nullable=False),
            sa.Column('destination', sa.String(length=160), primary_key=False, nullable=False),
            sa.Column('proof', sa.JSON(), primary_key=False, nullable=False),
            sa.Column('payload', sa.JSON(), primary_key=False, nullable=False),
            sa.Column('state', sa.String(length=20), primary_key=False, nullable=False),
            sa.Column('payment_id', sa.Integer(), sa.ForeignKey('payments.id'), primary_key=False, nullable=True),
            sa.Column('reviewed_by', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=True),
            sa.Column('notes', sa.Text(), primary_key=False, nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), primary_key=False, nullable=False),
            sa.Column('reviewed_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
            sa.Column('version', sa.Integer(), primary_key=False, nullable=False),
            sa.UniqueConstraint('payment_id'),
        )
        op.create_index('ix_cash_transfers_box_id', 'cash_transfers', ['box_id'], unique=False)

def downgrade():
    op.drop_table('cash_transfers')
    op.drop_table('cash_movements')
    op.drop_table('cash_allocations')
    op.drop_table('cash_deliveries')
    op.drop_table('cash_sessions')
    op.drop_table('cash_requests')
    op.drop_table('cash_configs')
    op.drop_table('cash_audit')
    op.drop_table('cash_boxes')
    with op.batch_alter_table('payments') as batch:
        batch.drop_constraint("fk_payments_branch_id", type_="foreignkey")
        batch.drop_column('branch_id')
        batch.drop_column('method')
        batch.drop_column('origin')
        batch.drop_column('cash_state')
    with op.batch_alter_table('loans') as batch:
        batch.drop_constraint("fk_loans_cash_branch_id", type_="foreignkey")
        batch.drop_column('cash_branch_id')
    with op.batch_alter_table('customers') as batch:
        batch.drop_constraint("fk_customers_cash_branch_id", type_="foreignkey")
        batch.drop_column('cash_branch_id')
