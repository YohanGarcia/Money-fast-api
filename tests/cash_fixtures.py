"""Shared cash-session fixtures for the Credit and field-custody suites (T-021 contract).

Since T-021 a session is born ``open`` under the v2 contract, anchored to a CashPoint with at most one active session,
and its cash enters only through movements: ``zero`` (nothing) or ``capital`` (one ``opening_capital_fund`` backed by one
capital ``to_cash``). These helpers build such sessions directly (the database triggers still check them), so the
earlier suites keep testing their own rules on a real, valid cash custody.
"""

import itertools
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.capital import CapitalMovement
from app.models.cash import ACTIVE_SESSION_STATES, CashBox, CashMovement, CashSession
from app.modules.cash.ddl import DENOMINATIONS
from app.modules.organization.models import CashPoint

_codes = itertools.count(1)


def denominations_for(amount) -> dict[str, int]:
    """An exact denomination count for ``amount`` (greedy, largest first)."""
    rest, out = Decimal(amount), {}
    for d in DENOMINATIONS:
        qty = int(rest // Decimal(d))
        if qty:
            out[d] = qty
            rest -= Decimal(d) * qty
    assert rest == 0, amount
    return out


def free_cash_point(db, box_id: int) -> int:
    """The box's base CashPoint if it has no active session, otherwise a new CashPoint of the same branch (D1)."""
    box = db.get(CashBox, box_id)
    base = db.scalar(select(CashPoint).where(CashPoint.box_id == box_id))
    busy = db.scalar(
        select(CashSession.id).where(CashSession.cash_point_id == base.id, CashSession.state.in_(ACTIVE_SESSION_STATES))
    )
    if not busy:
        return base.id
    cp = CashPoint(
        tenant_id=box.company_id,
        branch_id=box.branch_id,
        code=f"TP{box_id}-{next(_codes)}",
        name="Caja de prueba",
        status="active",
        origin="manual",
    )
    db.add(cp)
    db.flush()
    return cp.id


def open_v2_session(
    db, *, box_id: int, cashier_id: int, balance="0.00", opened_by: int | None = None, cash_point_id: int | None = None
) -> CashSession:
    """An OPEN v2 session owned by ``cashier_id``: zero opening, or a capital opening of ``balance`` (capital is injected
    first, so the reserve nets to zero). Flushes; the caller commits."""
    box = db.get(CashBox, box_id)
    amount = Decimal(balance)
    actor = opened_by or cashier_id
    s = CashSession(
        box_id=box_id,
        tenant_id=box.company_id,
        cash_point_id=cash_point_id or free_cash_point(db, box_id),
        business_date=date.today(),
        state="open",
        opening_source="capital" if amount > 0 else "zero",
        opening_contract="v2",
        opening_expected=amount,
        opening_counted=amount,
        opening_denominations=denominations_for(amount) if amount > 0 else {},
        balance=Decimal(0),
        balance_base=Decimal(0),
        opened_by=actor,
        cashier_id=cashier_id,
    )
    db.add(s)
    db.flush()
    if amount > 0:
        db.add(CapitalMovement(company_id=box.company_id, kind="injection", amount=amount, actor_id=actor, notes="Fixture"))
        fund = CashMovement(
            box_id=box_id,
            session_id=s.id,
            kind="opening_capital_fund",
            amount=amount,
            actor_id=actor,
            notes="Fondo de apertura de prueba",
            reference=f"APE-{s.id}",
        )
        db.add(fund)
        db.flush()
        db.add(CapitalMovement(company_id=box.company_id, kind="to_cash", amount=amount, actor_id=actor,
                               notes="Fixture", cash_movement_id=fund.id))
        s.balance = amount
        db.flush()
    return s


def close_v2_session_empty(db, s: CashSession, actor_id: int) -> None:
    """Close an EMPTY open session directly (counted 0 = expected 0: no handover, no difference)."""
    assert s.balance == 0, "only an empty session closes without a handover"
    s.counted, s.closing_expected, s.difference = Decimal(0), Decimal(0), Decimal(0)
    s.denominations = {}
    s.close_contract = "v2"
    s.closed_by = actor_id
    from app.core.time import now_utc

    s.closed_at = now_utc()
    s.close_idempotency_key = f"fixture-close-{s.id}"
    s.close_request_digest = "fixture"
    s.state = "closed"
    db.flush()


def receiver_user(db, tenant_id: int, email: str = "receptor-fixture@x.com") -> int:
    """A plain active user of the tenant to receive a closing handover (maker != receiver)."""
    from app.modules.identity.models import UserAccount

    existing = db.scalar(select(UserAccount.id).where(UserAccount.company_id == tenant_id, UserAccount.email == email))
    if existing:
        return existing
    u = UserAccount(full_name="Receptor de cierre", email=email, password_hash="!", status="active", company_id=tenant_id)
    db.add(u)
    db.flush()
    return u.id


def close_with_handover(db, s: CashSession, receiver_id: int, counted=None, note: str = "") -> None:
    """A complete v2 close of a funded session: count (default = expected), pending handover of the count, receiver
    acceptance (one closing transfer + one capital from_cash), closed. A count that differs from the expected amount
    is recorded as a pending_review difference with ``note``. Flushes; the caller commits."""
    from app.core.time import now_utc
    from app.models.cash import CashCustodyTransfer, CashSessionDifference

    expected = Decimal(s.balance)
    amount = expected if counted is None else Decimal(counted)
    assert amount > 0
    s.counted, s.closing_expected, s.difference = amount, expected, amount - expected
    s.denominations = denominations_for(amount)
    s.close_contract, s.closed_by, s.closed_at = "v2", s.cashier_id, now_utc()
    s.close_idempotency_key, s.close_request_digest = f"fixture-close-{s.id}", "fixture"
    s.state = "closing"
    db.flush()
    if amount != expected:
        db.add(CashSessionDifference(tenant_id=s.tenant_id, session_id=s.id, cash_point_id=s.cash_point_id, phase="closing",
                                     currency_code="DOP", expected=expected, counted=amount, difference=amount - expected,
                                     observation_note=note, status="pending_review", provenance="v2",
                                     detected_by=s.cashier_id, detected_at=now_utc()))
        db.flush()
    h = CashCustodyTransfer(company_id=s.tenant_id, box_id=s.box_id, session_id=s.id, kind="closing_capital",
                            from_user_id=s.cashier_id, to_user_id=receiver_id, amount=amount, state="pending",
                            provenance="v2", currency_code="DOP")
    db.add(h)
    db.flush()
    m = CashMovement(box_id=s.box_id, session_id=s.id, kind="closing_capital_transfer", amount=-amount,
                     actor_id=receiver_id, notes="Entrega de cierre de prueba", reference=f"CUST-{h.id}",
                     custody_transfer_id=h.id)
    db.add(m)
    db.flush()
    c = CapitalMovement(company_id=s.tenant_id, kind="from_cash", amount=amount, actor_id=receiver_id, notes="Fixture",
                        cash_movement_id=m.id)
    db.add(c)
    db.flush()
    s.balance = expected - amount
    h.state, h.accepted_by, h.accepted_at = "confirmed", receiver_id, now_utc()
    h.acceptance_id, h.acceptance_method = f"fixture-accept-{h.id}", "authenticated_confirmation"
    h.accept_idempotency_key, h.accept_request_digest = f"fixture-accept-{h.id}", "fixture"
    h.cash_movement_id, h.capital_movement_id = m.id, c.id
    db.flush()
    s.state = "closed"
    db.flush()


def close_session_now(session_id: int) -> None:
    """Close an open fixture session the T-021 way (handover when it holds cash, direct when empty)."""
    with SessionLocal() as db:
        s = db.get(CashSession, session_id)
        if s.balance > 0:
            close_with_handover(db, s, receiver_user(db, s.tenant_id))
        else:
            close_v2_session_empty(db, s, s.cashier_id)
        db.commit()


def session_with_state(tenant: dict, box_id: int, cashier_id: int, state="open", balance="0.00", opened_by=None) -> int:
    """``open`` with ``balance``; ``closed`` = an empty session closed directly (a session that admits no movement)."""
    with SessionLocal() as db:
        if state == "open":
            s = open_v2_session(db, box_id=box_id, cashier_id=cashier_id, balance=balance, opened_by=opened_by)
        elif state == "closed":
            s = open_v2_session(db, box_id=box_id, cashier_id=cashier_id, opened_by=opened_by)
            close_v2_session_empty(db, s, cashier_id)
        else:
            raise AssertionError(f"unsupported fixture state {state!r}")
        db.commit()
        return s.id


def grant_cash_permissions(tenant_id: int, user_id: int, codes: list[str], branch_id: int | None = None) -> None:
    """Give a (legacy-created) user an explicit role with ``codes`` (branch scope when ``branch_id``): T-021 never grants
    cash permissions implicitly by legacy role."""
    from app.modules.identity.models import Permission, Role, RolePermission, UserRoleAssignment

    with SessionLocal() as db:
        role = Role(tenant_id=tenant_id, name=f"Caja QA {user_id}-{next(_codes)}", description="", status="active")
        db.add(role)
        db.flush()
        for code in codes:
            db.add(RolePermission(role_id=role.id, permission_id=db.scalar(select(Permission.id).where(Permission.code == code))))
        db.add(UserRoleAssignment(tenant_id=tenant_id, user_id=user_id, role_id=role.id,
                                  scope_kind="branch" if branch_id else "tenant", branch_id=branch_id))
        db.commit()
