"""Credit product use cases (T-005). Every function resolves the tenant from the principal; another tenant's ids are
a 404. Reads never write. Concurrency: version numbering and publication lock the product row (``FOR UPDATE``).
"""

import json
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BusinessRuleViolation, Conflict
from app.core.time import business_date, get_zone, now_utc
from app.models.company import Company
from app.modules.credit import engine
from app.modules.credit.errors import (
    ProductValidationFailed,
    RowVersionConflict,
    RulesIntegrityFailed,
    SimulationRejected,
    VersionImmutable,
)
from app.modules.credit.models import CreditProduct, CreditProductCurrency, CreditProductVersion
from app.modules.credit.rules import _canon, compute_rules_hash, parse_rules
from app.modules.credit.schemas import (
    ProductCreateIn,
    PublishIn,
    SimulateIn,
    VersionCreateIn,
    VersionUpdateIn,
)
from app.modules.credit.validation import Validation, validate_rules
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, require
from app.modules.identity.errors import InvalidStateTransition, TenantMismatch
from app.modules.organization.models import Currency, TenantCurrency

READ, CREATE, UPDATE_DRAFT, PUBLISH, DEACTIVATE = (
    "credit.products.read",
    "credit.products.create",
    "credit.products.update_draft",
    "credit.products.publish",
    "credit.products.deactivate",
)


# --- scope helpers ----------------------------------------------------------------------------------
def _gate(actor: Principal, permission: str) -> None:
    require(actor, permission, tenant_id=actor.tenant_id)
    if actor.tenant_id is None:  # platform principals have no tenant products
        raise TenantMismatch()


def _tenant(db: Session, actor: Principal) -> Company:
    tenant = db.get(Company, actor.tenant_id)
    if tenant is None:
        raise TenantMismatch()
    return tenant


def _product(db: Session, actor: Principal, product_id: int, *, lock: bool = False) -> CreditProduct:
    stmt = select(CreditProduct).where(CreditProduct.id == product_id, CreditProduct.tenant_id == actor.tenant_id)
    product = db.scalar(stmt.with_for_update() if lock else stmt)
    if product is None:
        raise TenantMismatch()
    return product


def _version(db: Session, actor: Principal, product_id: int, version_id: int) -> CreditProductVersion:
    version = db.scalar(
        select(CreditProductVersion).where(
            CreditProductVersion.id == version_id,
            CreditProductVersion.product_id == product_id,
            CreditProductVersion.tenant_id == actor.tenant_id,
        )
    )
    if version is None:
        raise TenantMismatch()
    return version


def _currency_rows(db: Session, version_id: int) -> list[dict]:
    rows = db.scalars(
        select(CreditProductCurrency)
        .where(CreditProductCurrency.version_id == version_id)
        .order_by(CreditProductCurrency.currency_code)
    )
    return [
        {"code": r.currency_code, "min_amount": _plain(r.min_amount), "max_amount": _plain(r.max_amount)} for r in rows
    ]


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _enabled_currencies(db: Session, tenant_id: int) -> dict[str, int]:
    rows = db.execute(
        select(Currency.code, Currency.exponent)
        .join(TenantCurrency, TenantCurrency.currency_code == Currency.code)
        .where(TenantCurrency.tenant_id == tenant_id, TenantCurrency.disabled_at.is_(None), Currency.is_active)
    )
    return {code: exp for code, exp in rows}


def _summary(version: CreditProductVersion) -> dict:
    return {
        "id": version.id,
        "version_number": version.version_number,
        "status": version.status,
        "effective_from": version.effective_from,
        "effective_to": version.effective_to,
        "rules_hash": version.rules_hash,
        "published_at": version.published_at,
        "validated": version.validated_at is not None,
        "row_version": version.row_version,
    }


def _version_out(db: Session, version: CreditProductVersion) -> dict:
    return {
        **_summary(version),
        "product_id": version.product_id,
        "tenant_id": version.tenant_id,
        "rules": version.rules,
        "currencies": _currency_rows(db, version.id),
        "created_at": version.created_at,
        "updated_at": version.updated_at,
    }


def _content_hash(rules: dict, currencies: list[dict]) -> str | None:
    """Hash of the draft content when it parses (used to detect edits after validation); None otherwise."""
    try:
        return compute_rules_hash(parse_rules(rules), currencies)
    except Exception:  # an incomplete/ill-typed draft has no hash yet
        return None


def _audit(db: Session, actor: Principal, event: str, client_ip: str | None, **details) -> None:
    record_event(db, event, tenant_id=actor.tenant_id, actor_id=actor.user_id, client_ip=client_ip, details=details)


# --- products ---------------------------------------------------------------------------------------
def create_product(db: Session, actor: Principal, body: ProductCreateIn, client_ip: str | None) -> dict:
    _gate(actor, CREATE)
    code = body.code.strip().upper()
    product = CreditProduct(
        tenant_id=actor.tenant_id, code=code, name=body.name, description=body.description, created_by=actor.user_id
    )
    db.add(product)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("Ya existe un producto con ese codigo (formato: A-Z, 0-9, _ o -).") from None
    _audit(db, actor, "credit_product.created", client_ip, product_id=product.id, code=code, status="draft")
    db.commit()
    return get_product(db, actor, product.id)


def list_products(db: Session, actor: Principal, *, status: str | None, q: str | None, limit: int, offset: int):
    _gate(actor, READ)
    stmt = select(CreditProduct).where(CreditProduct.tenant_id == actor.tenant_id)
    if status:
        stmt = stmt.where(CreditProduct.status == status)
    if q:
        term = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(CreditProduct.name.ilike(term) | CreditProduct.code.ilike(term))
    products = db.scalars(stmt.order_by(CreditProduct.code).limit(limit).offset(offset)).all()
    open_versions = {}
    if products:  # one query for every row: no N+1
        for v in db.scalars(
            select(CreditProductVersion).where(
                CreditProductVersion.tenant_id == actor.tenant_id,
                CreditProductVersion.product_id.in_([p.id for p in products]),
                CreditProductVersion.status == "published",
                CreditProductVersion.effective_to.is_(None),
            )
        ):
            open_versions[v.product_id] = v
    return [
        {
            "id": p.id,
            "code": p.code,
            "name": p.name,
            "status": p.status,
            "current_version": _summary(open_versions[p.id]) if p.id in open_versions else None,
        }
        for p in products
    ]


def get_product(db: Session, actor: Principal, product_id: int) -> dict:
    _gate(actor, READ)
    p = _product(db, actor, product_id)
    versions = db.scalars(
        select(CreditProductVersion)
        .where(CreditProductVersion.product_id == p.id, CreditProductVersion.tenant_id == actor.tenant_id)
        .order_by(CreditProductVersion.version_number)
    ).all()
    return {
        "id": p.id,
        "tenant_id": p.tenant_id,
        "code": p.code,
        "name": p.name,
        "description": p.description,
        "status": p.status,
        "row_version": p.row_version,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "versions": [_summary(v) for v in versions],
    }


def set_product_active(
    db: Session, actor: Principal, product_id: int, *, active: bool, reason: str | None, client_ip: str | None
) -> dict:
    _gate(actor, PUBLISH if active else DEACTIVATE)
    product = _product(db, actor, product_id, lock=True)
    before = product.status
    if active:
        offered = db.scalar(
            select(CreditProductVersion.id).where(
                CreditProductVersion.product_id == product.id,
                CreditProductVersion.status == "published",
                CreditProductVersion.effective_to.is_(None),
            )
        )
        if before != "inactive" or offered is None:
            raise InvalidStateTransition("Solo se reactiva un producto inactivo con una version publicada vigente.")
    elif before != "active":
        raise InvalidStateTransition("Solo se desactiva un producto activo.")
    product.status = "active" if active else "inactive"
    product.row_version += 1
    _audit(
        db,
        actor,
        "credit_product.activated" if active else "credit_product.deactivated",
        client_ip,
        product_id=product.id,
        before={"status": before},
        after={"status": product.status},
        reason=reason,
    )
    db.commit()
    return get_product(db, actor, product.id)


# --- versions ---------------------------------------------------------------------------------------
def _set_currencies(db: Session, actor: Principal, version: CreditProductVersion, currencies: list) -> None:
    db.query(CreditProductCurrency).filter(CreditProductCurrency.version_id == version.id).delete()
    db.flush()
    for c in currencies:
        db.add(
            CreditProductCurrency(
                version_id=version.id,
                tenant_id=actor.tenant_id,
                currency_code=c["code"],
                min_amount=Decimal(c["min_amount"]),
                max_amount=Decimal(c["max_amount"]),
            )
        )
    try:
        db.flush()
    except IntegrityError:  # unknown/foreign currency or invalid range: the composite FK / CHECK refused it
        db.rollback()
        raise BusinessRuleViolation(
            "Moneda no habilitada para la agencia, repetida o con un rango de montos invalido."
        ) from None


def create_version(
    db: Session, actor: Principal, product_id: int, body: VersionCreateIn, client_ip: str | None
) -> dict:
    _gate(actor, UPDATE_DRAFT)
    product = _product(db, actor, product_id, lock=True)  # serialises numbering of this product's versions
    if body.based_on_version_id is not None:
        base = _version(db, actor, product.id, body.based_on_version_id)
        rules, currencies = json.loads(json.dumps(base.rules)), _currency_rows(db, base.id)
    else:
        rules = body.rules.model_dump(mode="json", exclude_none=True)
        currencies = [c.model_dump() for c in body.currencies or []]
    number = (
        db.scalar(
            select(CreditProductVersion.version_number)
            .where(CreditProductVersion.product_id == product.id)
            .order_by(CreditProductVersion.version_number.desc())
            .limit(1)
        )
        or 0
    ) + 1
    version = CreditProductVersion(
        tenant_id=actor.tenant_id,
        product_id=product.id,
        version_number=number,
        status="draft",
        rules=rules,
        created_by=actor.user_id,
    )
    db.add(version)
    db.flush()
    _set_currencies(db, actor, version, currencies)
    _audit(
        db,
        actor,
        "credit_product_version.created",
        client_ip,
        product_id=product.id,
        version_id=version.id,
        version_number=number,
        based_on_version_id=body.based_on_version_id,
    )
    db.commit()
    return _version_out(db, version)


def get_version(db: Session, actor: Principal, product_id: int, version_id: int) -> dict:
    _gate(actor, READ)
    return _version_out(db, _version(db, actor, product_id, version_id))


def update_version(
    db: Session, actor: Principal, product_id: int, version_id: int, body: VersionUpdateIn, client_ip: str | None
) -> dict:
    _gate(actor, UPDATE_DRAFT)
    product = _product(db, actor, product_id, lock=True)
    version = _version(db, actor, product.id, version_id)
    if version.status != "draft":
        raise VersionImmutable()
    if version.row_version != body.row_version:
        raise RowVersionConflict()
    before_rules, before_currencies = version.rules, _currency_rows(db, version.id)
    changed = []
    if body.rules is not None:
        new_rules = body.rules.model_dump(mode="json", exclude_none=True)
        changed += [k for k in sorted(set(new_rules) | set(before_rules)) if new_rules.get(k) != before_rules.get(k)]
        version.rules = new_rules
    if body.currencies is not None:
        _set_currencies(db, actor, version, [c.model_dump() for c in body.currencies])
        changed.append("currencies")
    version.validated_at = version.validated_hash = None  # any edit voids a previous validation
    version.row_version += 1
    version.updated_at = now_utc()
    _audit(
        db,
        actor,
        "credit_product_version.updated",
        client_ip,
        product_id=product.id,
        version_id=version.id,
        changed_sections=changed,
        before={"content_digest": _content_hash(before_rules, before_currencies)},
        after={"content_digest": _content_hash(version.rules, _currency_rows(db, version.id))},
    )
    db.commit()
    return _version_out(db, version)


def _run_validation(db: Session, actor: Principal, version: CreditProductVersion) -> tuple[Validation, list[dict]]:
    currencies = _currency_rows(db, version.id)
    return validate_rules(version.rules, currencies, _enabled_currencies(db, actor.tenant_id)), currencies


def validate_version(db: Session, actor: Principal, product_id: int, version_id: int, client_ip: str | None) -> dict:
    _gate(actor, UPDATE_DRAFT)
    product = _product(db, actor, product_id, lock=True)
    version = _version(db, actor, product.id, version_id)
    if version.status != "draft":
        raise VersionImmutable("Solo se validan borradores; las versiones publicadas ya fueron validadas.")
    result, currencies = _run_validation(db, actor, version)
    digest = compute_rules_hash(result.rules, currencies) if result.valid else None
    version.validated_at = now_utc() if result.valid else None
    version.validated_hash = digest
    _audit(
        db,
        actor,
        "credit_product_version.validated",
        client_ip,
        product_id=product.id,
        version_id=version.id,
        valid=result.valid,
        issue_count=len(result.issues),
        rules_digest=digest,
    )
    db.commit()
    return {
        "valid": result.valid,
        "issues": [i.as_dict() for i in result.issues],
        "warnings": result.warnings,
        "rules_hash": digest,
    }


def build_snapshot(
    product: CreditProduct, version: CreditProductVersion, rules, currencies: list[dict], digest: str
) -> dict:
    """Canonical, serialisable, immutable image of the effective rules (T-005 §21). Keys are all JSON-native."""
    return {
        "snapshot_version": 1,
        "tenant_id": product.tenant_id,
        "product_id": product.id,
        "product_code": product.code,
        "product_version_id": version.id,
        "version_number": version.version_number,
        "effective_from": version.effective_from.isoformat(),
        "rules": _canon(rules),
        "currencies": [
            {"code": c["code"], "min_amount": c["min_amount"], "max_amount": c["max_amount"]} for c in currencies
        ],
        "rules_hash": digest,
    }


def _verify(version: CreditProductVersion, currencies: list[dict]) -> bool:
    """Recompute the hash from the stored columns and from the snapshot; both must equal ``rules_hash``."""
    if version.rules_hash is None or version.snapshot is None:
        return False
    snap = version.snapshot
    try:
        from_columns = compute_rules_hash(parse_rules(version.rules), currencies)
        from_snapshot = compute_rules_hash(snap["rules"], snap["currencies"])
    except Exception:
        return False
    return from_columns == from_snapshot == version.rules_hash == snap.get("rules_hash")


def publish_version(
    db: Session, actor: Principal, product_id: int, version_id: int, body: PublishIn, client_ip: str | None
) -> dict:
    _gate(actor, PUBLISH)
    product = _product(db, actor, product_id, lock=True)
    version = _version(db, actor, product.id, version_id)
    if version.status != "draft":
        raise VersionImmutable("La version ya esta publicada.")
    if version.row_version != body.row_version:
        raise RowVersionConflict()
    if product.status == "inactive":
        raise InvalidStateTransition("El producto esta inactivo: reactivalo antes de publicar.")
    result, currencies = _run_validation(db, actor, version)
    if not result.valid:
        _audit(
            db,
            actor,
            "credit_product_version.publish_rejected",
            client_ip,
            product_id=product.id,
            version_id=version.id,
            issue_count=len(result.issues),
        )
        db.commit()
        raise ProductValidationFailed(details=[i.as_dict() for i in result.issues])
    digest = compute_rules_hash(result.rules, currencies)
    if version.validated_at is None or version.validated_hash != digest:
        raise BusinessRuleViolation("La version debe validarse (sin cambios posteriores) antes de publicarse.")

    tenant = _tenant(db, actor)
    today = business_date(tz=tenant.default_timezone)
    if body.effective_from < today:
        raise BusinessRuleViolation("La vigencia no puede iniciar en el pasado (fecha de la agencia).")
    others = db.scalars(
        select(CreditProductVersion).where(
            CreditProductVersion.product_id == product.id, CreditProductVersion.status != "draft"
        )
    ).all()
    floor = max((o.effective_to or o.effective_from for o in others), default=None)
    if floor is not None and body.effective_from <= floor:
        raise BusinessRuleViolation(f"La vigencia debe iniciar despues de {floor.isoformat()} (versiones previas).")
    for o in others:  # supersede: the previous open-ended version ends the day before this one starts
        if o.status == "published" and o.effective_to is None:
            o.effective_to = body.effective_from - timedelta(days=1)

    version.effective_from = body.effective_from
    version.rules_hash = digest
    version.published_at = now_utc()
    version.published_by = actor.user_id
    version.snapshot = build_snapshot(product, version, result.rules, currencies, digest)
    version.status = "published"
    version.row_version += 1
    version.updated_at = now_utc()
    before_status = product.status
    product.status = "active"
    product.row_version += 1
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("Otra publicacion concurrente gano; reintenta.") from None
    _audit(
        db,
        actor,
        "credit_product_version.published",
        client_ip,
        product_id=product.id,
        version_id=version.id,
        version_number=version.version_number,
        effective_from=body.effective_from.isoformat(),
        rules_digest=digest,
        before={"status": "draft", "product_status": before_status},
        after={"status": "published", "product_status": "active"},
    )
    db.commit()
    return _version_out(db, version)


def retire_version(
    db: Session, actor: Principal, product_id: int, version_id: int, reason: str, client_ip: str | None
) -> dict:
    _gate(actor, DEACTIVATE)
    product = _product(db, actor, product_id, lock=True)
    version = _version(db, actor, product.id, version_id)
    if version.status != "published":
        raise InvalidStateTransition("Solo se retira una version publicada.")
    today = business_date(tz=_tenant(db, actor).default_timezone)
    if version.effective_to is None:
        version.effective_to = max(version.effective_from, today)
    version.status = "retired"
    version.retired_at, version.retired_by = now_utc(), actor.user_id
    version.row_version += 1
    _audit(
        db,
        actor,
        "credit_product_version.retired",
        client_ip,
        product_id=product.id,
        version_id=version.id,
        rules_digest=version.rules_hash,
        before={"status": "published"},
        after={"status": "retired"},
        reason=reason,
    )
    db.commit()
    return _version_out(db, version)


def effective_version(db: Session, actor: Principal, product_id: int, on: date | None) -> dict:
    """Version in force on ``on`` (default: today in the tenant's timezone). Pure read."""
    _gate(actor, READ)
    product = _product(db, actor, product_id)
    day = on or business_date(tz=_tenant(db, actor).default_timezone)
    version = db.scalar(
        select(CreditProductVersion).where(
            CreditProductVersion.product_id == product.id,
            CreditProductVersion.tenant_id == actor.tenant_id,
            CreditProductVersion.status.in_(("published", "retired")),
            CreditProductVersion.effective_from <= day,
            (CreditProductVersion.effective_to.is_(None)) | (CreditProductVersion.effective_to >= day),
        )
    )
    if version is None:
        raise TenantMismatch("No hay una version vigente en esa fecha.")
    return _version_out(db, version)


def get_snapshot(db: Session, actor: Principal, product_id: int, version_id: int) -> dict:
    _gate(actor, READ)
    version = _version(db, actor, product_id, version_id)
    if version.status == "draft" or version.snapshot is None:
        raise InvalidStateTransition("El borrador aun no tiene snapshot.")
    return {
        "snapshot": version.snapshot,
        "rules_hash": version.rules_hash,
        "hash_verified": _verify(version, _currency_rows(db, version.id)),
    }


# --- simulation (no writes of any kind) -------------------------------------------------------------
def simulate(db: Session, actor: Principal, product_id: int, version_id: int, body: SimulateIn) -> dict:
    _gate(actor, READ)
    version = _version(db, actor, product_id, version_id)
    currencies = _currency_rows(db, version.id)
    if version.status == "draft":
        result = validate_rules(version.rules, currencies, _enabled_currencies(db, actor.tenant_id))
        if not result.valid:
            raise ProductValidationFailed(details=[i.as_dict() for i in result.issues])
        rules = result.rules
    else:
        if not _verify(version, currencies):
            raise RulesIntegrityFailed()
        rules = parse_rules(version.rules)
    limits = next((c for c in currencies if c["code"] == body.currency), None)
    if limits is None:
        raise SimulationRejected("La moneda no esta permitida por esta version del producto.")
    exponent = db.scalar(select(Currency.exponent).where(Currency.code == body.currency))
    tz = rules.calendar.timezone
    start = body.start_date or body.start_at.astimezone(get_zone(tz)).date()
    try:
        out = engine.simulate(
            rules,
            currency=body.currency,
            exponent=exponent,
            limits=(Decimal(limits["min_amount"]), Decimal(limits["max_amount"])),
            principal=Decimal(body.principal),
            term_periods=body.term_periods,
            start_date=start,
        )
    except engine.EngineError as exc:
        raise SimulationRejected(str(exc)) from None
    return {
        **out,
        "product_id": product_id,
        "product_version_id": version.id,
        "version_number": version.version_number,
        "version_status": version.status,
        "rules_hash": version.rules_hash if version.status != "draft" else compute_rules_hash(rules, currencies),
        "timezone": tz,
    }
