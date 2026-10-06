"""Collection promise-to-pay (T-015): a customer's declared commitment to pay an amount on ONE loan by a date.

* Target = the loan; currency = the loan's (server-derived). Terms (amount, date) are immutable; the only change a row ever gets
  is ONE closing transition: ``cancelled`` (explicit cancel) or ``superseded`` (explicit replace). Nothing is edited or deleted.
* At most ONE row per loan is not closed (partial UNIQUE + the loan lock). "Current" = not closed, whatever its financial result.
* The financial outcome is NEVER stored: ``projected_status`` is derived on read from the NET payment ledger and the contract's
  business date (no scheduler, no write on read). Closed promises keep their terminal ``cancelled`` / ``superseded`` status.
    fulfilled  = qualifying paid amount >= promised amount
    broken     = not fulfilled and business date today > promise date
    open       = otherwise
  Qualifying payment = a confirmed payment of the loan with ``received_at >= promise.created_at``,
  ``business_date <= promise_date`` and without a reversal (reversals are full): any origin, summed cumulatively. A late payment
  never repairs a broken promise; a reversal re-projects (fulfilled -> open before the date, -> broken after it).
* Create / replace cap: ``promised_amount <= due_to_date_outstanding`` of the NET ledger evaluated AT the promise date (T-008 never
  lets a payment exceed what is due on its date), and that amount must be > 0. No new formula, no delinquency accrual.
* A promise is a commitment, not a payment: it moves no money, creates no payment, reduces no debt, changes neither overdue,
  delinquency, loan status nor the worklist, and creates no activity.
* Write (create / replace / cancel) = ``collections.promises.create`` on the loan's MANAGING branch (tenant-level when none); read =
  ``collections.read`` on the same boundary. Assignment, creator and activity grant nothing.
* ``managing_branch_id`` and ``assignment_id`` (the open T-012 assignment, or NULL) are server snapshots checked by an INSERT trigger.
* Idempotency: create/replace key in ``(tenant, idempotency_key)`` + digest; cancel/replace-closure in its own
  ``(tenant, close_idempotency_key)`` namespace. Authorization is re-checked BEFORE any replay.
* Lock order: 1. loan ``FOR UPDATE``  2. the current promise row  3. ledger reads (no row locks) and the open assignment  4. INSERT / close.
"""

import hashlib
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflict
from app.core.time import now_utc
from app.modules.credit.rules import canonical_json
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal
from app.modules.identity.errors import PermissionDenied, TenantMismatch
from app.modules.loans import allocation, ledger
from app.modules.loans.activities import _decode_cursor, _encode_cursor
from app.modules.loans.assignments import _covers, _locked_loan, _open, _readable_loan
from app.modules.loans.errors import (
    AlreadyHasOpenPromise,
    NoOpenPromise,
    PromiseAlreadyClosed,
    PromiseAmountExceedsDueByDate,
    PromiseAmountInvalid,
    PromiseDateInPast,
    PromiseInvariantViolation,
    PromiseNotApplicable,
    PromiseNotCancellable,
)
from app.modules.loans.models import CreditCollectionPromise, CreditLoan, CreditPayment, CreditPaymentReversal
from app.modules.loans.payments import _gate_tenant
from app.modules.loans.promise_schemas import CancelPromiseIn, CreatePromiseIn, ReplacePromiseIn
from app.modules.origination.service import _amt

CREATE = "collections.promises.create"
CENT = Decimal("0.01")
ZERO = Decimal("0")


def _digest(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _terms_digest(operation: str, loan_id: int, amount: Decimal, promise_date: date) -> str:
    return _digest(
        {
            "operation": operation,
            "loan_id": loan_id,
            "promised_amount": str(amount),
            "promise_date": promise_date.isoformat(),
        }
    )


def _cancel_digest(loan_id: int, promise_id: int) -> str:
    return _digest({"operation": "cancel_collection_promise", "loan_id": loan_id, "promise_id": promise_id})


def _money(raw: str) -> Decimal:
    """Positive and expressed in cents: the same precision a payment can actually be made in."""
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise PromiseAmountInvalid() from None
    if amount <= 0 or amount != amount.quantize(CENT):
        raise PromiseAmountInvalid()
    return amount.quantize(CENT)


# --- the derived outcome ------------------------------------------------------------------------------------
def qualifying_paid_amounts(db: Session, tenant_id: int, promises: list[CreditCollectionPromise]) -> dict[int, Decimal]:
    """THE definition of the qualifying payments (shared by the T-015 reads and the T-016 worklist). ONE query for any number of promises: net payments of the loan received since the promise was created and dated no later
    than the promise date, never reversed (a reversed payment contributes 0), any origin."""
    if not promises:
        return {}
    pay, p = CreditPayment, CreditCollectionPromise
    stmt = (
        select(p.id, func.coalesce(func.sum(pay.amount), ZERO))
        .select_from(p)
        .outerjoin(
            pay,
            and_(
                pay.tenant_id == p.tenant_id,
                pay.loan_id == p.loan_id,
                pay.status == "confirmed",
                pay.received_at >= p.created_at,
                pay.business_date <= p.promise_date,
                ~exists().where(CreditPaymentReversal.payment_id == pay.id),
            ),
        )
        .where(p.tenant_id == tenant_id, p.id.in_([x.id for x in promises]))
        .group_by(p.id)
    )
    return {pid: Decimal(total) for pid, total in db.execute(stmt)}


def projected_status(row: CreditCollectionPromise, paid: Decimal, today: date) -> str:
    if row.closed_kind is not None:  # a terminal lifecycle status is never reopened by payments or reversals
        return row.closed_kind
    if paid >= row.promised_amount:
        return "fulfilled"
    return "broken" if today > row.promise_date else "open"


def _view(row: CreditCollectionPromise, paid: Decimal, today: date) -> dict:
    return {
        "promise_id": row.id,
        "loan_id": row.loan_id,
        "managing_branch_id": row.managing_branch_id,
        "assignment_id": row.assignment_id,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "currency_code": row.currency_code,
        "promised_amount": _amt(row.promised_amount),
        "promise_date": row.promise_date,
        "supersedes_promise_id": row.supersedes_promise_id,
        "closed_at": row.closed_at,
        "closed_by": row.closed_by,
        "closed_kind": row.closed_kind,
        "projected_status": projected_status(row, paid, today),
        "qualifying_paid_amount": _amt(paid),
    }


def _views(db: Session, loan: CreditLoan, rows: list[CreditCollectionPromise]) -> list[dict]:
    today = ledger.business_date(db, loan, now_utc())
    paid = qualifying_paid_amounts(db, loan.tenant_id, rows)
    return [_view(r, paid.get(r.id, ZERO), today) for r in rows]


def current_mini_views(db: Session, tenant_id: int, business_date_by_loan: dict[int, date]) -> dict[int, dict]:
    """T-016 worklist enrichment for the loans of ONE PAGE (the keys): the promise that is not closed (never a closed one, never a
    historical fallback) with its derived status, in the loan's own business date. Two queries at most (none when the page has no
    current promise): the current rows, then the shared qualifying-payment aggregation. Pure read."""
    if not business_date_by_loan:
        return {}
    rows = list(
        db.scalars(
            select(CreditCollectionPromise).where(
                CreditCollectionPromise.tenant_id == tenant_id,
                CreditCollectionPromise.loan_id.in_(list(business_date_by_loan)),
                CreditCollectionPromise.closed_at.is_(None),
            )
        )
    )
    paid = qualifying_paid_amounts(db, tenant_id, rows)
    return {
        r.loan_id: {
            "promise_id": r.id,
            "promised_amount": _amt(r.promised_amount),
            "currency_code": r.currency_code,
            "promise_date": r.promise_date,
            "projected_status": projected_status(r, paid.get(r.id, ZERO), business_date_by_loan[r.loan_id]),
            "qualifying_paid_amount": _amt(paid.get(r.id, ZERO)),
            "created_at": r.created_at,
        }
        for r in rows
    }


def _current(db: Session, tenant_id: int, loan_id: int, *, lock: bool) -> CreditCollectionPromise | None:
    stmt = select(CreditCollectionPromise).where(
        CreditCollectionPromise.tenant_id == tenant_id,
        CreditCollectionPromise.loan_id == loan_id,
        CreditCollectionPromise.closed_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return db.scalar(stmt)


# --- shared validation of new terms (create and replace) ----------------------------------------------------
def _validate_terms(db: Session, loan: CreditLoan, amount: Decimal, promise_date: date, today: date) -> None:
    if promise_date < today:  # today is allowed; the past would be born broken
        raise PromiseDateInPast()
    _obligations, views = ledger.views(db, loan.id)  # the NET ledger (applications minus reversal applications)
    due = allocation.due_to_date_outstanding(views, promise_date)  # evaluated AT the promise date, not today
    if due <= 0:  # economically settled / nothing collectable by that date (stored status is not the truth)
        raise PromiseNotApplicable()
    if amount > due:
        raise PromiseAmountExceedsDueByDate()


def _insert(
    db: Session, actor: Principal, loan: CreditLoan, now, amount, promise_date, key, digest, supersedes: int | None
) -> CreditCollectionPromise:
    open_assignment = _open(db, loan.id, lock=False)  # the snapshot (None = no open assignment)
    row = CreditCollectionPromise(
        tenant_id=actor.tenant_id,
        loan_id=loan.id,
        managing_branch_id=loan.managing_branch_id,  # snapshot of the loan's, never from the client
        assignment_id=open_assignment.id if open_assignment else None,
        created_by=actor.user_id,
        created_at=now,
        currency_code=loan.currency_code,  # the loan's: never from the client
        promised_amount=amount,
        promise_date=promise_date,
        supersedes_promise_id=supersedes,
        idempotency_key=key,
        request_digest=digest,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        message = str(exc.orig)
        if "idempotency" in message:
            raise IdempotencyConflict() from None
        if "uq_credit_collection_promises_current" in message:
            raise AlreadyHasOpenPromise() from None
        raise PromiseInvariantViolation() from None
    return row


def _audit(db: Session, actor: Principal, loan: CreditLoan, event_type: str, client_ip, row, **extra) -> None:
    record_event(
        db,
        event_type,
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={
            "promise_id": row.id,
            "loan_id": loan.id,
            "loan_number": loan.loan_number,
            "promised_amount": _amt(row.promised_amount),
            "currency_code": row.currency_code,
            "promise_date": row.promise_date.isoformat(),
            "created_by": row.created_by,
            "assignment_id": row.assignment_id,
            "managing_branch_id": row.managing_branch_id,
            **extra,
        },
    )


def _by_key(db: Session, actor: Principal, key: str) -> CreditCollectionPromise | None:
    return db.scalar(
        select(CreditCollectionPromise).where(
            CreditCollectionPromise.tenant_id == actor.tenant_id, CreditCollectionPromise.idempotency_key == key
        )
    )


# --- commands -----------------------------------------------------------------------------------------------
def create(db: Session, actor: Principal, loan_id: int, body: CreatePromiseIn, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    loan = _locked_loan(db, actor, loan_id)  # 1. loan first (serializes with payments, reversals, assignments)
    if not _covers(actor, CREATE, loan):  # before any replay: an idempotency key never bypasses authorization
        raise PermissionDenied()
    amount = _money(body.promised_amount)
    digest = _terms_digest("create_collection_promise", loan.id, amount, body.promise_date)
    prior = _by_key(db, actor, body.idempotency_key)
    if prior is not None:  # decided under the loan lock; the UNIQUE constraint is the database backstop
        if prior.request_digest == digest and prior.loan_id == loan.id:
            return {**_views(db, loan, [prior])[0], "replayed": True}
        raise IdempotencyConflict()
    if (
        _current(db, actor.tenant_id, loan.id, lock=True) is not None
    ):  # 2. never a hidden supersede: replace is explicit
        raise AlreadyHasOpenPromise()
    now = now_utc()
    _validate_terms(db, loan, amount, body.promise_date, ledger.business_date(db, loan, now))  # 3.
    row = _insert(db, actor, loan, now, amount, body.promise_date, body.idempotency_key, digest, None)  # 4.
    _audit(db, actor, loan, "loan.collection_promise_created", client_ip, row)
    db.commit()
    return {**_views(db, loan, [row])[0], "replayed": False}


def replace(db: Session, actor: Principal, loan_id: int, body: ReplacePromiseIn, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    loan = _locked_loan(db, actor, loan_id)
    if not _covers(actor, CREATE, loan):
        raise PermissionDenied()
    amount = _money(body.promised_amount)
    digest = _terms_digest("replace_collection_promise", loan.id, amount, body.promise_date)
    prior = _by_key(db, actor, body.idempotency_key)  # durable even after the new promise is itself closed
    if prior is not None:
        if prior.request_digest == digest and prior.loan_id == loan.id and prior.supersedes_promise_id is not None:
            return {
                **_views(db, loan, [prior])[0],
                "superseded_promise_id": prior.supersedes_promise_id,
                "replayed": True,
            }
        raise IdempotencyConflict()
    old = _current(
        db, actor.tenant_id, loan.id, lock=True
    )  # any current promise may be replaced: open, fulfilled or broken
    if old is None:
        raise NoOpenPromise()
    now = now_utc()
    _validate_terms(db, loan, amount, body.promise_date, ledger.business_date(db, loan, now))
    old.closed_by, old.closed_at, old.closed_kind = actor.user_id, now, "superseded"
    old.close_idempotency_key, old.close_request_digest = body.idempotency_key, digest
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise PromiseInvariantViolation() from None
    row = _insert(db, actor, loan, now, amount, body.promise_date, body.idempotency_key, digest, old.id)
    _audit(db, actor, loan, "loan.collection_promise_replaced", client_ip, row, superseded_promise_id=old.id)
    db.commit()
    return {**_views(db, loan, [row])[0], "superseded_promise_id": old.id, "replayed": False}


def cancel(
    db: Session, actor: Principal, loan_id: int, promise_id: int, body: CancelPromiseIn, client_ip: str | None
) -> dict:
    _gate_tenant(actor)
    loan = _locked_loan(db, actor, loan_id)
    if not _covers(actor, CREATE, loan):
        raise PermissionDenied()
    digest = _cancel_digest(loan.id, promise_id)
    prior = db.scalar(
        select(CreditCollectionPromise).where(
            CreditCollectionPromise.tenant_id == actor.tenant_id,
            CreditCollectionPromise.close_idempotency_key == body.idempotency_key,
        )
    )
    if prior is not None:
        if prior.id == promise_id and prior.loan_id == loan.id and prior.close_request_digest == digest:
            return {**_views(db, loan, [prior])[0], "replayed": True}  # the very same cancel command, retried
        raise IdempotencyConflict()
    row = db.scalar(
        select(CreditCollectionPromise)
        .where(
            CreditCollectionPromise.tenant_id == actor.tenant_id,
            CreditCollectionPromise.loan_id == loan.id,
            CreditCollectionPromise.id == promise_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise TenantMismatch()
    if row.closed_at is not None:
        raise PromiseAlreadyClosed()  # a new key never "re-cancels" or relabels a closed promise
    now = now_utc()
    if _views(db, loan, [row])[0]["projected_status"] != "open":
        raise PromiseNotCancellable()  # a fulfilled / broken promise is not relabelled retroactively
    row.closed_by, row.closed_at, row.closed_kind = actor.user_id, now, "cancelled"
    row.close_idempotency_key, row.close_request_digest = body.idempotency_key, digest
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise PromiseInvariantViolation() from None
    _audit(db, actor, loan, "loan.collection_promise_cancelled", client_ip, row, closed_kind="cancelled")
    db.commit()
    return {**_views(db, loan, [row])[0], "replayed": False}


# --- reads (collections.read, same boundary as the other collection reads; pure) ----------------------------
def get_current(db: Session, actor: Principal, loan_id: int) -> dict:
    loan = _readable_loan(db, actor, loan_id)
    row = _current(db, actor.tenant_id, loan.id, lock=False)
    return {"loan_id": loan.id, "promise": _views(db, loan, [row])[0] if row else None}  # none yet is a normal state


def list_promises(db: Session, actor: Principal, loan_id: int, *, limit: int, cursor: str | None) -> dict:
    loan = _readable_loan(db, actor, loan_id)
    after = _decode_cursor(cursor) if cursor else None
    stmt = select(CreditCollectionPromise).where(
        CreditCollectionPromise.tenant_id == actor.tenant_id, CreditCollectionPromise.loan_id == loan.id
    )
    if after is not None:
        stmt = stmt.where(CreditCollectionPromise.id < after)  # keyset, newest first
    rows = list(db.scalars(stmt.order_by(CreditCollectionPromise.id.desc()).limit(limit + 1)))
    page, more = rows[:limit], len(rows) > limit
    return {
        "loan_id": loan.id,
        "items": _views(db, loan, page),
        "next_cursor": _encode_cursor(page[-1].id) if more and page else None,
        "limit": limit,
    }


def get_promise(db: Session, actor: Principal, loan_id: int, promise_id: int) -> dict:
    loan = _readable_loan(db, actor, loan_id)
    row = db.scalar(
        select(CreditCollectionPromise).where(
            CreditCollectionPromise.tenant_id == actor.tenant_id,
            CreditCollectionPromise.loan_id == loan.id,
            CreditCollectionPromise.id == promise_id,
        )
    )
    if row is None:
        raise TenantMismatch()  # missing, another loan's or another tenant's: the same safe 404
    return _views(db, loan, [row])[0]
