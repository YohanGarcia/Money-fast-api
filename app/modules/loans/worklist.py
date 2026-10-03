"""Collection worklist (T-011): READ-ONLY list of loans with overdue NET debt. No write of any kind.

* Overdue is exactly T-010's rule (``allocation.is_overdue``: ``business_date > effective due_date`` and net outstanding
  > 0) over the T-008/T-009 net ledger (``ledger.applied_by_obligation_many``): there is no second formula here.
* Each loan is evaluated with ITS OWN frozen contract timezone (``contract_snapshot.product.snapshot.rules.calendar.timezone``);
  the live product, ``date.today()`` and the UTC date are never used.
* The stored ``loan.status`` is NOT a filter: a loan stored ``active`` that is overdue today shows up, one stored
  ``past_due`` that is no longer overdue does not. ``projected_status`` is derived.
* Scope: ``collections.read`` on the loan's MANAGING branch. A loan without managing branch needs tenant scope. The
  ``branch_id`` filter can only narrow what the actor already sees. No legacy ``collector`` role, no assignment.
* The row carries ``customer_id`` only (no PII), no bucket, no priority/score, no custody, no mora charge.
* Cost: two SELECTs for the candidates (loans, then obligations + net applications of ALL candidates, batched) and one for
  the last payments of the PAGE only; never one query per loan. Sorting/keyset paging happen over the derived rows, so the
  cost grows with the number of candidate loans (those with an obligation due on or before today), not with the page.
"""

import base64
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from sqlalchemy import and_, exists, select, text
from sqlalchemy.orm import Session

from app.core.time import now_utc
from app.models.branch import Branch
from app.modules.customers.service import visible_branch_ids
from app.modules.identity.authorization import Principal
from app.modules.identity.errors import PermissionDenied, TenantMismatch
from app.modules.loans import allocation, ledger
from app.modules.loans.errors import InvalidCursor
from app.modules.loans.models import (
    CreditLoan,
    CreditLoanObligation,
    CreditPayment,
    CreditPaymentReversal,
)
from app.modules.loans.payments import _gate_tenant
from app.modules.origination.models import CreditFormalization
from app.modules.origination.service import _amt

READ = "collections.read"
SORTS = ("days_overdue", "overdue_outstanding", "oldest_overdue_date")
Sort = Literal["days_overdue", "overdue_outstanding", "oldest_overdue_date"]
Order = Literal["asc", "desc"]
_TZ_PATH = "{product,snapshot,rules,calendar,timezone}"


# --- cursor (parseable and validated; bound to the sort and order that produced it) -----------------------------
def encode_cursor(sort: str, order: str, value, loan_id: int) -> str:
    raw = json.dumps({"s": sort, "o": order, "v": str(value), "i": loan_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, sort: str, order: str):
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
        if not isinstance(raw, dict) or set(raw) != {"s", "o", "v", "i"}:
            raise ValueError
        if raw["s"] != sort or raw["o"] != order:  # a cursor of another ordering is never reused
            raise ValueError
        loan_id = int(raw["i"])
        value = {
            "days_overdue": int,
            "overdue_outstanding": Decimal,
            "oldest_overdue_date": date.fromisoformat,
        }[sort](raw["v"])
        return value, loan_id
    except (ValueError, TypeError, KeyError, InvalidOperation, UnicodeDecodeError):
        raise InvalidCursor() from None


# --- the query ---------------------------------------------------------------------------------------------------
def _candidates(db: Session, actor: Principal, scope: set[int] | None, branch_id, currency, now: datetime):
    """Loans of the tenant that may have overdue debt, with their FROZEN contract timezone. Cheap pre-filter only (an
    obligation due on or before today's UTC date: the local date is at most one day ahead of UTC); the exact overdue
    test is done by the shared helpers on the net ledger."""
    tz = CreditFormalization.contract_snapshot.op("#>>")(text(f"'{_TZ_PATH}'"))
    stmt = (
        select(
            CreditLoan.id,
            CreditLoan.loan_number,
            CreditLoan.customer_id,
            CreditLoan.managing_branch_id,
            CreditLoan.currency_code,
            tz.label("tz"),
        )
        .join(
            CreditFormalization,
            and_(
                CreditFormalization.id == CreditLoan.formalization_id, CreditFormalization.tenant_id == actor.tenant_id
            ),
        )
        .where(
            CreditLoan.tenant_id == actor.tenant_id,
            CreditLoan.status.in_(ledger.PROJECTABLE_STATUSES),  # NOT a status filter: past_due may be stale or missing
            exists().where(CreditLoanObligation.loan_id == CreditLoan.id, CreditLoanObligation.due_date <= now.date()),
        )
    )
    if scope is not None:  # branch-level actor: only loans MANAGED by a branch of theirs (NULL never matches)
        stmt = stmt.where(CreditLoan.managing_branch_id.in_(scope))
    if branch_id is not None:
        stmt = stmt.where(CreditLoan.managing_branch_id == branch_id)
    if currency is not None:
        stmt = stmt.where(CreditLoan.currency_code == currency)
    return db.execute(stmt).all()


def _sort_value(row: dict, sort: str):
    return row[sort]


def _after(row: dict, sort: str, order: str, cursor) -> bool:
    cv, cid = cursor
    v = _sort_value(row, sort)
    if v == cv:
        return row["loan_id"] > cid  # the tie-break is always ascending loan_id
    return v > cv if order == "asc" else v < cv


def _last_net_payments(db: Session, tenant_id: int, loan_ids: list[int]) -> dict[int, dict]:
    """Latest economically VALID payment per loan: a payment with a reversal is excluded (T-009 reverses in full).
    Newest by business date, then created_at, then id. One query for the whole page."""
    if not loan_ids:
        return {}
    stmt = (
        select(
            CreditPayment.loan_id,
            CreditPayment.id,
            CreditPayment.payment_number,
            CreditPayment.amount,
            CreditPayment.currency_code,
            CreditPayment.business_date,
            CreditPayment.origin,
        )
        .where(
            CreditPayment.tenant_id == tenant_id,
            CreditPayment.loan_id.in_(loan_ids),
            ~exists().where(CreditPaymentReversal.payment_id == CreditPayment.id),
        )
        .distinct(CreditPayment.loan_id)
        .order_by(
            CreditPayment.loan_id,
            CreditPayment.business_date.desc(),
            CreditPayment.created_at.desc(),
            CreditPayment.id.desc(),
        )
    )
    return {
        r.loan_id: {
            "payment_id": r.id,
            "payment_number": r.payment_number,
            "amount": _amt(r.amount),
            "currency": r.currency_code,
            "business_date": r.business_date,
            "origin": r.origin,
        }
        for r in db.execute(stmt)
    }


def overdue_loans(
    db: Session,
    actor: Principal,
    *,
    branch_id: int | None,
    min_days_overdue: int | None,
    currency: str | None,
    sort: str,
    order: str,
    limit: int,
    cursor: str | None,
) -> dict:
    _gate_tenant(actor)
    scope = visible_branch_ids(actor, READ)  # None = tenant scope; raises 403 without any collections.read grant
    if branch_id is not None:
        branch = db.get(Branch, branch_id)
        if branch is None or branch.company_id != actor.tenant_id:
            raise TenantMismatch()  # another tenant's branch is a 404
        if not actor.allows(READ, tenant_id=actor.tenant_id, branch_id=branch_id):
            raise PermissionDenied()  # the filter can narrow the actor's scope, never widen it
    after = decode_cursor(cursor, sort, order) if cursor else None
    now = now_utc()
    cands = _candidates(db, actor, scope, branch_id, currency, now)
    views = ledger.views_many(db, [c.id for c in cands])
    rows: list[dict] = []
    for c in cands:
        bd = ledger.business_date_in(c.tz, now)  # THIS loan's frozen timezone
        v = views[c.id]
        s = allocation.overdue_summary(v, bd)
        if s["overdue_obligations"] == 0:
            continue
        if min_days_overdue is not None and s["max_days_overdue"] < min_days_overdue:
            continue
        rows.append(
            {
                "loan_id": c.id,
                "loan_number": c.loan_number,
                "customer_id": c.customer_id,  # the ONLY customer datum: no name, contact, document or location
                "managing_branch_id": c.managing_branch_id,
                "currency": c.currency_code,
                "projected_status": allocation.loan_status(v, bd),
                "overdue_obligations": s["overdue_obligations"],
                "days_overdue": s["max_days_overdue"],
                "overdue_outstanding": s["overdue_outstanding"],
                "oldest_overdue_date": allocation.oldest_overdue_date(v, bd),
                "next_due_date": allocation.next_due_date(v, bd),
            }
        )
    rows.sort(key=lambda r: r["loan_id"])  # stable: ties of the primary sort keep ascending loan_id
    rows.sort(key=lambda r: _sort_value(r, sort), reverse=order == "desc")
    if after is not None:
        rows = [r for r in rows if _after(r, sort, order, after)]
    page, more = rows[:limit], len(rows) > limit
    payments = _last_net_payments(db, actor.tenant_id, [r["loan_id"] for r in page])
    items = [
        {
            **r,
            "overdue_outstanding": _amt(r["overdue_outstanding"]),
            "last_net_payment": payments.get(r["loan_id"]),
        }
        for r in page
    ]
    nxt = encode_cursor(sort, order, _sort_value(page[-1], sort), page[-1]["loan_id"]) if more and page else None
    return {"items": items, "next_cursor": nxt, "sort": sort, "order": order, "limit": limit}
