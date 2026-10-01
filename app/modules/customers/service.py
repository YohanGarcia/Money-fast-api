"""Customer use cases (T-004).

* Tenant = the principal's tenant, always. Another tenant's ids behave like missing ones (404).
* Branch scope applies to the customer's *management* branch: a branch-scoped grant reaches only customers
  managed by that branch; customers without a management branch need tenant-wide authority.
* Nothing here merges, deletes or auto-resolves duplicates. Reads never write.
* Audit records changed field names, masked identifiers and ids — never full documents, phones or e-mails; the
  real before/after of identity changes lives in ``person_identity_revisions`` (access-controlled data).
"""

from datetime import date

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BusinessRuleViolation, Conflict, ValidationFailed
from app.core.time import now_utc
from app.models.branch import Branch
from app.models.company import Company
from app.modules.customers import dedup
from app.modules.customers.errors import DuplicateIdentity, PossibleDuplicate, VersionConflict
from app.modules.customers.models import (
    CustomerAddress,
    CustomerContact,
    CustomerDuplicateFlag,
    CustomerProfile,
    CustomerReference,
    PersonIdentityRevision,
)
from app.modules.customers.schemas import (
    AddressIn,
    BranchAssignIn,
    ContactIn,
    CustomerCreateIn,
    CustomerPatchIn,
    DuplicateCheckIn,
    IdentityCorrectionIn,
    IdentityIn,
    ReferenceIn,
)
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, require
from app.modules.identity.errors import InvalidStateTransition, PermissionDenied, TenantMismatch
from app.modules.identity.models import Person
from app.shared.normalization import (
    collapse_spaces,
    mask_document,
    mask_email,
    mask_phone,
    normalize_code,
    normalize_document,
    normalize_document_type,
    normalize_email,
    normalize_name,
    normalize_phone,
)

CODE_PREFIX = "CLI-"
MIN_PHONE_DIGITS = 7  # technical sanity bound, not a legal rule
IDENTITY_FIELDS = (
    "given_names",
    "family_names",
    "document_type",
    "document_number",
    "document_country",
    "document_issue_date",
    "document_expiry_date",
    "birth_date",
    "nationality",
)


# --- scope helpers ----------------------------------------
def _tenant(db: Session, actor: Principal) -> Company:
    tenant = db.get(Company, actor.tenant_id) if actor.tenant_id is not None else None
    if tenant is None:
        raise TenantMismatch()
    return tenant


def _profile(db: Session, actor: Principal, customer_id: int) -> CustomerProfile:
    profile = db.get(CustomerProfile, customer_id)
    if profile is None or profile.tenant_id != actor.tenant_id:
        raise TenantMismatch()
    return profile


def _require(actor: Principal, permission: str, profile: CustomerProfile) -> None:
    require(actor, permission, tenant_id=actor.tenant_id, branch_id=profile.management_branch_id)


def _can(actor: Principal, permission: str, profile: CustomerProfile) -> bool:
    return actor.allows(permission, tenant_id=actor.tenant_id, branch_id=profile.management_branch_id)


def visible_branch_ids(actor: Principal, permission: str) -> set[int] | None:
    """None = whole tenant; otherwise the management branches reachable by the actor's grants."""
    if actor.holds_at_tenant_scope(permission):
        return None
    ids = {g.branch_id for g in actor.grants if g.permission == permission and g.scope_kind == "branch" and g.branch_id}
    if not ids:
        raise PermissionDenied()
    return ids


def _branch(db: Session, actor: Principal, branch_id: int | None, *, must_be_active: bool = True) -> Branch | None:
    if branch_id is None:
        return None
    branch = db.get(Branch, branch_id)
    if branch is None or branch.company_id != actor.tenant_id:
        raise TenantMismatch()  # another tenant's branch is a 404, never a mismatch message
    if must_be_active and branch.status != "active":
        raise BusinessRuleViolation("La sucursal esta inactiva.")
    return branch


def next_customer_code(db: Session, tenant_id: int) -> str:
    """Per-tenant monotonic counter (atomic upsert). Codes are never reused."""
    value = db.execute(
        text(
            "INSERT INTO tenant_sequences (tenant_id, name, last_value) VALUES (:t, 'customer_code', 1) "
            "ON CONFLICT (tenant_id, name) DO UPDATE SET last_value = tenant_sequences.last_value + 1 "
            "RETURNING last_value"
        ),
        {"t": tenant_id},
    ).scalar_one()
    return f"{CODE_PREFIX}{value:06d}"


# --- duplicates ----------------------------------------
def _candidate_out(db: Session, actor: Principal, cand: dedup.Candidate) -> dict:
    """Candidates are shown only when the caller could read that customer anyway."""
    if cand.customer_id is None:
        return {
            "person_id": cand.person_id,
            "customer_id": None,
            "customer_code": None,
            "signals": cand.signals,
            "restricted": False,
        }
    profile = db.get(CustomerProfile, cand.customer_id)
    if profile is None or not _can(actor, "customers.read", profile):
        return {
            "person_id": None,
            "customer_id": None,
            "customer_code": None,
            "signals": cand.signals,
            "restricted": True,
        }
    return {
        "person_id": cand.person_id,
        "customer_id": profile.id,
        "customer_code": profile.customer_code,
        "signals": cand.signals,
        "restricted": False,
    }


def _result_out(db: Session, actor: Principal, result: dedup.DuplicateResult) -> dict:
    return {
        "classification": result.classification,
        "exact": [_candidate_out(db, actor, c) for c in result.exact],
        "possible": [_candidate_out(db, actor, c) for c in result.possible],
    }


def check_duplicates(db: Session, actor: Principal, body: DuplicateCheckIn) -> dict:
    """Read-only preview of the classification (POST only because it carries identifiers in the body)."""
    require(actor, "customers.read", tenant_id=actor.tenant_id)  # tenant-wide read: it searches the whole tenant
    result = dedup.classify(
        db,
        actor.tenant_id,
        document_type=normalize_document_type(body.document_type),
        document_normalized=normalize_document(body.document_number),
        phones=[p for p in map(normalize_phone, body.phones) if p],
        emails=[e for e in map(normalize_email, body.emails) if e],
        name_normalized=normalize_name(f"{body.given_names or ''} {body.family_names or ''}"),
        birth_date=body.birth_date,
    )
    return _result_out(db, actor, result)


def _flag_pairs(
    db: Session, actor: Principal, profile: CustomerProfile, result: dedup.DuplicateResult
) -> list[CustomerDuplicateFlag]:
    flags = []
    for cand in result.possible:
        a, b = profile.id, cand.customer_id
        exists = db.scalar(
            select(CustomerDuplicateFlag.id).where(
                or_(
                    (CustomerDuplicateFlag.customer_id == a) & (CustomerDuplicateFlag.candidate_customer_id == b),
                    (CustomerDuplicateFlag.customer_id == b) & (CustomerDuplicateFlag.candidate_customer_id == a),
                )
            )
        )
        if exists:
            continue
        flag = CustomerDuplicateFlag(
            tenant_id=actor.tenant_id,
            customer_id=a,
            candidate_customer_id=b,
            signals=cand.signals,
            created_by=actor.user_id,
        )
        db.add(flag)
        flags.append(flag)
    db.flush()
    return flags


# --- creation ----------------------------------------
def _identity_values(identity: IdentityIn) -> dict:
    return {
        "given_names": collapse_spaces(identity.given_names),
        "family_names": collapse_spaces(identity.family_names),
        "alias": collapse_spaces(identity.alias) or None,
        "document_type": normalize_document_type(identity.document_type),
        "document_number": collapse_spaces(identity.document_number) or None,
        "document_country": identity.document_country,
        "document_issue_date": identity.document_issue_date,
        "document_expiry_date": identity.document_expiry_date,
        "birth_date": identity.birth_date,
        "nationality": identity.nationality,
    }


def _exact_error(db: Session, actor: Principal, result: dedup.DuplicateResult) -> DuplicateIdentity:
    return DuplicateIdentity(details=_result_out(db, actor, result))


def create_customer(db: Session, actor: Principal, body: CustomerCreateIn, client_ip: str | None) -> CustomerProfile:
    tenant = _tenant(db, actor)
    if tenant.status != "active":
        raise BusinessRuleViolation("La agencia esta inactiva.")
    require(actor, "customers.create", tenant_id=actor.tenant_id, branch_id=body.management_branch_id)
    _branch(db, actor, body.origin_branch_id)
    _branch(db, actor, body.management_branch_id)

    linked_existing = body.person_id is not None
    if linked_existing:
        person = db.get(Person, body.person_id)
        if person is None or person.tenant_id != actor.tenant_id:
            raise TenantMismatch()
        if db.scalar(select(CustomerProfile.id).where(CustomerProfile.person_id == person.id)):
            raise Conflict("Esa persona ya es cliente de la agencia.")
    else:
        values = _identity_values(body.identity)
        person = None

    # --- duplicates (identity of the person that will become / already is the customer)
    contacts_phones = [normalize_phone(c.value) for c in body.contacts if c.type in ("phone", "mobile")]
    contacts_emails = [normalize_email(c.value) for c in body.contacts if c.type == "email"]
    if linked_existing:
        probe = dict(
            document_type=person.document_type,
            document_normalized=person.document_number_normalized,
            name_normalized=person.search_name,
            birth_date=person.birth_date,
            exclude_person_id=person.id,
        )
    else:
        probe = dict(
            document_type=values["document_type"],
            document_normalized=normalize_document(values["document_number"]),
            name_normalized=normalize_name(f"{values['given_names']} {values['family_names']}"),
            birth_date=values["birth_date"],
            exclude_person_id=None,
        )
    result = dedup.classify(
        db, actor.tenant_id, phones=[p for p in contacts_phones if p], emails=[e for e in contacts_emails if e], **probe
    )
    if result.classification == dedup.EXACT_MATCH:
        raise _exact_error(db, actor, result)
    if result.classification == dedup.POSSIBLE_MATCH and not body.acknowledge_possible_duplicates:
        raise PossibleDuplicate(details=_result_out(db, actor, result))

    if not linked_existing:
        person = Person(tenant_id=actor.tenant_id, **values)
        db.add(person)
        try:
            db.flush()
        except IntegrityError:  # lost a race on the unique normalised document
            db.rollback()
            raise DuplicateIdentity() from None

    code = normalize_code(body.customer_code) if body.customer_code else next_customer_code(db, actor.tenant_id)
    profile = CustomerProfile(
        tenant_id=actor.tenant_id,
        person_id=person.id,
        customer_code=code,
        status="pending",
        origin_branch_id=body.origin_branch_id,
        management_branch_id=body.management_branch_id,
        marital_status=body.marital_status,
        internal_note=body.internal_note,
        created_by=actor.user_id,
        updated_by=actor.user_id,
    )
    db.add(profile)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("Ya existe un cliente con ese codigo.") from None

    for c in body.contacts:
        _add_contact_row(db, actor, profile, c)
    for a in body.addresses:
        _add_address_row(db, actor, profile, a)
    for r in body.references:
        _add_reference_row(db, actor, profile, r)
    db.flush()
    flags = _flag_pairs(db, actor, profile, result) if result.possible else []

    record_event(
        db,
        "customer.created",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={
            "customer_id": profile.id,
            "customer_code": profile.customer_code,
            "person_id": person.id,
            "linked_existing_person": linked_existing,
            "status": profile.status,
            "origin_branch_id": profile.origin_branch_id,
            "management_branch_id": profile.management_branch_id,
            "document_masked": mask_document(person.document_number_normalized),
        },
    )
    for flag in flags:
        record_event(
            db,
            "customer.duplicate_flagged",
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            client_ip=client_ip,
            details={
                "customer_id": profile.id,
                "candidate_customer_id": flag.candidate_customer_id,
                "signals": flag.signals,
            },
        )
    db.commit()
    return profile


# --- reads ----------------------------------------
def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_customers(
    db: Session,
    actor: Principal,
    *,
    q: str | None = None,
    status: str | None = None,
    branch_id: int | None = None,
    code: str | None = None,
    document: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Server-side, tenant-scoped search. Exact normalised match for document/phone/email (no partial
    scans of identifiers); accent/case-insensitive substring only for names. Minimal fields are returned."""
    ids = visible_branch_ids(actor, "customers.read")
    stmt = (
        select(CustomerProfile, Person)
        .join(Person, Person.id == CustomerProfile.person_id)
        .where(CustomerProfile.tenant_id == actor.tenant_id)
        .order_by(CustomerProfile.id)
    )
    if ids is not None:
        stmt = stmt.where(CustomerProfile.management_branch_id.in_(ids))
    if status:
        stmt = stmt.where(CustomerProfile.status == status)
    if branch_id is not None:
        stmt = stmt.where(CustomerProfile.management_branch_id == branch_id)
    if code:
        stmt = stmt.where(CustomerProfile.customer_code == normalize_code(code))
    if q:
        term = normalize_name(q)
        stmt = stmt.where(
            or_(
                CustomerProfile.customer_code == normalize_code(q),
                Person.search_name.like(f"%{_escape_like(term)}%", escape="\\"),
            )
        )
    if document:
        stmt = stmt.where(Person.document_number_normalized == normalize_document(document))
    for value, ctypes, normalizer in (
        (phone, ("phone", "mobile"), normalize_phone),
        (email, ("email",), normalize_email),
    ):
        if value:
            normalized = normalizer(value)
            stmt = stmt.where(
                CustomerProfile.id.in_(
                    select(CustomerContact.customer_id).where(
                        CustomerContact.tenant_id == actor.tenant_id,
                        CustomerContact.type.in_(ctypes),
                        CustomerContact.status == "active",
                        CustomerContact.normalized_value == normalized,
                    )
                )
            )
    rows = db.execute(stmt.limit(min(limit, 100)).offset(max(offset, 0))).all()
    return [
        {
            "id": p.id,
            "customer_code": p.customer_code,
            "display_name": person.full_name,
            "status": p.status,
            "origin_branch_id": p.origin_branch_id,
            "management_branch_id": p.management_branch_id,
            "document_masked": mask_document(person.document_number_normalized),
        }
        for p, person in rows
    ]


def get_customer(db: Session, actor: Principal, customer_id: int) -> dict:
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.read", profile)
    return _detail(db, actor, profile)


def _detail(db: Session, actor: Principal, profile: CustomerProfile) -> dict:
    person = db.get(Person, profile.person_id)
    sensitive = _can(actor, "customers.read_sensitive", profile)
    pending = db.scalar(
        select(func.count())
        .select_from(CustomerDuplicateFlag)
        .where(
            or_(
                CustomerDuplicateFlag.customer_id == profile.id,
                CustomerDuplicateFlag.candidate_customer_id == profile.id,
            ),
            CustomerDuplicateFlag.status == "pending_review",
        )
    )
    active = lambda model: list(  # noqa: E731
        db.scalars(select(model).where(model.customer_id == profile.id, model.status == "active").order_by(model.id))
    )
    return {
        "id": profile.id,
        "tenant_id": profile.tenant_id,
        "customer_code": profile.customer_code,
        "status": profile.status,
        "origin_branch_id": profile.origin_branch_id,
        "management_branch_id": profile.management_branch_id,
        "marital_status": profile.marital_status,
        "internal_note": profile.internal_note,
        "version": profile.version,
        "person": {
            "id": person.id,
            "given_names": person.given_names,
            "family_names": person.family_names,
            "alias": person.alias,
            "nationality": person.nationality,
            "document_type": person.document_type,
            "document_number": person.document_number
            if sensitive
            else mask_document(person.document_number_normalized),
            "document_masked": not sensitive,
            "document_country": person.document_country if sensitive else None,
            "document_issue_date": person.document_issue_date if sensitive else None,
            "document_expiry_date": person.document_expiry_date if sensitive else None,
            "birth_date": person.birth_date if sensitive else None,
        },
        "contacts": active(CustomerContact),
        "addresses": active(CustomerAddress),
        "references": active(CustomerReference),
        "duplicate_flags_pending": pending or 0,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


# --- updates ----------------------------------------
def _check_version(profile: CustomerProfile, version: int) -> None:
    if profile.version != version:
        raise VersionConflict()


def update_customer(
    db: Session, actor: Principal, customer_id: int, body: CustomerPatchIn, client_ip: str | None
) -> dict:
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.update", profile)
    _check_version(profile, body.version)
    person = db.get(Person, profile.person_id)
    changed = []
    if body.marital_status is not None and body.marital_status != profile.marital_status:
        profile.marital_status = body.marital_status
        changed.append("marital_status")
    if body.internal_note is not None and body.internal_note != profile.internal_note:
        profile.internal_note = body.internal_note
        changed.append("internal_note")
    if body.alias is not None and (collapse_spaces(body.alias) or None) != person.alias:
        person.alias = collapse_spaces(body.alias) or None
        changed.append("alias")
    if changed:
        profile.updated_by = actor.user_id
        profile.updated_at = now_utc()
        record_event(
            db,
            "customer.updated",
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            client_ip=client_ip,
            details={"customer_id": profile.id, "changed_fields": changed},
        )
    db.commit()
    db.refresh(profile)
    return _detail(db, actor, profile)


def correct_identity(
    db: Session, actor: Principal, customer_id: int, body: IdentityCorrectionIn, client_ip: str | None
) -> dict:
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.identity.correct", profile)
    _check_version(profile, body.version)
    person = db.get(Person, profile.person_id)
    supplied = {f: getattr(body, f) for f in IDENTITY_FIELDS if f in body.model_fields_set}
    if not supplied:
        raise ValidationFailed("No se indico ningun dato de identidad.")
    before = {f: _jsonable(getattr(person, f)) for f in IDENTITY_FIELDS}
    for field, value in supplied.items():
        setattr(person, field, collapse_spaces(value) or None if isinstance(value, str) else value)
    if person.document_number is not None and normalize_document(person.document_number) is None:
        raise ValidationFailed("El numero de documento no contiene caracteres validos.")
    if person.document_number is not None and normalize_document_type(person.document_type) is None:
        raise ValidationFailed("El tipo de documento es obligatorio cuando se indica el numero.")
    result = dedup.classify(
        db,
        actor.tenant_id,
        document_type=normalize_document_type(person.document_type),
        document_normalized=normalize_document(person.document_number),
        phones=[],
        emails=[],
        name_normalized="",
        birth_date=None,
        exclude_person_id=person.id,
    )
    if result.classification == dedup.EXACT_MATCH:
        db.rollback()
        raise _exact_error(db, actor, result)
    after = {f: _jsonable(getattr(person, f)) for f in IDENTITY_FIELDS}
    changed = [f for f in IDENTITY_FIELDS if before[f] != after[f]]
    if changed:
        db.add(
            PersonIdentityRevision(
                tenant_id=actor.tenant_id,
                person_id=person.id,
                kind=body.kind,
                reason=body.reason.strip(),
                before=before,
                after=after,
                changed_by=actor.user_id,
            )
        )
        profile.updated_by = actor.user_id
        profile.updated_at = now_utc()  # marks the profile dirty so its version column bumps
        masked = {}
        if "document_number" in changed or "document_type" in changed:
            masked = {
                "document_before_masked": mask_document(normalize_document(before["document_number"])),
                "document_after_masked": mask_document(normalize_document(after["document_number"])),
            }
        record_event(
            db,
            "customer.identity_changed",
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            client_ip=client_ip,
            details={
                "customer_id": profile.id,
                "person_id": person.id,
                "kind": body.kind,
                "changed_fields": changed,
                **masked,
            },
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DuplicateIdentity() from None
    db.refresh(profile)
    return _detail(db, actor, profile)


def _jsonable(value):
    return value.isoformat() if isinstance(value, date) else value


def set_status(db: Session, actor: Principal, customer_id: int, action: str, client_ip: str | None) -> dict:
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.activate", profile)
    transitions = {"activate": (("pending", "inactive"), "active"), "deactivate": (("pending", "active"), "inactive")}
    allowed_from, target = transitions[action]
    if profile.status not in allowed_from:
        raise InvalidStateTransition()
    before = profile.status
    profile.status, profile.status_changed_at, profile.updated_by = target, now_utc(), actor.user_id
    record_event(
        db,
        f"customer.{'activated' if action == 'activate' else 'deactivated'}",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"customer_id": profile.id, "before": {"status": before}, "after": {"status": target}},
    )
    db.commit()
    db.refresh(profile)
    return _detail(db, actor, profile)


def assign_management_branch(
    db: Session, actor: Principal, customer_id: int, body: BranchAssignIn, client_ip: str | None
) -> dict:
    """Moving a customer between branches needs tenant-wide authority; origin is immutable history."""
    profile = _profile(db, actor, customer_id)
    if not actor.holds_at_tenant_scope("customers.assign_branch"):
        raise PermissionDenied()
    _check_version(profile, body.version)
    _branch(db, actor, body.management_branch_id)
    before = profile.management_branch_id
    if before != body.management_branch_id:
        profile.management_branch_id, profile.updated_by = body.management_branch_id, actor.user_id
        record_event(
            db,
            "customer.management_branch_changed",
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            client_ip=client_ip,
            details={
                "customer_id": profile.id,
                "before": {"management_branch_id": before},
                "after": {"management_branch_id": body.management_branch_id},
            },
        )
    db.commit()
    db.refresh(profile)
    return _detail(db, actor, profile)


# --- contacts ----------------------------------------
def _validate_contact(c: ContactIn) -> None:
    if c.type == "email" and normalize_email(c.value) is None:
        raise ValidationFailed("El correo no tiene un formato valido.")
    if c.type in ("phone", "mobile"):
        digits = normalize_phone(c.value)
        if not digits or len(digits) < MIN_PHONE_DIGITS:
            raise ValidationFailed("El telefono debe tener al menos 7 digitos.")


def _add_contact_row(db: Session, actor: Principal, profile: CustomerProfile, c: ContactIn) -> CustomerContact:
    _validate_contact(c)
    active_same_type = db.scalars(
        select(CustomerContact).where(
            CustomerContact.customer_id == profile.id,
            CustomerContact.type == c.type,
            CustomerContact.status == "active",
        )
    ).all()
    make_primary = c.is_primary or not active_same_type  # the first active contact of a type is its primary
    if make_primary:
        for old in active_same_type:
            old.is_primary = False
        db.flush()
    row = CustomerContact(
        tenant_id=actor.tenant_id,
        customer_id=profile.id,
        type=c.type,
        value=c.value,
        label=c.label,
        is_primary=make_primary,
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    return row


def list_contacts(db: Session, actor: Principal, customer_id: int, include_inactive: bool = False):
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.read", profile)
    stmt = select(CustomerContact).where(CustomerContact.customer_id == profile.id).order_by(CustomerContact.id)
    if not include_inactive:
        stmt = stmt.where(CustomerContact.status == "active")
    return list(db.scalars(stmt))


def add_contact(db: Session, actor: Principal, customer_id: int, body: ContactIn, client_ip: str | None):
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.update", profile)
    row = _add_contact_row(db, actor, profile, body)
    flagged = 0
    if row.type != "other" and row.normalized_value:
        probe = dedup.classify(
            db,
            actor.tenant_id,
            document_type=None,
            document_normalized=None,
            phones=[row.normalized_value] if row.type in ("phone", "mobile") else [],
            emails=[row.normalized_value] if row.type == "email" else [],
            name_normalized="",
            birth_date=None,
            exclude_person_id=profile.person_id,
        )
        flagged = len(_flag_pairs(db, actor, profile, probe))
    masked = mask_email(row.value) if row.type == "email" else mask_phone(row.value) if row.type != "other" else None
    record_event(
        db,
        "customer.contact_added",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={
            "customer_id": profile.id,
            "contact_id": row.id,
            "type": row.type,
            "is_primary": row.is_primary,
            "value_masked": masked,
            "duplicate_flags": flagged,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def _contact(
    db: Session, actor: Principal, customer_id: int, contact_id: int
) -> tuple[CustomerProfile, CustomerContact]:
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.update", profile)
    row = db.get(CustomerContact, contact_id)
    if row is None or row.customer_id != profile.id or row.tenant_id != actor.tenant_id:
        raise TenantMismatch()
    return profile, row


def set_primary_contact(db: Session, actor: Principal, customer_id: int, contact_id: int, client_ip: str | None):
    profile, row = _contact(db, actor, customer_id, contact_id)
    if row.status != "active":
        raise InvalidStateTransition()
    if not row.is_primary:
        db.execute(
            update(CustomerContact)
            .where(
                CustomerContact.customer_id == profile.id,
                CustomerContact.type == row.type,
                CustomerContact.is_primary.is_(True),
            )
            .values(is_primary=False)
        )
        row.is_primary = True
        record_event(
            db,
            "customer.contact_primary_changed",
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            client_ip=client_ip,
            details={"customer_id": profile.id, "contact_id": row.id, "type": row.type},
        )
    db.commit()
    db.refresh(row)
    return row


def deactivate_contact(db: Session, actor: Principal, customer_id: int, contact_id: int, client_ip: str | None):
    """History is kept: the row is deactivated, never deleted."""
    profile, row = _contact(db, actor, customer_id, contact_id)
    if row.status != "active":
        raise InvalidStateTransition()
    row.status, row.is_primary, row.inactivated_at, row.inactivated_by = "inactive", False, now_utc(), actor.user_id
    record_event(
        db,
        "customer.contact_deactivated",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"customer_id": profile.id, "contact_id": row.id, "type": row.type},
    )
    db.commit()
    db.refresh(row)
    return row


# --- addresses ----------------------------------------
_TEXT_PARTS = ("street", "sector", "barrio", "municipality", "reference_note")


def _add_address_row(db: Session, actor: Principal, profile: CustomerProfile, a: AddressIn) -> CustomerAddress:
    if not any(collapse_spaces(getattr(a, f)) for f in _TEXT_PARTS):
        raise ValidationFailed("La direccion requiere al menos calle, sector, barrio, municipio o referencia textual.")
    if (a.latitude is None) != (a.longitude is None):
        raise ValidationFailed("Latitud y longitud deben indicarse juntas.")
    active = db.scalars(
        select(CustomerAddress).where(CustomerAddress.customer_id == profile.id, CustomerAddress.status == "active")
    ).all()
    make_primary = a.is_primary or not active
    if make_primary:
        for old in active:
            old.is_primary = False
        db.flush()
    data = a.model_dump(exclude={"is_primary"})
    for key, value in data.items():
        if isinstance(value, str):
            data[key] = collapse_spaces(value) or None
    row = CustomerAddress(
        tenant_id=actor.tenant_id, customer_id=profile.id, is_primary=make_primary, created_by=actor.user_id, **data
    )
    db.add(row)
    db.flush()
    return row


def list_addresses(db: Session, actor: Principal, customer_id: int, include_inactive: bool = False):
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.read", profile)
    stmt = select(CustomerAddress).where(CustomerAddress.customer_id == profile.id).order_by(CustomerAddress.id)
    if not include_inactive:
        stmt = stmt.where(CustomerAddress.status == "active")
    return list(db.scalars(stmt))


def add_address(db: Session, actor: Principal, customer_id: int, body: AddressIn, client_ip: str | None):
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.update", profile)
    row = _add_address_row(db, actor, profile, body)
    record_event(
        db,
        "customer.address_added",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={
            "customer_id": profile.id,
            "address_id": row.id,
            "type": row.type,
            "is_primary": row.is_primary,
            "has_coordinates": row.latitude is not None,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def _address(db: Session, actor: Principal, customer_id: int, address_id: int):
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.update", profile)
    row = db.get(CustomerAddress, address_id)
    if row is None or row.customer_id != profile.id or row.tenant_id != actor.tenant_id:
        raise TenantMismatch()
    return profile, row


def set_primary_address(db: Session, actor: Principal, customer_id: int, address_id: int, client_ip: str | None):
    profile, row = _address(db, actor, customer_id, address_id)
    if row.status != "active":
        raise InvalidStateTransition()
    if not row.is_primary:
        db.execute(
            update(CustomerAddress)
            .where(CustomerAddress.customer_id == profile.id, CustomerAddress.is_primary.is_(True))
            .values(is_primary=False)
        )
        row.is_primary = True
        record_event(
            db,
            "customer.address_primary_changed",
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            client_ip=client_ip,
            details={"customer_id": profile.id, "address_id": row.id},
        )
    db.commit()
    db.refresh(row)
    return row


def deactivate_address(db: Session, actor: Principal, customer_id: int, address_id: int, client_ip: str | None):
    profile, row = _address(db, actor, customer_id, address_id)
    if row.status != "active":
        raise InvalidStateTransition()
    row.status, row.is_primary, row.inactivated_at, row.inactivated_by = "inactive", False, now_utc(), actor.user_id
    record_event(
        db,
        "customer.address_deactivated",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"customer_id": profile.id, "address_id": row.id},
    )
    db.commit()
    db.refresh(row)
    return row


# --- references ----------------------------------------
def _add_reference_row(db: Session, actor: Principal, profile: CustomerProfile, r: ReferenceIn) -> CustomerReference:
    row = CustomerReference(
        tenant_id=actor.tenant_id,
        customer_id=profile.id,
        kind=r.kind,
        name=collapse_spaces(r.name),
        relation=r.relation,
        phone=collapse_spaces(r.phone) or None,
        notes=r.notes,
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    return row


def list_references(db: Session, actor: Principal, customer_id: int, include_inactive: bool = False):
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.read", profile)
    stmt = select(CustomerReference).where(CustomerReference.customer_id == profile.id).order_by(CustomerReference.id)
    if not include_inactive:
        stmt = stmt.where(CustomerReference.status == "active")
    return list(db.scalars(stmt))


def add_reference(db: Session, actor: Principal, customer_id: int, body: ReferenceIn, client_ip: str | None):
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.update", profile)
    row = _add_reference_row(db, actor, profile, body)
    record_event(
        db,
        "customer.reference_added",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"customer_id": profile.id, "reference_id": row.id, "kind": row.kind},
    )
    db.commit()
    db.refresh(row)
    return row


def deactivate_reference(db: Session, actor: Principal, customer_id: int, reference_id: int, client_ip: str | None):
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.update", profile)
    row = db.get(CustomerReference, reference_id)
    if row is None or row.customer_id != profile.id or row.tenant_id != actor.tenant_id:
        raise TenantMismatch()
    if row.status != "active":
        raise InvalidStateTransition()
    row.status, row.inactivated_at, row.inactivated_by = "inactive", now_utc(), actor.user_id
    record_event(
        db,
        "customer.reference_deactivated",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"customer_id": profile.id, "reference_id": row.id},
    )
    db.commit()
    db.refresh(row)
    return row


# --- duplicate flags (review only; never a merge) ----------------------------------------
def list_flags(db: Session, actor: Principal, customer_id: int) -> list[dict]:
    profile = _profile(db, actor, customer_id)
    _require(actor, "customers.duplicates.review", profile)
    flags = db.scalars(
        select(CustomerDuplicateFlag)
        .where(
            or_(
                CustomerDuplicateFlag.customer_id == profile.id,
                CustomerDuplicateFlag.candidate_customer_id == profile.id,
            )
        )
        .order_by(CustomerDuplicateFlag.id)
    ).all()
    out = []
    for f in flags:
        other = f.candidate_customer_id if f.customer_id == profile.id else f.customer_id
        cand = _candidate_out(db, actor, dedup.Candidate(person_id=0, customer_id=other, signals=list(f.signals)))
        out.append(
            {
                "id": f.id,
                "customer_id": profile.id,
                "candidate": cand,
                "signals": list(f.signals),
                "status": f.status,
                "created_at": f.created_at,
                "reviewed_at": f.reviewed_at,
                "review_note": f.review_note,
            }
        )
    return out


def review_flag(db: Session, actor: Principal, flag_id: int, resolution: str, note: str, client_ip: str | None) -> dict:
    flag = db.get(CustomerDuplicateFlag, flag_id)
    if flag is None or flag.tenant_id != actor.tenant_id:
        raise TenantMismatch()
    profile = db.get(CustomerProfile, flag.customer_id)
    _require(actor, "customers.duplicates.review", profile)
    if flag.status != "pending_review":
        raise InvalidStateTransition()
    flag.status, flag.reviewed_by, flag.reviewed_at, flag.review_note = (
        resolution,
        actor.user_id,
        now_utc(),
        note.strip(),
    )
    record_event(
        db,
        "customer.duplicate_reviewed",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={
            "flag_id": flag.id,
            "customer_id": flag.customer_id,
            "candidate_customer_id": flag.candidate_customer_id,
            "resolution": resolution,
        },  # no merge is performed: that needs its own formal operation
    )
    db.commit()
    return {"id": flag.id, "status": flag.status}
