"""Credit origination use cases (T-006): application -> evaluation -> decision/approval -> formalization.

Rules of the road
* Tenant comes from the principal only; another tenant's ids are a 404.
* Every transition locks the application row ``FOR UPDATE`` first, then (approve/formalize) the product and the pinned
  version ``FOR SHARE`` (order application -> product -> version; T-005 uses product -> version, so no cycle).
  Terminal rows (decision, approval, formalization) are UNIQUE per application and immutable in the database.
* Repeating an equivalent command is a deterministic 200 replay (nothing new is written); a different command against a
  finished application is a 409. Reads never write.
* Nothing here moves money: no cash/bank movement, payment, balance or live loan is ever created.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BusinessRuleViolation, Conflict
from app.core.time import business_date, now_utc
from app.models.branch import Branch
from app.models.company import Company
from app.modules.credit import service as credit_service
from app.modules.credit.errors import RulesIntegrityFailed
from app.modules.credit.models import CreditProduct, CreditProductVersion
from app.modules.credit.rules import canonical_json, compute_rules_hash, parse_rules
from app.modules.customers.models import CustomerProfile
from app.modules.customers.service import visible_branch_ids
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, require
from app.modules.identity.errors import InvalidStateTransition, PermissionDenied, SelfEscalationDenied, TenantMismatch
from app.modules.identity.models import UserAccount, UserRoleAssignment
from app.modules.organization.models import Currency
from app.modules.origination.errors import (
    AlreadyDecided,
    ApplicationNotEditable,
    ApprovalLimitExceeded,
    ApprovalPolicyNotConfigured,
    BlockingConditionsPending,
    MakerCheckerViolation,
    OriginationValidationFailed,
    ProductNotAvailable,
    StaleApplicationVersion,
)
from app.modules.origination.models import (
    CreditApplication,
    CreditApplicationCondition,
    CreditApplicationDocumentLink,
    CreditApplicationEvaluation,
    CreditApplicationSubmission,
    CreditApproval,
    CreditApprovalLimit,
    CreditApprovalPolicy,
    CreditDecision,
    CreditFormalization,
)
from app.modules.origination.schemas import (
    ApplicationCreateIn,
    ApplicationPatchIn,
    ApproveIn,
    ConditionResolveIn,
    DocumentLinkIn,
    DocumentStatusIn,
    EvaluationIn,
    LimitIn,
    PolicyIn,
    RejectIn,
)

READ, CREATE, UPDATE_DRAFT, SUBMIT, EVALUATE, APPROVE, REJECT, CANCEL, FORMALIZE = (
    "credit.applications.read",
    "credit.applications.create",
    "credit.applications.update_draft",
    "credit.applications.submit",
    "credit.applications.evaluate",
    "credit.applications.approve",
    "credit.applications.reject",
    "credit.applications.cancel",
    "credit.applications.formalize",
)
MANAGE_POLICY, MANAGE_LIMITS = "credit.approval_policy.manage", "credit.approval_limits.manage"
CONTRACT_SCHEMA = "fastmoney.credit-contract.v1"


# --- small helpers ----------------------------------------------------------------------------------
def _amt(value: Decimal | None) -> str | None:
    """Plain decimal string at the column's scale (NUMERIC(20,4)), the same before and after a database round trip."""
    return None if value is None else format(value.quantize(Decimal("0.0001")), "f")


def _dec(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:  # pydantic already constrains the shape; keep the service honest too
        raise OriginationValidationFailed(
            details=[{"path": "amount", "code": "invalid_decimal", "message": value}]
        ) from None


def _audit(db: Session, actor: Principal, event: str, client_ip: str | None, **details) -> None:
    record_event(db, event, tenant_id=actor.tenant_id, actor_id=actor.user_id, client_ip=client_ip, details=details)


def _gate_tenant(actor: Principal) -> None:
    if actor.tenant_id is None:  # platform principals own no applications
        raise TenantMismatch()


def _allowed(actor: Principal, permission: str, app: CreditApplication) -> bool:
    branches = [app.origin_branch_id] + ([app.managing_branch_id] if app.managing_branch_id else [])
    return any(actor.allows(permission, tenant_id=actor.tenant_id, branch_id=b) for b in branches)


def _require_app(actor: Principal, permission: str, app: CreditApplication) -> None:
    _gate_tenant(actor)
    if not _allowed(actor, permission, app):
        raise PermissionDenied()


def _app(db: Session, actor: Principal, application_id: int, *, lock: bool = False) -> CreditApplication:
    _gate_tenant(actor)
    stmt = select(CreditApplication).where(
        CreditApplication.id == application_id, CreditApplication.tenant_id == actor.tenant_id
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    app = db.scalar(stmt)
    if app is None:
        raise TenantMismatch()
    return app


def _next_number(db: Session, tenant_id: int, name: str, prefix: str) -> str:
    value = db.execute(
        text(
            "INSERT INTO tenant_sequences (tenant_id, name, last_value) VALUES (:t, :n, 1) "
            "ON CONFLICT (tenant_id, name) DO UPDATE SET last_value = tenant_sequences.last_value + 1 "
            "RETURNING last_value"
        ),
        {"t": tenant_id, "n": name},
    ).scalar_one()
    return f"{prefix}{value:06d}"


def _branch(db: Session, actor: Principal, branch_id: int | None) -> Branch | None:
    if branch_id is None:
        return None
    branch = db.get(Branch, branch_id)
    if branch is None or branch.company_id != actor.tenant_id:
        raise TenantMismatch()
    if branch.status != "active":
        raise OriginationValidationFailed(
            details=[{"path": "branch", "code": "branch_inactive", "message": "La sucursal esta inactiva."}]
        )
    return branch


def _customer(db: Session, actor: Principal, customer_id: int) -> CustomerProfile:
    customer = db.get(CustomerProfile, customer_id)
    if customer is None or customer.tenant_id != actor.tenant_id:
        raise TenantMismatch()
    if customer.status == "inactive":  # whether a 'pending' customer may apply is BLOCKED_BY_SPEC (DF-02)
        raise OriginationValidationFailed(
            details=[{"path": "customer_id", "code": "customer_inactive", "message": "El cliente esta inactivo."}]
        )
    return customer


def _product(db: Session, actor: Principal, product_id: int, *, lock_share: bool = False) -> CreditProduct:
    stmt = select(CreditProduct).where(CreditProduct.id == product_id, CreditProduct.tenant_id == actor.tenant_id)
    if lock_share:
        stmt = stmt.with_for_update(read=True).execution_options(populate_existing=True)
    product = db.scalar(stmt)
    if product is None:
        raise TenantMismatch()
    return product


def _version(db: Session, actor: Principal, version_id: int, *, lock_share: bool = False) -> CreditProductVersion:
    stmt = select(CreditProductVersion).where(
        CreditProductVersion.id == version_id, CreditProductVersion.tenant_id == actor.tenant_id
    )
    if lock_share:
        stmt = stmt.with_for_update(read=True).execution_options(populate_existing=True)
    version = db.scalar(stmt)
    if version is None:
        raise TenantMismatch()
    return version


def _today(db: Session, actor: Principal) -> date:
    return business_date(tz=db.get(Company, actor.tenant_id).default_timezone)


def _effective_version(db: Session, actor: Principal, product: CreditProduct) -> CreditProductVersion:
    if product.status != "active":
        raise ProductNotAvailable("El producto no esta activo.")
    day = _today(db, actor)
    version = db.scalar(
        select(CreditProductVersion).where(
            CreditProductVersion.product_id == product.id,
            CreditProductVersion.tenant_id == actor.tenant_id,
            CreditProductVersion.status == "published",
            CreditProductVersion.effective_from <= day,
            or_(CreditProductVersion.effective_to.is_(None), CreditProductVersion.effective_to >= day),
        )
    )
    if version is None:
        raise ProductNotAvailable("El producto no tiene una version vigente.")
    return version


def _check_usable(db: Session, product: CreditProduct, version: CreditProductVersion) -> None:
    """In-flight rule (documented policy gap): an explicit withdrawal (retired version, inactive product) blocks;
    a version merely superseded by a newer one stays usable for applications already pinned to it."""
    if product.status != "active":
        raise ProductNotAvailable("El producto no esta activo.")
    if version.status != "published":
        raise ProductNotAvailable("La version del producto fue retirada.")
    if not credit_service.verify_version_integrity(db, version):
        raise RulesIntegrityFailed()


def _validate_terms(
    db: Session, version: CreditProductVersion, *, amount: Decimal, currency: str, term: int, frequency: str
) -> None:
    """Amount/currency/term/frequency against the pinned version's rules. Raises with EVERY issue."""
    issues: list[dict] = []
    rules = parse_rules(version.rules)
    limits = next((c for c in credit_service.version_currencies(db, version.id) if c["code"] == currency), None)
    if limits is None:
        issues.append(
            {"path": "currency_code", "code": "currency_not_allowed", "message": "Moneda no permitida por el producto."}
        )
    else:
        lo, hi = Decimal(limits["min_amount"]), Decimal(limits["max_amount"])
        if not lo <= amount <= hi:
            issues.append(
                {
                    "path": "amount",
                    "code": "amount_out_of_range",
                    "message": f"El monto debe estar entre {_amt(lo)} y {_amt(hi)}.",
                }
            )
        exponent = db.scalar(select(Currency.exponent).where(Currency.code == currency))
        if exponent is not None and amount != amount.quantize(Decimal(1).scaleb(-exponent)):
            issues.append(
                {"path": "amount", "code": "too_many_decimals", "message": f"{currency} admite {exponent} decimales."}
            )
    if amount <= 0:
        issues.append({"path": "amount", "code": "amount_not_positive", "message": "El monto debe ser positivo."})
    if not rules.term.min_periods <= term <= rules.term.max_periods:
        issues.append(
            {
                "path": "term",
                "code": "term_out_of_range",
                "message": f"El plazo debe estar entre {rules.term.min_periods} y {rules.term.max_periods} periodos.",
            }
        )
    if frequency != rules.frequency.code:
        issues.append(
            {
                "path": "frequency",
                "code": "frequency_incompatible",
                "message": f"El producto opera con frecuencia {rules.frequency.code}.",
            }
        )
    if issues:
        raise OriginationValidationFailed(details=issues)


def _request_dict(app: CreditApplication, version: CreditProductVersion) -> dict:
    return {
        "customer_id": app.customer_id,
        "product_id": app.product_id,
        "product_version_id": app.product_version_id,
        "rules_hash": version.rules_hash,
        "requested_amount": _amt(app.requested_amount),
        "currency_code": app.currency_code,
        "requested_term": app.requested_term,
        "requested_frequency": app.requested_frequency,
        "origin_branch_id": app.origin_branch_id,
        "managing_branch_id": app.managing_branch_id,
    }


# --- serialisation ----------------------------------------------------------------------------------
def _summary(app: CreditApplication) -> dict:
    return {
        "id": app.id,
        "application_number": app.application_number,
        "customer_id": app.customer_id,
        "product_id": app.product_id,
        "status": app.status,
        "requested_amount": _amt(app.requested_amount),
        "currency_code": app.currency_code,
        "requested_term": app.requested_term,
        "requested_frequency": app.requested_frequency,
        "origin_branch_id": app.origin_branch_id,
        "managing_branch_id": app.managing_branch_id,
        "created_at": app.created_at,
        "submitted_at": app.submitted_at,
    }


def _decision_out(db: Session, app_id: int) -> dict | None:
    decision = db.scalar(select(CreditDecision).where(CreditDecision.application_id == app_id))
    if decision is None:
        return None
    out = {
        "id": decision.id,
        "outcome": decision.outcome,
        "decided_by": decision.decided_by,
        "decided_at": decision.decided_at,
        "reason": decision.reason,
        "application_row_version": decision.application_row_version,
        "approval": None,
    }
    approval = db.scalar(select(CreditApproval).where(CreditApproval.decision_id == decision.id))
    if approval is not None:
        out["approval"] = {
            "id": approval.id,
            "approved_amount": _amt(approval.approved_amount),
            "requested_amount": _amt(approval.requested_amount),
            "currency_code": approval.currency_code,
            "approved_term": approval.approved_term,
            "approved_frequency": approval.approved_frequency,
            "product_version_id": approval.product_version_id,
            "rules_hash": approval.rules_hash,
            "authority": approval.authorization,
        }
    return out


def _condition_out(c: CreditApplicationCondition) -> dict:
    return {
        "id": c.id,
        "kind": c.kind,
        "description": c.description,
        "blocks_formalization": c.blocks_formalization,
        "status": c.status,
        "resolved_by": c.resolved_by,
        "resolved_at": c.resolved_at,
        "resolution_note": c.resolution_note,
    }


def _document_out(d: CreditApplicationDocumentLink) -> dict:
    return {
        "id": d.id,
        "requirement": d.requirement,
        "reference": d.reference,
        "status": d.status,
        "note": d.note,
        "created_by": d.created_by,
        "created_at": d.created_at,
    }


def _contract_hash(contract: dict) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256((CONTRACT_SCHEMA + "\n" + canonical_json(contract)).encode("utf-8")).hexdigest()


def _formalization_out(db: Session, f: CreditFormalization) -> dict:
    version = db.get(CreditProductVersion, f.product_version_id)
    verified = (
        _contract_hash(f.contract_snapshot) == f.contract_hash
        and f.contract_snapshot["product"]["rules_hash"] == f.rules_hash == version.rules_hash
        and credit_service.verify_version_integrity(db, version)
    )
    return {
        "id": f.id,
        "application_id": f.application_id,
        "reference": f.reference,
        "status": f.status,
        "approved_amount": _amt(f.approved_amount),
        "currency_code": f.currency_code,
        "term": f.term,
        "frequency": f.frequency,
        "origin_branch_id": f.origin_branch_id,
        "managing_branch_id": f.managing_branch_id,
        "product_version_id": f.product_version_id,
        "rules_hash": f.rules_hash,
        "contract_hash": f.contract_hash,
        "hash_verified": verified,
        "contract_snapshot": f.contract_snapshot,
        "formalized_by": f.formalized_by,
        "formalized_at": f.formalized_at,
    }


def _detail(db: Session, actor: Principal, app: CreditApplication) -> dict:
    submissions = db.scalars(
        select(CreditApplicationSubmission)
        .where(CreditApplicationSubmission.application_id == app.id)
        .order_by(CreditApplicationSubmission.submission_number)
    ).all()
    conditions = db.scalars(
        select(CreditApplicationCondition)
        .where(CreditApplicationCondition.application_id == app.id)
        .order_by(CreditApplicationCondition.id)
    ).all()
    documents = db.scalars(
        select(CreditApplicationDocumentLink)
        .where(CreditApplicationDocumentLink.application_id == app.id)
        .order_by(CreditApplicationDocumentLink.id)
    ).all()
    formalization = db.scalar(select(CreditFormalization).where(CreditFormalization.application_id == app.id))
    return {
        **_summary(app),
        "tenant_id": app.tenant_id,
        "product_version_id": app.product_version_id,
        "row_version": app.row_version,
        "created_by": app.created_by,
        "submitted_by": app.submitted_by,
        "submissions": [
            {
                "submission_number": s.submission_number,
                "submitted_by": s.submitted_by,
                "submitted_at": s.submitted_at,
                "request": s.request,
                "reopened_at": s.reopened_at,
                "reopen_reason": s.reopen_reason,
            }
            for s in submissions
        ],
        "decision": _decision_out(db, app.id),
        "conditions": [_condition_out(c) for c in conditions],
        "documents": [_document_out(d) for d in documents],
        "formalization": _formalization_out(db, formalization) if formalization else None,
        "cancellation": {
            "by": app.cancelled_by,
            "at": app.cancelled_at,
            "reason": app.cancellation_reason,
            "from_status": app.cancelled_from_status,
        }
        if app.status == "cancelled"
        else None,
    }


# --- application: create / read / edit ---------------------------------------------------------------
def create_application(db: Session, actor: Principal, body: ApplicationCreateIn, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    require(actor, CREATE, tenant_id=actor.tenant_id, branch_id=body.origin_branch_id)
    _branch(db, actor, body.origin_branch_id)
    _branch(db, actor, body.managing_branch_id)
    _customer(db, actor, body.customer_id)
    product = _product(db, actor, body.product_id)
    version = _effective_version(db, actor, product)
    amount = _dec(body.requested_amount)
    _validate_terms(
        db,
        version,
        amount=amount,
        currency=body.currency_code,
        term=body.requested_term,
        frequency=body.requested_frequency,
    )
    app = CreditApplication(
        tenant_id=actor.tenant_id,
        application_number=_next_number(db, actor.tenant_id, "credit_application", "SOL-"),
        customer_id=body.customer_id,
        product_id=product.id,
        product_version_id=version.id,
        requested_amount=amount,
        currency_code=body.currency_code,
        requested_term=body.requested_term,
        requested_frequency=body.requested_frequency,
        origin_branch_id=body.origin_branch_id,
        managing_branch_id=body.managing_branch_id,
        status="draft",
        created_by=actor.user_id,
    )
    db.add(app)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise OriginationValidationFailed(
            details=[
                {
                    "path": "currency_code",
                    "code": "currency_not_enabled",
                    "message": "Moneda no habilitada para la agencia.",
                }
            ]
        ) from None
    _audit(
        db,
        actor,
        "credit_application.created",
        client_ip,
        application_id=app.id,
        application_number=app.application_number,
        customer_id=app.customer_id,
        product_id=product.id,
        product_version_id=version.id,
        rules_digest=version.rules_hash,
        requested_amount=_amt(amount),
        currency_code=app.currency_code,
    )
    db.commit()
    return _detail(db, actor, app)


def list_applications(
    db: Session,
    actor: Principal,
    *,
    status: str | None,
    customer_id: int | None,
    product_id: int | None,
    limit: int,
    offset: int,
) -> list[dict]:
    _gate_tenant(actor)
    branches = visible_branch_ids(actor, READ)
    stmt = select(CreditApplication).where(CreditApplication.tenant_id == actor.tenant_id)
    if branches is not None:
        stmt = stmt.where(
            or_(CreditApplication.origin_branch_id.in_(branches), CreditApplication.managing_branch_id.in_(branches))
        )
    if status:
        stmt = stmt.where(CreditApplication.status == status)
    if customer_id:
        stmt = stmt.where(CreditApplication.customer_id == customer_id)
    if product_id:
        stmt = stmt.where(CreditApplication.product_id == product_id)
    rows = db.scalars(stmt.order_by(CreditApplication.id.desc()).limit(limit).offset(offset)).all()
    return [_summary(a) for a in rows]


def get_application(db: Session, actor: Principal, application_id: int) -> dict:
    app = _app(db, actor, application_id)
    _require_app(actor, READ, app)
    return _detail(db, actor, app)


def update_draft(
    db: Session, actor: Principal, application_id: int, body: ApplicationPatchIn, client_ip: str | None
) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, UPDATE_DRAFT, app)
    if app.status != "draft":
        raise ApplicationNotEditable()
    if app.row_version != body.row_version:
        raise StaleApplicationVersion()
    fields = body.model_fields_set - {"row_version"}
    before = _summary(app)
    if "origin_branch_id" in fields and body.origin_branch_id is not None:
        _branch(db, actor, body.origin_branch_id)
        app.origin_branch_id = body.origin_branch_id
    if "managing_branch_id" in fields:
        _branch(db, actor, body.managing_branch_id)
        app.managing_branch_id = body.managing_branch_id
    product = _product(db, actor, body.product_id if body.product_id is not None else app.product_id)
    if "product_id" in fields and body.product_id is not None:
        version = _effective_version(db, actor, product)  # re-pins the version in force today
        app.product_id, app.product_version_id = product.id, version.id
    else:
        version = _version(db, actor, app.product_version_id)
    if "requested_amount" in fields and body.requested_amount is not None:
        app.requested_amount = _dec(body.requested_amount)
    for name in ("currency_code", "requested_term", "requested_frequency"):
        if name in fields and getattr(body, name) is not None:
            setattr(app, name, getattr(body, name))
    _validate_terms(
        db,
        version,
        amount=app.requested_amount,
        currency=app.currency_code,
        term=app.requested_term,
        frequency=app.requested_frequency,
    )
    app.row_version += 1
    app.updated_at = now_utc()
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise OriginationValidationFailed(
            details=[
                {
                    "path": "currency_code",
                    "code": "currency_not_enabled",
                    "message": "Moneda no habilitada para la agencia.",
                }
            ]
        ) from None
    _audit(
        db,
        actor,
        "credit_application.updated",
        client_ip,
        application_id=app.id,
        changed_fields=sorted(fields),
        before={k: before[k] for k in ("requested_amount", "currency_code", "requested_term", "requested_frequency")},
        after={
            "requested_amount": _amt(app.requested_amount),
            "currency_code": app.currency_code,
            "requested_term": app.requested_term,
            "requested_frequency": app.requested_frequency,
        },
    )
    db.commit()
    return _detail(db, actor, app)


# --- submit / reopen / review -------------------------------------------------------------------------
def submit(db: Session, actor: Principal, application_id: int, client_ip: str | None) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, SUBMIT, app)
    if app.status == "submitted":  # equivalent repeat: a deterministic replay, nothing new is written
        return {**_detail(db, actor, app), "replayed": True}
    if app.status != "draft":
        raise InvalidStateTransition(f"Una solicitud en estado {app.status} no se puede enviar.")
    product = _product(db, actor, app.product_id)
    version = _version(db, actor, app.product_version_id)
    _check_usable(db, product, version)
    if _effective_version(db, actor, product).id != version.id:
        raise ProductNotAvailable(
            "La version fijada ya no es la vigente: edita el borrador para fijar la nueva version."
        )
    _customer(db, actor, app.customer_id)
    _branch(db, actor, app.origin_branch_id)
    _branch(db, actor, app.managing_branch_id)
    _validate_terms(
        db,
        version,
        amount=app.requested_amount,
        currency=app.currency_code,
        term=app.requested_term,
        frequency=app.requested_frequency,
    )
    number = app.submission_count + 1
    db.add(
        CreditApplicationSubmission(
            tenant_id=app.tenant_id,
            application_id=app.id,
            submission_number=number,
            request=_request_dict(app, version),
            submitted_by=actor.user_id,
        )
    )
    app.submission_count = number
    app.status, app.submitted_at, app.submitted_by = "submitted", now_utc(), actor.user_id
    app.row_version += 1
    _audit(
        db,
        actor,
        "credit_application.submitted",
        client_ip,
        application_id=app.id,
        submission_number=number,
        product_version_id=version.id,
        rules_digest=version.rules_hash,
        before={"status": "draft"},
        after={"status": "submitted"},
    )
    db.commit()
    return {**_detail(db, actor, app), "replayed": False}


def reopen(db: Session, actor: Principal, application_id: int, reason: str, client_ip: str | None) -> dict:
    """Formal way to correct a submitted request: back to draft with a reason; the submitted request is kept."""
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, UPDATE_DRAFT, app)
    if app.status != "submitted":
        raise InvalidStateTransition("Solo una solicitud enviada (sin revision iniciada) se puede reabrir.")
    sub = db.scalar(
        select(CreditApplicationSubmission).where(
            CreditApplicationSubmission.application_id == app.id,
            CreditApplicationSubmission.submission_number == app.submission_count,
        )
    )
    sub.reopened_by, sub.reopened_at, sub.reopen_reason = actor.user_id, now_utc(), reason
    app.status = "draft"
    app.row_version += 1
    _audit(
        db,
        actor,
        "credit_application.reopened",
        client_ip,
        application_id=app.id,
        submission_number=app.submission_count,
        reason=reason,
        before={"status": "submitted"},
        after={"status": "draft"},
    )
    db.commit()
    return _detail(db, actor, app)


def start_review(db: Session, actor: Principal, application_id: int, client_ip: str | None) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, EVALUATE, app)
    if app.status == "under_review":
        return {**_detail(db, actor, app), "replayed": True}
    if app.status != "submitted":
        raise InvalidStateTransition(f"Una solicitud en estado {app.status} no pasa a revision.")
    app.status = "under_review"
    app.row_version += 1
    _audit(
        db,
        actor,
        "credit_application.review_started",
        client_ip,
        application_id=app.id,
        before={"status": "submitted"},
        after={"status": "under_review"},
    )
    db.commit()
    return {**_detail(db, actor, app), "replayed": False}


def add_evaluation(
    db: Session, actor: Principal, application_id: int, body: EvaluationIn, client_ip: str | None
) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, EVALUATE, app)
    if app.status != "under_review":
        raise InvalidStateTransition("Solo se evalua una solicitud en revision.")
    data = body.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    row = CreditApplicationEvaluation(
        tenant_id=app.tenant_id, application_id=app.id, evaluator_id=actor.user_id, data=data
    )
    db.add(row)
    db.flush()
    # privacy: the audit row names the evaluation and which sections it had, never the values
    _audit(
        db,
        actor,
        "credit_application.evaluated",
        client_ip,
        application_id=app.id,
        evaluation_id=row.id,
        sections=sorted(data),
        recommendation=data.get("recommendation"),
    )
    db.commit()
    return {"id": row.id, "application_id": app.id, "evaluator_id": actor.user_id, "created_at": row.created_at}


def list_evaluations(db: Session, actor: Principal, application_id: int) -> list[dict]:
    app = _app(db, actor, application_id)
    _require_app(actor, EVALUATE, app)  # evaluation content is sensitive: not part of a plain read
    rows = db.scalars(
        select(CreditApplicationEvaluation)
        .where(CreditApplicationEvaluation.application_id == app.id)
        .order_by(CreditApplicationEvaluation.id)
    ).all()
    return [{"id": r.id, "evaluator_id": r.evaluator_id, "created_at": r.created_at, "data": r.data} for r in rows]


# --- approval policy & limits ------------------------------------------------------------------------
def _policy_for(db: Session, tenant_id: int, product_id: int) -> tuple[CreditApprovalPolicy, str] | None:
    rows = db.scalars(
        select(CreditApprovalPolicy).where(
            CreditApprovalPolicy.tenant_id == tenant_id,
            or_(CreditApprovalPolicy.product_id == product_id, CreditApprovalPolicy.product_id.is_(None)),
        )
    ).all()
    specific = next((p for p in rows if p.product_id == product_id), None)
    if specific:
        return specific, "product"
    default = next((p for p in rows if p.product_id is None), None)
    return (default, "tenant") if default else None


def _matching_limit(db: Session, actor: Principal, app: CreditApplication) -> CreditApprovalLimit | None:
    role_ids = set(
        db.scalars(
            select(UserRoleAssignment.role_id).where(
                UserRoleAssignment.user_id == actor.user_id, UserRoleAssignment.revoked_at.is_(None)
            )
        )
    )
    branch = app.managing_branch_id or app.origin_branch_id  # the branch of operation
    rows = db.scalars(
        select(CreditApprovalLimit).where(
            CreditApprovalLimit.tenant_id == actor.tenant_id,
            CreditApprovalLimit.revoked_at.is_(None),
            CreditApprovalLimit.operation == "approve",
            CreditApprovalLimit.currency_code == app.currency_code,
            or_(CreditApprovalLimit.user_id == actor.user_id, CreditApprovalLimit.role_id.in_(role_ids)),
            or_(CreditApprovalLimit.product_id.is_(None), CreditApprovalLimit.product_id == app.product_id),
            or_(CreditApprovalLimit.branch_id.is_(None), CreditApprovalLimit.branch_id == branch),
        )
    ).all()
    return max(rows, key=lambda r: r.max_amount, default=None)  # any applicable grant may authorise: the widest wins


def set_policy(db: Session, actor: Principal, body: PolicyIn, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    require(actor, MANAGE_POLICY, tenant_id=actor.tenant_id)
    if body.product_id is not None:
        _product(db, actor, body.product_id)
    existing = db.scalar(
        select(CreditApprovalPolicy)
        .where(
            CreditApprovalPolicy.tenant_id == actor.tenant_id,
            CreditApprovalPolicy.product_id == body.product_id
            if body.product_id is not None
            else CreditApprovalPolicy.product_id.is_(None),
        )
        .with_for_update()
    )
    flags = {
        "maker_checker_required": body.maker_checker_required,
        "limits_enforced": body.limits_enforced,
        "approved_may_exceed_requested": body.approved_may_exceed_requested,
        "evaluation_required": body.evaluation_required,
    }
    before = None
    if existing is None:
        existing = CreditApprovalPolicy(
            tenant_id=actor.tenant_id, product_id=body.product_id, updated_by=actor.user_id, **flags
        )
        db.add(existing)
    else:
        before = {k: getattr(existing, k) for k in flags}
        for k, v in flags.items():
            setattr(existing, k, v)
        existing.updated_by, existing.updated_at = actor.user_id, now_utc()
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("Otra actualizacion concurrente de la politica gano; reintenta.") from None
    _audit(
        db,
        actor,
        "credit_approval_policy.set",
        client_ip,
        policy_id=existing.id,
        product_id=body.product_id,
        before=before,
        after=flags,
    )
    db.commit()
    return {"id": existing.id, "product_id": existing.product_id, **flags}


def list_policies(db: Session, actor: Principal) -> list[dict]:
    _gate_tenant(actor)
    require(actor, READ, tenant_id=actor.tenant_id)
    rows = db.scalars(
        select(CreditApprovalPolicy)
        .where(CreditApprovalPolicy.tenant_id == actor.tenant_id)
        .order_by(CreditApprovalPolicy.id)
    ).all()
    return [
        {
            "id": p.id,
            "product_id": p.product_id,
            "maker_checker_required": p.maker_checker_required,
            "limits_enforced": p.limits_enforced,
            "approved_may_exceed_requested": p.approved_may_exceed_requested,
            "evaluation_required": p.evaluation_required,
        }
        for p in rows
    ]


def _limit_out(row: CreditApprovalLimit) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "role_id": row.role_id,
        "currency_code": row.currency_code,
        "max_amount": _amt(row.max_amount),
        "product_id": row.product_id,
        "branch_id": row.branch_id,
        "operation": row.operation,
        "revoked_at": row.revoked_at,
    }


def create_limit(db: Session, actor: Principal, body: LimitIn, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    require(actor, MANAGE_LIMITS, tenant_id=actor.tenant_id)
    if body.user_id is not None:
        if body.user_id == actor.user_id:
            raise SelfEscalationDenied()  # nobody raises their own authorisation limit
        user = db.get(UserAccount, body.user_id)
        if user is None or user.company_id != actor.tenant_id:
            raise TenantMismatch()
    if body.role_id is not None:
        from app.modules.identity.models import Role

        role = db.get(Role, body.role_id)
        if role is None or role.tenant_id != actor.tenant_id:
            raise TenantMismatch()
    if body.product_id is not None:
        _product(db, actor, body.product_id)
    _branch(db, actor, body.branch_id)
    amount = _dec(body.max_amount)
    row = CreditApprovalLimit(
        tenant_id=actor.tenant_id,
        user_id=body.user_id,
        role_id=body.role_id,
        currency_code=body.currency_code,
        max_amount=amount,
        product_id=body.product_id,
        branch_id=body.branch_id,
        created_by=actor.user_id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise BusinessRuleViolation("Moneda no habilitada para la agencia o monto invalido.") from None
    _audit(
        db,
        actor,
        "credit_approval_limit.created",
        client_ip,
        limit_id=row.id,
        user_id=body.user_id,
        role_id=body.role_id,
        currency_code=body.currency_code,
        max_amount=_amt(amount),
        product_id=body.product_id,
        branch_id=body.branch_id,
    )
    db.commit()
    return _limit_out(row)


def list_limits(db: Session, actor: Principal) -> list[dict]:
    _gate_tenant(actor)
    require(actor, MANAGE_LIMITS, tenant_id=actor.tenant_id)
    rows = db.scalars(
        select(CreditApprovalLimit)
        .where(CreditApprovalLimit.tenant_id == actor.tenant_id)
        .order_by(CreditApprovalLimit.id)
    ).all()
    return [_limit_out(r) for r in rows]


def revoke_limit(db: Session, actor: Principal, limit_id: int, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    require(actor, MANAGE_LIMITS, tenant_id=actor.tenant_id)
    row = db.scalar(
        select(CreditApprovalLimit)
        .where(CreditApprovalLimit.id == limit_id, CreditApprovalLimit.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    if row is None:
        raise TenantMismatch()
    if row.revoked_at is not None:
        raise InvalidStateTransition("El limite ya esta revocado.")
    row.revoked_at, row.revoked_by = now_utc(), actor.user_id
    _audit(db, actor, "credit_approval_limit.revoked", client_ip, limit_id=row.id)
    db.commit()
    return _limit_out(row)


# --- decision: approve / reject / cancel -------------------------------------------------------------
def _same_approval(db: Session, decision: CreditDecision, body: ApproveIn) -> bool:
    approval = db.scalar(select(CreditApproval).where(CreditApproval.decision_id == decision.id))
    if approval is None or decision.application_row_version != body.row_version:
        return False
    conditions = db.scalars(
        select(CreditApplicationCondition)
        .where(CreditApplicationCondition.decision_id == decision.id)
        .order_by(CreditApplicationCondition.id)
    ).all()
    mine = [(c.kind, c.description, c.blocks_formalization) for c in body.conditions]
    return (
        approval.approved_amount == _dec(body.approved_amount)
        and approval.approved_term == body.approved_term
        and approval.approved_frequency == body.approved_frequency
        and [(c.kind, c.description, c.blocks_formalization) for c in conditions] == mine
    )


def approve(db: Session, actor: Principal, application_id: int, body: ApproveIn, client_ip: str | None) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, APPROVE, app)
    decision = db.scalar(select(CreditDecision).where(CreditDecision.application_id == app.id))
    if decision is not None:  # already decided: an equivalent approval is a replay, anything else a conflict
        if decision.outcome == "approved" and _same_approval(db, decision, body):
            return {**_detail(db, actor, app), "replayed": True}
        raise AlreadyDecided()
    if app.status != "under_review":
        raise InvalidStateTransition(f"Una solicitud en estado {app.status} no se puede aprobar.")
    if app.row_version != body.row_version:
        raise StaleApplicationVersion()
    product = _product(db, actor, app.product_id, lock_share=True)
    version = _version(db, actor, app.product_version_id, lock_share=True)
    _check_usable(db, product, version)  # explicit withdrawal blocks; integrity (rules_hash) is re-verified

    found = _policy_for(db, actor.tenant_id, app.product_id)
    if found is None:
        raise ApprovalPolicyNotConfigured()
    policy, policy_scope = found
    if policy.evaluation_required and not db.scalar(
        select(CreditApplicationEvaluation.id).where(CreditApplicationEvaluation.application_id == app.id).limit(1)
    ):
        raise BusinessRuleViolation("La politica exige al menos una evaluacion registrada antes de aprobar.")

    amount = _dec(body.approved_amount)
    _validate_terms(
        db,
        version,
        amount=amount,
        currency=app.currency_code,
        term=body.approved_term,
        frequency=body.approved_frequency,
    )
    if amount > app.requested_amount and not policy.approved_may_exceed_requested:
        raise OriginationValidationFailed(
            details=[
                {
                    "path": "approved_amount",
                    "code": "approved_exceeds_requested",
                    "message": "La politica no permite aprobar mas de lo solicitado.",
                }
            ]
        )
    makers = sorted({app.created_by, app.submitted_by} - {None})
    if policy.maker_checker_required and actor.user_id in makers:
        raise MakerCheckerViolation()
    limit = _matching_limit(db, actor, app) if policy.limits_enforced else None
    if policy.limits_enforced and (limit is None or limit.max_amount < amount):
        raise ApprovalLimitExceeded()

    decision = CreditDecision(
        tenant_id=app.tenant_id,
        application_id=app.id,
        outcome="approved",
        decided_by=actor.user_id,
        reason=body.reason,
        application_row_version=app.row_version,
    )
    db.add(decision)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise AlreadyDecided() from None
    approval = CreditApproval(
        tenant_id=app.tenant_id,
        decision_id=decision.id,
        application_id=app.id,
        approved_amount=amount,
        currency_code=app.currency_code,
        approved_term=body.approved_term,
        approved_frequency=body.approved_frequency,
        product_id=app.product_id,
        product_version_id=version.id,
        rules_hash=version.rules_hash,
        requested_amount=app.requested_amount,
        authorization={
            "policy": {
                "id": policy.id,
                "scope": policy_scope,
                "maker_checker_required": policy.maker_checker_required,
                "limits_enforced": policy.limits_enforced,
                "approved_may_exceed_requested": policy.approved_may_exceed_requested,
                "evaluation_required": policy.evaluation_required,
            },
            "maker_checker": {"makers": makers, "checker": actor.user_id},
            "limit": None
            if limit is None
            else {"id": limit.id, "max_amount": _amt(limit.max_amount), "subject": "user" if limit.user_id else "role"},
        },
    )
    db.add(approval)
    for c in body.conditions:
        db.add(
            CreditApplicationCondition(
                tenant_id=app.tenant_id,
                application_id=app.id,
                decision_id=decision.id,
                kind=c.kind,
                description=c.description,
                blocks_formalization=c.blocks_formalization,
            )
        )
    before = {"status": app.status, "requested_amount": _amt(app.requested_amount)}
    app.status = "approved"
    app.row_version += 1
    db.flush()
    _audit(
        db,
        actor,
        "credit_application.approved",
        client_ip,
        application_id=app.id,
        decision_id=decision.id,
        approval_id=approval.id,
        product_version_id=version.id,
        rules_digest=version.rules_hash,
        before=before,
        after={
            "status": "approved",
            "approved_amount": _amt(amount),
            "approved_term": body.approved_term,
            "approved_frequency": body.approved_frequency,
        },
        conditions=len(body.conditions),
        reason=body.reason,
        policy_scope=policy_scope,
        maker_checker_required=policy.maker_checker_required,
        limit_id=limit.id if limit else None,
    )
    db.commit()
    return {**_detail(db, actor, app), "replayed": False}


def reject(db: Session, actor: Principal, application_id: int, body: RejectIn, client_ip: str | None) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, REJECT, app)
    decision = db.scalar(select(CreditDecision).where(CreditDecision.application_id == app.id))
    if decision is not None:
        if (
            decision.outcome == "rejected"
            and decision.reason == body.reason
            and decision.application_row_version == body.row_version
        ):
            return {**_detail(db, actor, app), "replayed": True}
        raise AlreadyDecided()
    if app.status != "under_review":
        raise InvalidStateTransition(f"Una solicitud en estado {app.status} no se puede rechazar.")
    if app.row_version != body.row_version:
        raise StaleApplicationVersion()
    decision = CreditDecision(
        tenant_id=app.tenant_id,
        application_id=app.id,
        outcome="rejected",
        decided_by=actor.user_id,
        reason=body.reason,
        application_row_version=app.row_version,
    )
    db.add(decision)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise AlreadyDecided() from None
    app.status = "rejected"
    app.row_version += 1
    _audit(
        db,
        actor,
        "credit_application.rejected",
        client_ip,
        application_id=app.id,
        decision_id=decision.id,
        reason=body.reason,
        before={"status": "under_review"},
        after={"status": "rejected"},
    )
    db.commit()
    return {**_detail(db, actor, app), "replayed": False}


def cancel(db: Session, actor: Principal, application_id: int, reason: str, client_ip: str | None) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, CANCEL, app)
    if app.status == "cancelled":
        return {**_detail(db, actor, app), "replayed": True}
    if app.status not in ("draft", "submitted", "under_review", "approved"):
        raise InvalidStateTransition(f"Una solicitud en estado {app.status} no se puede cancelar.")
    previous = app.status
    app.status = "cancelled"
    app.cancelled_by, app.cancelled_at = actor.user_id, now_utc()
    app.cancellation_reason, app.cancelled_from_status = reason, previous
    app.row_version += 1
    _audit(
        db,
        actor,
        "credit_application.cancelled",
        client_ip,
        application_id=app.id,
        reason=reason,
        before={"status": previous},
        after={"status": "cancelled"},
    )
    db.commit()
    return {**_detail(db, actor, app), "replayed": False}


# --- conditions & document links ---------------------------------------------------------------------
def resolve_condition(
    db: Session,
    actor: Principal,
    application_id: int,
    condition_id: int,
    body: ConditionResolveIn,
    client_ip: str | None,
) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, EVALUATE, app)
    cond = db.scalar(
        select(CreditApplicationCondition).where(
            CreditApplicationCondition.id == condition_id,
            CreditApplicationCondition.application_id == app.id,
            CreditApplicationCondition.tenant_id == actor.tenant_id,
        )
    )
    if cond is None:
        raise TenantMismatch()
    if app.status != "approved":
        raise InvalidStateTransition(
            "Las condiciones se resuelven mientras la solicitud esta aprobada (antes de formalizar)."
        )
    if cond.status != "pending":
        if cond.status == body.status:
            return _condition_out(cond)  # equivalent repeat
        raise InvalidStateTransition("La condicion ya fue resuelta.")
    cond.status, cond.resolved_by, cond.resolved_at, cond.resolution_note = (
        body.status,
        actor.user_id,
        now_utc(),
        body.note,
    )
    _audit(
        db,
        actor,
        "credit_application.condition_resolved",
        client_ip,
        application_id=app.id,
        condition_id=cond.id,
        before={"status": "pending"},
        after={"status": body.status},
    )
    db.commit()
    return _condition_out(cond)


def add_document_link(
    db: Session, actor: Principal, application_id: int, body: DocumentLinkIn, client_ip: str | None
) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _gate_tenant(actor)
    if app.status == "draft":
        _require_app(actor, UPDATE_DRAFT, app)
    elif app.status in ("submitted", "under_review", "approved"):
        _require_app(actor, EVALUATE, app)
    else:
        raise InvalidStateTransition("La solicitud ya no admite nuevos requisitos.")
    row = CreditApplicationDocumentLink(
        tenant_id=app.tenant_id,
        application_id=app.id,
        requirement=body.requirement,
        reference=body.reference,
        note=body.note,
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    _audit(
        db,
        actor,
        "credit_application.document_linked",
        client_ip,
        application_id=app.id,
        document_link_id=row.id,
        requirement=body.requirement,
    )
    db.commit()
    return _document_out(row)


def set_document_status(
    db: Session, actor: Principal, application_id: int, link_id: int, body: DocumentStatusIn, client_ip: str | None
) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, EVALUATE, app)
    row = db.scalar(
        select(CreditApplicationDocumentLink).where(
            CreditApplicationDocumentLink.id == link_id,
            CreditApplicationDocumentLink.application_id == app.id,
            CreditApplicationDocumentLink.tenant_id == actor.tenant_id,
        )
    )
    if row is None:
        raise TenantMismatch()
    if app.status in ("formalized", "cancelled", "rejected"):
        raise InvalidStateTransition("La solicitud ya no admite cambios de requisitos.")
    before = row.status
    row.status, row.note, row.updated_by, row.updated_at = body.status, body.note or row.note, actor.user_id, now_utc()
    _audit(
        db,
        actor,
        "credit_application.document_status_changed",
        client_ip,
        application_id=app.id,
        document_link_id=row.id,
        before={"status": before},
        after={"status": body.status},
    )
    db.commit()
    return _document_out(row)


# --- formalization ------------------------------------------------------------------------------------
def formalize(db: Session, actor: Principal, application_id: int, client_ip: str | None) -> dict:
    app = _app(db, actor, application_id, lock=True)
    _require_app(actor, FORMALIZE, app)
    existing = db.scalar(select(CreditFormalization).where(CreditFormalization.application_id == app.id))
    if existing is not None:  # one formalization per application: repeating returns it, nothing new is written
        return {**_formalization_out(db, existing), "replayed": True}
    if app.status != "approved":
        raise InvalidStateTransition(f"Una solicitud en estado {app.status} no se puede formalizar.")
    decision = db.scalar(select(CreditDecision).where(CreditDecision.application_id == app.id))
    approval = db.scalar(select(CreditApproval).where(CreditApproval.application_id == app.id))
    if decision is None or approval is None or decision.outcome != "approved":
        raise InvalidStateTransition("No existe una aprobacion valida para formalizar.")
    product = _product(db, actor, approval.product_id, lock_share=True)
    version = _version(db, actor, approval.product_version_id, lock_share=True)
    _check_usable(db, product, version)  # published (not retired), product active, T-005 integrity re-verified
    if version.rules_hash != approval.rules_hash:
        raise RulesIntegrityFailed()
    _validate_terms(
        db,
        version,
        amount=approval.approved_amount,
        currency=approval.currency_code,
        term=approval.approved_term,
        frequency=approval.approved_frequency,
    )
    conditions = db.scalars(
        select(CreditApplicationCondition)
        .where(CreditApplicationCondition.application_id == app.id)
        .order_by(CreditApplicationCondition.id)
    ).all()
    blocking = [c for c in conditions if c.blocks_formalization and c.status == "pending"]
    if blocking:
        raise BlockingConditionsPending(details=[_condition_out(c) for c in blocking])

    reference = _next_number(db, app.tenant_id, "credit_formalization", "FRM-")
    now = now_utc()
    contract = {
        "contract_version": 1,
        "tenant_id": app.tenant_id,
        "reference": reference,
        "application": {
            "id": app.id,
            "number": app.application_number,
            "customer_id": app.customer_id,
            "requested_amount": _amt(approval.requested_amount),
            "requested_term": app.requested_term,
            "requested_frequency": app.requested_frequency,
        },
        "approved": {
            "amount": _amt(approval.approved_amount),
            "currency_code": approval.currency_code,
            "term_periods": approval.approved_term,
            "frequency": approval.approved_frequency,
        },
        "branches": {"origin_branch_id": app.origin_branch_id, "managing_branch_id": app.managing_branch_id},
        "conditions": [
            {
                "kind": c.kind,
                "description": c.description,
                "blocks_formalization": c.blocks_formalization,
                "status": c.status,
            }
            for c in conditions
        ],
        "approval": {
            "decision_id": decision.id,
            "approval_id": approval.id,
            "approved_by": decision.decided_by,
            "approved_at": decision.decided_at.isoformat(),
        },
        "product": {
            "product_id": product.id,
            "product_version_id": version.id,
            "version_number": version.version_number,
            "rules_hash": version.rules_hash,
            "snapshot": version.snapshot,
        },
        "formalized_by": actor.user_id,
        "formalized_at": now.isoformat(),
    }
    row = CreditFormalization(
        tenant_id=app.tenant_id,
        application_id=app.id,
        approval_id=approval.id,
        reference=reference,
        status="ready_for_disbursement",
        approved_amount=approval.approved_amount,
        currency_code=approval.currency_code,
        term=approval.approved_term,
        frequency=approval.approved_frequency,
        origin_branch_id=app.origin_branch_id,
        managing_branch_id=app.managing_branch_id,
        product_id=product.id,
        product_version_id=version.id,
        rules_hash=version.rules_hash,
        contract_snapshot=contract,
        contract_hash=_contract_hash(contract),
        formalized_by=actor.user_id,
        formalized_at=now,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("La solicitud ya fue formalizada por otra operacion.") from None
    app.status = "formalized"
    app.row_version += 1
    _audit(
        db,
        actor,
        "credit_application.formalized",
        client_ip,
        application_id=app.id,
        formalization_id=row.id,
        reference=reference,
        product_version_id=version.id,
        rules_digest=version.rules_hash,
        contract_digest=row.contract_hash,
        before={"status": "approved"},
        after={"status": "formalized", "formalization_status": "ready_for_disbursement"},
        approved_amount=_amt(approval.approved_amount),
        currency_code=approval.currency_code,
    )
    db.commit()
    return {**_formalization_out(db, row), "replayed": False}


def get_formalization(db: Session, actor: Principal, application_id: int) -> dict:
    app = _app(db, actor, application_id)
    _require_app(actor, READ, app)
    row = db.scalar(select(CreditFormalization).where(CreditFormalization.application_id == app.id))
    if row is None:
        raise TenantMismatch("La solicitud no esta formalizada.")
    return _formalization_out(db, row)


def verify_formalization_contract(db: Session, f: CreditFormalization) -> bool:
    """Integrity of a formalized contract WITHOUT consulting the live product: the frozen contract, its hash, the T-005
    snapshot it embeds (hashed again from its own content) and the approved terms must all agree (T-007 uses this)."""
    approval = db.get(CreditApproval, f.approval_id)
    try:
        contract = f.contract_snapshot
        product = contract["product"]
        snap = product["snapshot"]
        return (
            approval is not None
            and approval.approved_amount == f.approved_amount
            and approval.currency_code == f.currency_code
            and approval.approved_term == f.term
            and approval.approved_frequency == f.frequency
            and _contract_hash(contract) == f.contract_hash
            and product["rules_hash"] == f.rules_hash == snap["rules_hash"]
            and compute_rules_hash(snap["rules"], snap["currencies"]) == f.rules_hash
            and contract["tenant_id"] == f.tenant_id
            and contract["approved"]["amount"] == _amt(f.approved_amount)
            and contract["approved"]["currency_code"] == f.currency_code
            and contract["approved"]["term_periods"] == f.term
            and contract["approved"]["frequency"] == f.frequency
            and product["product_version_id"] == f.product_version_id
        )
    except (KeyError, TypeError):
        return False
