"""payroll without Capital: bank ledger and idempotent payments (FM-FINANCIAL-CLOSURE-012)

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-14 09:00:00.000000

Additive over 0023. ``bank_account_movements`` is a minimal bank ledger (outflows of the company, never a balance);
``payroll_payments`` gains the bank account, a reference, an optional proof and an idempotency key/digest, and its
``source`` becomes ``cash | capital | bank`` (``capital`` stays valid for the historical rows). No row is rewritten,
nothing is backfilled and no Capital data is touched. "Agregar dinero a caja" needs no schema change: it is the
``cash_addition`` kind of an existing movement.

Downgrade is LOSSLESS ONLY: it refuses (before any change) when a bank payroll payment, a bank ledger row or an
idempotency key exists.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0024'
down_revision: Union[str, Sequence[str], None] = '0023'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'bank_account_movements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('bank_account_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('reference', sa.String(length=160), nullable=False),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('account_label', sa.String(length=300), nullable=False),
        sa.Column('payroll_payment_id', sa.Integer(), nullable=True),
        sa.Column('actor_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('amount <> 0', name=op.f('ck_bank_account_movements_amount_nonzero')),
        sa.CheckConstraint("kind IN ('payroll_payment')", name=op.f('ck_bank_account_movements_kind_valid')),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name=op.f('fk_bank_account_movements_actor_id_users')),
        sa.ForeignKeyConstraint(['bank_account_id'], ['bank_accounts.id'], name=op.f('fk_bank_account_movements_bank_account_id_bank_accounts')),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], name=op.f('fk_bank_account_movements_company_id_companies')),
        sa.ForeignKeyConstraint(['payroll_payment_id'], ['payroll_payments.id'], name='fk_bank_movements_payroll_payment'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_bank_account_movements')),
        sa.UniqueConstraint('payroll_payment_id', name=op.f('uq_bank_account_movements_payroll_payment_id')),
    )
    op.create_index(op.f('ix_bank_account_movements_company_id'), 'bank_account_movements', ['company_id'], unique=False)
    op.create_index(op.f('ix_bank_account_movements_bank_account_id'), 'bank_account_movements', ['bank_account_id'], unique=False)

    op.add_column('payroll_payments', sa.Column('bank_account_id', sa.Integer(), nullable=True))
    op.add_column('payroll_payments', sa.Column('reference', sa.String(length=160), nullable=False, server_default=''))
    op.add_column('payroll_payments', sa.Column('proof', sa.JSON(), nullable=True))
    op.add_column('payroll_payments', sa.Column('idempotency_key', sa.String(length=80), nullable=True))
    op.add_column('payroll_payments', sa.Column('request_digest', sa.String(length=80), nullable=True))
    op.create_foreign_key(op.f('fk_payroll_payments_bank_account_id_bank_accounts'), 'payroll_payments', 'bank_accounts', ['bank_account_id'], ['id'])
    op.create_check_constraint('source_valid', 'payroll_payments', "source IN ('cash', 'capital', 'bank')")
    op.create_check_constraint('bank_source_has_account', 'payroll_payments', "(source = 'bank') = (bank_account_id IS NOT NULL)")
    op.create_index('uq_payroll_payments_idempotency', 'payroll_payments', ['company_id', 'idempotency_key'], unique=True,
                    postgresql_where=sa.text('idempotency_key IS NOT NULL'))


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("LOCK TABLE payroll_payments, bank_account_movements IN ACCESS EXCLUSIVE MODE")
    problems = []
    for label, sql in (
        ("bank payroll payments exist", "SELECT id FROM payroll_payments WHERE source = 'bank' OR bank_account_id IS NOT NULL ORDER BY id"),
        ("bank ledger rows exist", "SELECT id FROM bank_account_movements ORDER BY id"),
        ("idempotent payroll payments exist", "SELECT id FROM payroll_payments WHERE idempotency_key IS NOT NULL ORDER BY id"),
    ):
        ids = [r[0] for r in bind.execute(sa.text(sql))]
        if ids:
            problems.append(f"{label}: ids {ids[:25]}")
    if problems:
        raise RuntimeError("cannot downgrade 0024: payroll history written under the new contract exists (" + "; ".join(problems)
                           + "). Nothing was changed: payments and ledger rows are never rewritten or deleted.")
    op.drop_index('uq_payroll_payments_idempotency', table_name='payroll_payments', postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.drop_constraint(op.f('ck_payroll_payments_bank_source_has_account'), 'payroll_payments', type_='check')
    op.drop_constraint(op.f('ck_payroll_payments_source_valid'), 'payroll_payments', type_='check')
    op.drop_constraint(op.f('fk_payroll_payments_bank_account_id_bank_accounts'), 'payroll_payments', type_='foreignkey')
    for col in ('request_digest', 'idempotency_key', 'proof', 'reference', 'bank_account_id'):
        op.drop_column('payroll_payments', col)
    op.drop_index(op.f('ix_bank_account_movements_bank_account_id'), table_name='bank_account_movements')
    op.drop_index(op.f('ix_bank_account_movements_company_id'), table_name='bank_account_movements')
    op.drop_table('bank_account_movements')
