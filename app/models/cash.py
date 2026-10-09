"""Cash custody is separate from loan repayments. All amounts are RD$.

T-021: a session is anchored to a CashPoint (the operational cash position); ``cash_boxes`` stays as the branch container
of the legacy runtime. Lifecycle ``open -> closing -> closed`` and the other backstops live in ``app.modules.cash.ddl``.
"""
from datetime import UTC, datetime, date
from decimal import Decimal
from sqlalchemy import (DDL, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON,
                        Numeric, String, Text, UniqueConstraint, event, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.modules.cash import ddl

SESSION_STATES = ('open', 'closing', 'closed')
ACTIVE_SESSION_STATES = ('open', 'closing')
OPENING_SOURCES = ('legacy', 'zero', 'capital')
CONTRACTS = ('legacy', 'v2')
HANDOVER_PROVENANCES = ('legacy', 'migration', 'v2')
DIFFERENCE_STATUSES = ('pending_review', 'under_review', 'resolved', 'dismissed')
DIFFERENCE_PHASES = ('opening', 'closing')
DIFFERENCE_PROVENANCES = ('legacy_migration', 'v2')


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"

def now():
    return datetime.now(UTC)

class CashConfig(Base):
    __tablename__ = 'cash_configs'
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), primary_key=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    activated_by: Mapped[int] = mapped_column(ForeignKey('users.id'))

class CashBox(Base):
    __tablename__ = 'cash_boxes'
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey('branches.id'), unique=True)
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(12,2))
    version: Mapped[int] = mapped_column(default=1)

class CashSession(Base):
    __tablename__ = 'cash_sessions'
    __table_args__ = (
        CheckConstraint(_in('state', SESSION_STATES), name='state_valid'),
        CheckConstraint(_in('opening_source', OPENING_SOURCES), name='opening_source_valid'),
        CheckConstraint(_in('opening_contract', CONTRACTS), name='opening_contract_valid'),
        CheckConstraint(f"close_contract IS NULL OR {_in('close_contract', CONTRACTS)}", name='close_contract_valid'),
        CheckConstraint("(opening_contract = 'legacy') = (opening_source = 'legacy')", name='legacy_opening_consistent'),
        CheckConstraint("opening_contract = 'legacy' OR balance_base = 0", name='v2_balance_from_movements'),
        CheckConstraint("currency_code = 'DOP'", name='currency_dop'),
        CheckConstraint("opening_expected >= 0 AND opening_counted >= 0", name='opening_amounts_positive'),
        CheckConstraint("close_contract IS DISTINCT FROM 'v2' OR (counted >= 0 AND closing_expected IS NOT NULL "
                        "AND difference = counted - closing_expected)", name='v2_close_difference'),
        CheckConstraint("state = 'open' OR close_contract IS NOT NULL", name='closed_has_contract'),
        ForeignKeyConstraint(['tenant_id', 'cash_point_id'], ['cash_points.tenant_id', 'cash_points.id'],
                             name='fk_cash_sessions_tenant_cash_point'),
        Index('uq_cash_sessions_active_cash_point', 'cash_point_id', unique=True,
              postgresql_where=text("state IN ('open', 'closing')")),
        Index('uq_cash_sessions_open_key', 'tenant_id', 'open_idempotency_key', unique=True,
              postgresql_where=text('open_idempotency_key IS NOT NULL')),
        Index('uq_cash_sessions_close_key', 'tenant_id', 'close_idempotency_key', unique=True,
              postgresql_where=text('close_idempotency_key IS NOT NULL')),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    box_id: Mapped[int] = mapped_column(ForeignKey('cash_boxes.id'), index=True)
    # T-021 anchors; tenant and cash point are derived from the box by the insert trigger when a writer omits them
    tenant_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    cash_point_id: Mapped[int] = mapped_column(Integer)
    currency_code: Mapped[str] = mapped_column(String(3), default='DOP')
    opening_source: Mapped[str] = mapped_column(String(10))
    opening_contract: Mapped[str] = mapped_column(String(10), default='v2')
    close_contract: Mapped[str | None] = mapped_column(String(10), nullable=True)
    opening_denominations: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    closing_expected: Mapped[Decimal | None] = mapped_column(Numeric(12,2), nullable=True)
    balance_base: Mapped[Decimal] = mapped_column(Numeric(12,2), default=Decimal('0'))
    open_idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    open_request_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    close_idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    close_request_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    business_date: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(30), default='open')
    opening_expected: Mapped[Decimal] = mapped_column(Numeric(12,2))
    opening_counted: Mapped[Decimal] = mapped_column(Numeric(12,2))
    balance: Mapped[Decimal] = mapped_column(Numeric(12,2))
    counted: Mapped[Decimal | None] = mapped_column(Numeric(12,2))
    difference: Mapped[Decimal | None] = mapped_column(Numeric(12,2))
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    denominations: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default='')
    opened_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    # the owner of the custody: never NULL since T-021 (the insert trigger falls back to opened_by)
    cashier_id: Mapped[int] = mapped_column(ForeignKey('users.id', name='fk_cash_sessions_cashier_id'), index=True)
    closed_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(default=1)


class CashCustodyTransfer(Base):
    """Physical custody handover, distinct from loan-bank transfers.

    T-021: the closing handover to capital (``kind = 'closing_capital'``), one per session, ``pending -> confirmed``.
    ``provenance`` tells a v2 handover from adopted legacy ones (D9) and the ones migration 0020 created (D11)."""
    __tablename__ = 'cash_custody_transfers'
    __table_args__ = (
        UniqueConstraint('acceptance_id'),
        CheckConstraint("state IN ('pending', 'confirmed')", name='state_valid'),
        CheckConstraint("kind = 'closing_capital'", name='kind_valid'),
        CheckConstraint(_in('provenance', HANDOVER_PROVENANCES), name='provenance_valid'),
        CheckConstraint("currency_code = 'DOP'", name='currency_dop'),
        CheckConstraint("amount >= 0 AND (provenance = 'legacy' OR amount > 0)", name='amount_positive'),
        CheckConstraint("provenance <> 'v2' OR (to_user_id IS NOT NULL AND to_user_id <> from_user_id)",
                        name='v2_named_receiver'),
        Index('uq_cash_custody_transfers_closing_session', 'session_id', unique=True,
              postgresql_where=text("kind = 'closing_capital'")),
        Index('uq_cash_custody_transfers_accept_key', 'company_id', 'accept_idempotency_key', unique=True,
              postgresql_where=text('accept_idempotency_key IS NOT NULL')),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    box_id: Mapped[int] = mapped_column(ForeignKey('cash_boxes.id'), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey('cash_sessions.id'), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    from_user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    # the named receiver; NULL only for the handovers migration 0020 created for legacy closing_review cash (D11)
    to_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency_code: Mapped[str] = mapped_column(String(3), default='DOP')
    provenance: Mapped[str] = mapped_column(String(10), default='v2')
    accept_idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    accept_request_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state: Mapped[str] = mapped_column(String(30), default='pending')
    acceptance_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    acceptance_method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    accepted_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Link identifiers are intentionally application-level links: adding database
    # FKs in both directions would create a cyclic dependency between the three
    # ledger tables and complicate migrations.
    cash_movement_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    capital_movement_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    notes: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    version: Mapped[int] = mapped_column(default=1)

class CashDelivery(Base):
    __tablename__ = 'cash_deliveries'
    id: Mapped[int] = mapped_column(primary_key=True)
    box_id: Mapped[int] = mapped_column(ForeignKey('cash_boxes.id'), index=True)
    collector_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    declared: Mapped[Decimal] = mapped_column(Numeric(12,2))
    received: Mapped[Decimal | None] = mapped_column(Numeric(12,2))
    remaining: Mapped[Decimal | None] = mapped_column(Numeric(12,2))
    state: Mapped[str] = mapped_column(String(20), default='pending')
    cashier_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    session_id: Mapped[int | None] = mapped_column(ForeignKey('cash_sessions.id'))
    notes: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(default=1)

class CashAllocation(Base):
    __tablename__ = 'cash_allocations'
    __table_args__ = (UniqueConstraint('delivery_id','payment_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey('cash_deliveries.id'))
    payment_id: Mapped[int] = mapped_column(ForeignKey('payments.id'), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12,2))

class CashMovement(Base):
    """Append-only (T-021). Tenant, cash point and currency are inherited from the session by the insert trigger."""
    __tablename__ = 'cash_movements'
    __table_args__ = (
        CheckConstraint("currency_code = 'DOP'", name='currency_dop'),
        ForeignKeyConstraint(['tenant_id', 'cash_point_id'], ['cash_points.tenant_id', 'cash_points.id'],
                             name='fk_cash_movements_tenant_cash_point'),
        Index('uq_cash_movements_opening_fund', 'session_id', unique=True,
              postgresql_where=text("kind = 'opening_capital_fund'")),
        Index('uq_cash_movements_closing_transfer', 'session_id', unique=True,
              postgresql_where=text("kind = 'closing_capital_transfer'")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    box_id: Mapped[int] = mapped_column(ForeignKey('cash_boxes.id'), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey('cash_sessions.id'), index=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey('companies.id'), nullable=False)
    cash_point_id: Mapped[int | None] = mapped_column(Integer, nullable=False)
    currency_code: Mapped[str | None] = mapped_column(String(3), nullable=False)
    kind: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Numeric(12,2))
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    collector_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey('payments.id'))
    loan_id: Mapped[int | None] = mapped_column(ForeignKey('loans.id'))
    delivery_id: Mapped[int | None] = mapped_column(ForeignKey('cash_deliveries.id'))
    custody_transfer_id: Mapped[int | None] = mapped_column(ForeignKey('cash_custody_transfers.id', name='fk_cash_movements_custody_transfer_id'), nullable=True)
    reverses_id: Mapped[int | None] = mapped_column(ForeignKey('cash_movements.id'), unique=True)
    notes: Mapped[str] = mapped_column(Text)
    reference: Mapped[str] = mapped_column(String(160), default='')
    proof: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class CashSessionDifference(Base):
    """A physical difference observed at opening or close (DR-007): a record, never a balancing movement.

    T-021 only creates it (``pending_review``) and never changes it; review and resolution belong to a later package."""
    __tablename__ = 'cash_session_differences'
    __table_args__ = (
        CheckConstraint(_in('phase', DIFFERENCE_PHASES), name='phase_valid'),
        CheckConstraint(_in('status', DIFFERENCE_STATUSES), name='status_valid'),
        CheckConstraint(_in('provenance', DIFFERENCE_PROVENANCES), name='provenance_valid'),
        CheckConstraint("currency_code = 'DOP'", name='currency_dop'),
        CheckConstraint("difference = counted - expected AND difference <> 0 AND counted >= 0", name='figures_consistent'),
        CheckConstraint("provenance = 'legacy_migration' OR length(btrim(observation_note)) >= 3",
                        name='observation_required'),
        ForeignKeyConstraint(['tenant_id', 'cash_point_id'], ['cash_points.tenant_id', 'cash_points.id'],
                             name='fk_cash_session_differences_tenant_cash_point'),
        UniqueConstraint('session_id', 'phase', name='uq_cash_session_differences_session_phase'),
        Index('ix_cash_session_differences_review', 'tenant_id', 'status', 'id'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey('companies.id'))
    session_id: Mapped[int] = mapped_column(ForeignKey('cash_sessions.id'))
    cash_point_id: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(10))
    currency_code: Mapped[str] = mapped_column(String(3), default='DOP')
    expected: Mapped[Decimal] = mapped_column(Numeric(12,2))
    counted: Mapped[Decimal] = mapped_column(Numeric(12,2))
    difference: Mapped[Decimal] = mapped_column(Numeric(12,2))
    observation_note: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default='pending_review')
    provenance: Mapped[str] = mapped_column(String(20), default='v2')
    detected_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class CashTransfer(Base):
    __tablename__ = 'cash_transfers'
    id: Mapped[int] = mapped_column(primary_key=True)
    box_id: Mapped[int] = mapped_column(ForeignKey('cash_boxes.id'), index=True)
    collector_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    loan_id: Mapped[int] = mapped_column(ForeignKey('loans.id'))
    amount: Mapped[Decimal] = mapped_column(Numeric(12,2))
    reference: Mapped[str] = mapped_column(String(160))
    destination: Mapped[str] = mapped_column(String(160))
    bank_account_id: Mapped[int | None] = mapped_column(ForeignKey('bank_accounts.id'))
    proof: Mapped[dict] = mapped_column(JSON)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    payment_id: Mapped[int | None] = mapped_column(ForeignKey('payments.id'), unique=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    notes: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(default=1)

class CashAudit(Base):
    __tablename__ = 'cash_audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    box_id: Mapped[int] = mapped_column(ForeignKey('cash_boxes.id'), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    action: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class CashRequest(Base):
    __tablename__ = 'cash_requests'
    __table_args__ = (UniqueConstraint('company_id','actor_id','key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'))
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    key: Mapped[str] = mapped_column(String(80))
    digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)


# T-021 backstops (app.modules.cash.ddl). CREATE OR REPLACE: whichever table is created first brings every function.
for _table in sorted(ddl.TRIGGER_TABLES):
    for _fn in ddl.FUNCTIONS:
        event.listen(Base.metadata.tables[_table], 'after_create', DDL(_fn.replace('%', '%%')).execute_if(dialect='postgresql'))
for _sql in ddl.TRIGGERS:
    event.listen(Base.metadata.tables[_sql.split(' ON ')[1].split(' ')[0]], 'after_create',
                 DDL(_sql).execute_if(dialect='postgresql'))
