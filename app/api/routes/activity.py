"""Registro de actividad: every movement of the company's users, searchable and filterable.

The administrator sees the whole company; a manager only the movements made in their branch. Backed by the
append-only ``security_events`` table (written by ``AuditMiddleware`` and the explicit audit calls).
"""

import csv
import io
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin_manager
from app.api.pagination import Page, page_params
from app.core.audit_catalog import CATEGORIES, category_of_event, category_prefixes, label_for
from app.models.branch import Branch
from app.models.user import User
from app.modules.identity.models import SecurityEvent

router = APIRouter()
MAX_EXPORT = 10_000
ROLE_LABELS = {"admin": "Administrador", "manager": "Gerente", "cashier": "Cajero", "collector": "Cobrador", "notary": "Notario"}
OUTCOME_LABELS = {"success": "Correcto", "failed": "Fallido", "denied": "Denegado"}
_FAILED = ("failed", "failure")  # the identity service writes "failure"


def _outcome(value: str) -> str:
    return "failed" if value in _FAILED else value


class Filters:
    def __init__(
        self,
        q: str | None = Query(default=None, max_length=80, description="Texto: resumen, usuario, entidad, IP"),
        category: str | None = Query(default=None, max_length=30),
        event_type: str | None = Query(default=None, max_length=60),
        outcome: str | None = Query(default=None, max_length=20),
        actor_id: int | None = None,
        actor_role: str | None = Query(default=None, max_length=20),
        branch_id: int | None = None,
        entity_type: str | None = Query(default=None, max_length=40),
        entity_id: str | None = Query(default=None, max_length=40),
        start: date | None = None,
        end: date | None = None,
    ) -> None:
        self.q, self.category, self.event_type, self.outcome = q, category, event_type, outcome
        self.actor_id, self.actor_role, self.branch_id = actor_id, actor_role, branch_id
        self.entity_type, self.entity_id, self.start, self.end = entity_type, entity_id, start, end


def _category_clause(category: str):
    prefixes = category_prefixes(category)
    if not prefixes:
        return SecurityEvent.category == category
    legacy = and_(SecurityEvent.category.is_(None), func.split_part(SecurityEvent.event_type, ".", 1).in_(prefixes))
    return or_(SecurityEvent.category == category, legacy)


def _base(user: User, company_id: int, f: Filters, *, with_category: bool = True):
    stmt = (
        select(SecurityEvent, User.full_name.label("actor_name"))
        .outerjoin(User, User.id == func.coalesce(SecurityEvent.actor_id, SecurityEvent.subject_id))
        .where(
            SecurityEvent.tenant_id == company_id,
            ~SecurityEvent.event_type.like("platform.%"),
            ~SecurityEvent.event_type.like("%.read"),
            ~SecurityEvent.event_type.like("%.read_sensitive"),
        )
    )
    if user.role == "manager":  # a manager only sees what happened in their own branch
        stmt = stmt.where(SecurityEvent.branch_id == user.branch_id)
    elif f.branch_id is not None:
        stmt = stmt.where(SecurityEvent.branch_id == f.branch_id)
    if with_category and f.category:
        stmt = stmt.where(_category_clause(f.category))
    if f.event_type:
        stmt = stmt.where(SecurityEvent.event_type.like(f.event_type.strip() + "%"))
    if f.outcome:
        stmt = stmt.where(SecurityEvent.outcome.in_(_FAILED if f.outcome in _FAILED else (f.outcome,)))
    if f.actor_id is not None:
        stmt = stmt.where(func.coalesce(SecurityEvent.actor_id, SecurityEvent.subject_id) == f.actor_id)
    if f.actor_role:
        stmt = stmt.where(SecurityEvent.actor_role == f.actor_role)
    if f.entity_type:
        stmt = stmt.where(SecurityEvent.entity_type == f.entity_type)
    if f.entity_id:
        stmt = stmt.where(SecurityEvent.entity_id == f.entity_id)
    if f.start:
        stmt = stmt.where(SecurityEvent.occurred_at >= datetime.combine(f.start, time.min, tzinfo=UTC))
    if f.end:
        stmt = stmt.where(SecurityEvent.occurred_at < datetime.combine(f.end + timedelta(days=1), time.min, tzinfo=UTC))
    if f.q and f.q.strip():
        like = f"%{f.q.strip()}%"
        stmt = stmt.where(or_(
            SecurityEvent.summary.ilike(like), SecurityEvent.event_type.ilike(like), SecurityEvent.client_ip.ilike(like),
            SecurityEvent.entity_id.ilike(like), SecurityEvent.entity_type.ilike(like), User.full_name.ilike(like),
            User.email.ilike(like),
        ))
    return stmt


def _row(event: SecurityEvent, actor_name: str | None, branches: dict[int, str]) -> dict:
    details = dict(event.details or {})
    changes = details.pop("changes", None)
    category = event.category or category_of_event(event.event_type)
    return {
        "id": event.id, "occurred_at": event.occurred_at, "category": category, "category_label": CATEGORIES.get(category, category),
        "event_type": event.event_type, "outcome": _outcome(event.outcome), "outcome_label": OUTCOME_LABELS.get(_outcome(event.outcome), event.outcome),
        "summary": event.summary or label_for(event.event_type) or event.event_type,
        "actor_id": event.actor_id or event.subject_id, "actor_name": actor_name,
        "actor_role": event.actor_role, "actor_role_label": ROLE_LABELS.get(event.actor_role or "", event.actor_role),
        "branch_id": event.branch_id, "branch_name": branches.get(event.branch_id) if event.branch_id else None,
        "entity_type": event.entity_type, "entity_id": event.entity_id, "client_ip": event.client_ip,
        "details": details, "changes": changes,
    }


def _branches(db: Session, company_id: int) -> dict[int, str]:
    return {b.id: b.name for b in db.scalars(select(Branch).where(Branch.company_id == company_id)).all()}


@router.get("")
def list_activity(
    response: Response, f: Filters = Depends(), page: Page = Depends(page_params), db: Session = Depends(get_db),
    user: User = Depends(require_admin_manager), company_id: int = Depends(get_company_id),
) -> list[dict]:
    stmt = _base(user, company_id, f)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    response.headers["X-Total-Count"] = str(total)
    stmt = stmt.order_by(SecurityEvent.id.desc())
    if page.limit is not None:
        stmt = stmt.limit(page.limit).offset(page.offset)
    branches = _branches(db, company_id)
    return [_row(e, name, branches) for e, name in db.execute(stmt).all()]


@router.get("/summary")
def activity_summary(
    f: Filters = Depends(), db: Session = Depends(get_db), user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> dict:
    """Counters for the period; every filter applies except the category, so the tiles stay comparable."""
    rows = db.execute(_base(user, company_id, f, with_category=False)).all()
    by_category: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    by_actor: dict[int, int] = {}
    for event, _name in rows:
        c = event.category or category_of_event(event.event_type)
        by_category[c] = by_category.get(c, 0) + 1
        by_outcome[_outcome(event.outcome)] = by_outcome.get(_outcome(event.outcome), 0) + 1
        who = event.actor_id or event.subject_id
        if who:
            by_actor[who] = by_actor.get(who, 0) + 1
    top = sorted(by_actor.items(), key=lambda x: -x[1])[:8]
    names = {u.id: u.full_name for u in db.scalars(select(User).where(User.id.in_([a for a, _ in top]))).all()} if top else {}
    return {
        "total": len(rows),
        "by_category": [{"category": c, "label": CATEGORIES.get(c, c), "count": n} for c, n in sorted(by_category.items(), key=lambda x: -x[1])],
        "by_outcome": [{"outcome": k, "label": OUTCOME_LABELS.get(k, k), "count": n} for k, n in by_outcome.items()],
        "top_actors": [{"actor_id": a, "name": names.get(a, "—"), "count": n} for a, n in top],
    }


@router.get("/filters")
def activity_filters(
    db: Session = Depends(get_db), user: User = Depends(require_admin_manager), company_id: int = Depends(get_company_id),
) -> dict:
    users = select(User).where(User.company_id == company_id).order_by(User.full_name)
    branches = select(Branch).where(Branch.company_id == company_id).order_by(Branch.name)
    if user.role == "manager":
        users = users.where(User.branch_id == user.branch_id)
        branches = branches.where(Branch.id == user.branch_id)
    return {
        "categories": [{"value": k, "label": v} for k, v in CATEGORIES.items() if k != "platform"],
        "outcomes": [{"value": k, "label": v} for k, v in OUTCOME_LABELS.items()],
        "roles": [{"value": k, "label": v} for k, v in ROLE_LABELS.items()],
        "users": [{"id": u.id, "name": u.full_name, "role": u.role.value if hasattr(u.role, "value") else u.role} for u in db.scalars(users).all()],
        "branches": [{"id": b.id, "name": b.name} for b in db.scalars(branches).all()],
    }


@router.get("/export")
def export_activity(
    f: Filters = Depends(), db: Session = Depends(get_db), user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> StreamingResponse:
    stmt = _base(user, company_id, f).order_by(SecurityEvent.id.desc()).limit(MAX_EXPORT)
    branches = _branches(db, company_id)
    out = io.StringIO()
    out.write("﻿")
    w = csv.writer(out)
    w.writerow(["Fecha", "Categoría", "Acción", "Resultado", "Usuario", "Rol", "Sucursal", "Entidad", "ID", "IP"])
    for e, name in db.execute(stmt).all():
        r = _row(e, name, branches)
        w.writerow([r["occurred_at"].isoformat(timespec="seconds"), r["category_label"], r["summary"], r["outcome_label"],
                    r["actor_name"] or "", r["actor_role_label"] or "", r["branch_name"] or "", r["entity_type"] or "",
                    r["entity_id"] or "", r["client_ip"] or ""])
    filename = f"actividad-{date.today().isoformat()}.csv"
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{event_id}")
def activity_detail(
    event_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> dict:
    hit = db.execute(_base(user, company_id, Filters(None, None, None, None, None, None, None, None, None, None, None)).where(SecurityEvent.id == event_id)).first()
    if hit is None:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado.")
    return _row(hit[0], hit[1], _branches(db, company_id))
