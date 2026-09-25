"""Cash custody is separate from loan repayments. All amounts are RD$."""
from datetime import UTC, datetime, date
from decimal import Decimal
from sqlalchemy import Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

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
    id: Mapped[int] = mapped_column(primary_key=True)
    box_id: Mapped[int] = mapped_column(ForeignKey('cash_boxes.id'), index=True)
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
    cashier_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', name='fk_cash_sessions_cashier_id'), nullable=True, index=True)
    closed_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(default=1)


class CashCustodyTransfer(Base):
    """Physical custody handover, distinct from loan-bank transfers."""
    __tablename__ = 'cash_custody_transfers'
    __table_args__ = (UniqueConstraint('acceptance_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    box_id: Mapped[int] = mapped_column(ForeignKey('cash_boxes.id'), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey('cash_sessions.id'), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    from_user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    to_user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    state: Mapped[str] = mapped_column(String(30), default='pending')
    acceptance_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    acceptance_method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    accepted_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Link identifiers are intentionally application-level links: adding database
    # FKs in both directions would create a cyclic dependency between the three
    # ledger tables and complicate portable SQLite/PostgreSQL migrations.
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
    __tablename__ = 'cash_movements'
    id: Mapped[int] = mapped_column(primary_key=True)
    box_id: Mapped[int] = mapped_column(ForeignKey('cash_boxes.id'), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey('cash_sessions.id'))
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
