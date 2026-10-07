"""Collection activity (T-014): ONE management (call, visit, ...) recorded on ONE loan, as append-only business history.

* Target = the loan. Actor = the authenticated user (``recorded_by``); no proxy, no client actor/time/branch/assignment.
* Only ``activity_type`` (closed enum) is stored: no outcome, no free text, no contact data, no GPS, no attachment.
* ``created_at`` is the server's: no backdating, no future activity. Rows are never updated or deleted (database guard).
* ``managing_branch_id`` and ``assignment_id`` are snapshots taken under the loan lock (the open T-012 assignment or NULL) and
  checked again by an INSERT trigger. An assignment neither authorizes nor restricts the command: RBAC does.
* Write = ``collections.actions.create`` on the loan's MANAGING branch (tenant-level when the loan has none); read =
  ``collections.read`` on the same boundary. Neither being the recorder nor the snapshot assignee grants access.
* Any stored loan status accepts an activity (it is history, not a financial transition); nothing here moves money,
  changes the loan, the assignment or the worklist.
* Idempotency: ``(tenant, idempotency_key)`` + digest of (operation, loan, type). Authorization is re-checked BEFORE a replay.
* Lock order: 1. loan ``FOR UPDATE``  2. the open assignment row (read)  3. INSERT. No obligations, payments or Cash.
"""

import base64
import hashlib
import json

from sqlalchemy import Integer, column, exists, select, true, values
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflict
from app.modules.credit.rules import canonical_json
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal
from app.modules.identity.errors import PermissionDenied, TenantMismatch
from app.modules.loans.activity_schemas import CreateActivityIn
from app.modules.loans.assignments import _covers, _locked_loan, _open, _readable_loan
from app.modules.loans.errors import ActivityInvariantViolation, InvalidCursor
from app.modules.loans.models import CreditCollectionActivity
from app.modules.loans.payments import _gate_tenant

CREATE = "collections.actions.create"


def _digest(loan_id: int, activity_type: str) -> str:
    payload = {"operation": "create_collection_activity", "loan_id": loan_id, "activity_type": activity_type}
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _out(a: CreditCollectionActivity) -> dict:
    """IDs, the type and the server time only: no keys, digests, names or contact data."""
    return {
        "activity_id": a.id,
        "loan_id": a.loan_id,
        "managing_branch_id": a.managing_branch_id,
        "recorded_by": a.recorded_by,
        "assignment_id": a.assignment_id,
        "activity_type": a.activity_type,
        "created_at": a.created_at,
    }


def create(db: Session, actor: Principal, loan_id: int, body: CreateActivityIn, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    loan = _locked_loan(db, actor, loan_id)  # 1. loan first (serializes with assign / reassign / end)
    if not _covers(actor, CREATE, loan):  # before any replay: an idempotency key never bypasses authorization
        raise PermissionDenied()
    digest = _digest(loan.id, body.activity_type)
    prior = db.scalar(
        select(CreditCollectionActivity).where(
            CreditCollectionActivity.tenant_id == actor.tenant_id,
            CreditCollectionActivity.idempotency_key == body.idempotency_key,
        )
    )
    if prior is not None:  # decided under the loan lock; the UNIQUE constraint is the database backstop
        if prior.request_digest == digest and prior.loan_id == loan.id:
            return {**_out(prior), "replayed": True}
        raise IdempotencyConflict()
    open_assignment = _open(db, loan.id, lock=False)  # 2. the snapshot (None = no open assignment)
    row = CreditCollectionActivity(
        tenant_id=actor.tenant_id,
        loan_id=loan.id,
        managing_branch_id=loan.managing_branch_id,  # snapshot of the loan's, never from the client
        recorded_by=actor.user_id,
        assignment_id=open_assignment.id if open_assignment else None,
        activity_type=body.activity_type,
        idempotency_key=body.idempotency_key,
        request_digest=digest,
    )
    db.add(row)
    try:
        db.flush()  # 3. INSERT (the trigger re-checks both snapshots)
    except IntegrityError as exc:
        db.rollback()
        raise (IdempotencyConflict() if "idempotency" in str(exc.orig) else ActivityInvariantViolation()) from None
    record_event(
        db,
        "loan.collection_activity_created",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={
            "activity_id": row.id,
            "loan_id": loan.id,
            "loan_number": loan.loan_number,
            "activity_type": row.activity_type,
            "recorded_by": row.recorded_by,
            "assignment_id": row.assignment_id,
            "managing_branch_id": row.managing_branch_id,
        },
    )
    db.commit()
    return {**_out(row), "replayed": False}


def latest_by_loan(db: Session, tenant_id: int, loan_ids: list[int]) -> dict[int, dict]:
    """T-016 worklist enrichment for the loans of ONE PAGE: the activity with the greatest ``id`` of each loan (the canonical
    ``id DESC`` order of the history), at most one per loan, in ONE query. LATERAL + ``LIMIT 1`` reads one index entry per loan
    (``(tenant_id, loan_id, id)`` backwards): a global ``DISTINCT ON`` would sort every activity of a busy loan. Pure read; no
    user, assignment or contact data."""
    if not loan_ids:
        return {}
    wanted = values(column("loan_id", Integer), name="wanted").data([(i,) for i in loan_ids])
    a = CreditCollectionActivity
    newest = (
        select(a.id, a.activity_type, a.created_at)
        .where(a.tenant_id == tenant_id, a.loan_id == wanted.c.loan_id)
        .order_by(a.id.desc())
        .limit(1)
        .lateral("newest")
    )
    stmt = (
        select(wanted.c.loan_id, newest.c.id, newest.c.activity_type, newest.c.created_at)
        .select_from(wanted)
        .join(newest, true())
    )
    return {
        r.loan_id: {"activity_id": r.id, "activity_type": r.activity_type, "created_at": r.created_at}
        for r in db.execute(stmt)
    }


def restrict_candidates(stmt, tenant_id: int, loan_id_column, activity: str):
    """T-018 worklist membership: ``has_activity`` = at least one activity of the loan ever, ``no_activity`` = none. Exact
    ``EXISTS`` / ``NOT EXISTS`` correlated on tenant + loan only: type, recorder, date, count and the snapshots
    (``assignment_id``, ``managing_branch_id``) never matter. One index probe on ``(tenant_id, loan_id, id)`` per loan at most,
    never a sort of the history. Pure read."""
    a = CreditCollectionActivity
    any_activity = exists().where(a.tenant_id == tenant_id, a.loan_id == loan_id_column)
    return stmt.where(any_activity if activity == "has_activity" else ~any_activity)


# --- reads (collections.read, same boundary as the worklist and the assignment reads; pure) ------------------
def _encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"i": last_id}, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
        if (
            not isinstance(raw, dict)
            or set(raw) != {"i"}
            or isinstance(raw["i"], bool)
            or not isinstance(raw["i"], int)
        ):
            raise ValueError
        if raw["i"] <= 0:
            raise ValueError
        return raw["i"]
    except (ValueError, TypeError, UnicodeDecodeError):
        raise InvalidCursor() from None


def list_activities(db: Session, actor: Principal, loan_id: int, *, limit: int, cursor: str | None) -> dict:
    loan = _readable_loan(db, actor, loan_id)
    after = _decode_cursor(cursor) if cursor else None
    stmt = select(CreditCollectionActivity).where(
        CreditCollectionActivity.tenant_id == actor.tenant_id, CreditCollectionActivity.loan_id == loan.id
    )
    if after is not None:
        stmt = stmt.where(CreditCollectionActivity.id < after)  # keyset, newest first
    rows = db.scalars(stmt.order_by(CreditCollectionActivity.id.desc()).limit(limit + 1)).all()
    page, more = rows[:limit], len(rows) > limit
    return {
        "loan_id": loan.id,
        "items": [_out(r) for r in page],
        "next_cursor": _encode_cursor(page[-1].id) if more and page else None,
        "limit": limit,
    }


def get_activity(db: Session, actor: Principal, loan_id: int, activity_id: int) -> dict:
    loan = _readable_loan(db, actor, loan_id)
    row = db.scalar(
        select(CreditCollectionActivity).where(
            CreditCollectionActivity.tenant_id == actor.tenant_id,
            CreditCollectionActivity.loan_id == loan.id,
            CreditCollectionActivity.id == activity_id,
        )
    )
    if row is None:
        raise TenantMismatch()  # missing, another loan's or another tenant's: the same safe 404
    return _out(row)
