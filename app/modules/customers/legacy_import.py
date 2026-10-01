"""Explicit, idempotent TRANSFORM of legacy flat ``customers`` rows into Person + CustomerProfile (+ contacts,
address, references). Default policy for current data is RESET_AND_RESEED; this tool exists for the cases where
legacy rows must be carried over. It never merges: a legacy row whose document already belongs to another person
is imported WITHOUT its document (kept in the internal note) and flagged for manual review.

Legacy columns deliberately NOT copied: route/collector assignment (Cobranza), cash branch (Caja), version
counters. Legacy ids are only a mapping (``legacy_customer_id``), never a global key.
"""

import json
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.modules.customers import dedup
from app.modules.customers.models import (
    CustomerAddress,
    CustomerContact,
    CustomerDuplicateFlag,
    CustomerProfile,
    CustomerReference,
)
from app.modules.customers.service import next_customer_code
from app.modules.identity.models import Person
from app.shared.normalization import (
    collapse_spaces,
    normalize_document,
    normalize_email,
    normalize_name,
    normalize_phone,
)

LEGACY_DOCUMENT_TYPE = "ID"
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _birth_date(value: str | None) -> date | None:
    if value and _ISO_DATE.fullmatch(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def import_legacy_customers(db: Session, tenant_id: int | None = None) -> dict[str, int]:
    """Import every legacy customer (optionally of one tenant) that has no profile yet. Does not commit."""
    stats = {"imported": 0, "already_imported": 0, "document_conflicts": 0, "possible_duplicates": 0}
    stmt = select(Customer).order_by(Customer.id)
    if tenant_id is not None:
        stmt = stmt.where(Customer.company_id == tenant_id)
    for legacy in db.scalars(stmt):
        if db.scalar(select(CustomerProfile.id).where(CustomerProfile.legacy_customer_id == legacy.id)):
            stats["already_imported"] += 1
            continue
        tenant = legacy.company_id
        doc_norm = normalize_document(legacy.document_id)
        note = None
        document_taken = bool(
            doc_norm
            and db.scalar(
                select(Person.id).where(
                    Person.tenant_id == tenant,
                    Person.document_type == LEGACY_DOCUMENT_TYPE,
                    Person.document_number_normalized == doc_norm,
                )
            )
        )
        if document_taken:
            stats["document_conflicts"] += 1
            note = (
                f"Importado desde el sistema anterior: documento duplicado ({legacy.document_id}); "
                "requiere revision manual."
            )
        keep_document = bool(doc_norm) and not document_taken
        person = Person(
            tenant_id=tenant,
            given_names=collapse_spaces(legacy.full_name),  # never split: the name is kept exactly as stored
            family_names="",
            document_type=LEGACY_DOCUMENT_TYPE if keep_document else None,
            document_number=collapse_spaces(legacy.document_id) if keep_document else None,
            birth_date=_birth_date(legacy.birth_date),
            nationality=legacy.nationality,
        )
        db.add(person)
        db.flush()
        profile = CustomerProfile(
            tenant_id=tenant,
            person_id=person.id,
            customer_code=next_customer_code(db, tenant),
            status="active",
            marital_status=legacy.marital_status,
            internal_note=note,
            legacy_customer_id=legacy.id,
            created_by=legacy.created_by_id,
            updated_by=legacy.created_by_id,
        )
        db.add(profile)
        db.flush()

        phones, emails = [], []
        for ctype, value in (("mobile", legacy.phone), ("phone", legacy.home_phone)):
            if normalize_phone(value):
                db.add(
                    CustomerContact(tenant_id=tenant, customer_id=profile.id, type=ctype, value=value, is_primary=True)
                )
                phones.append(normalize_phone(value))
        if normalize_email(legacy.email):
            db.add(
                CustomerContact(
                    tenant_id=tenant, customer_id=profile.id, type="email", value=legacy.email, is_primary=True
                )
            )
            emails.append(normalize_email(legacy.email))
        street = legacy.calle or legacy.address
        extra = legacy.reference_note
        if legacy.calle and legacy.address and legacy.address != legacy.calle:
            extra = f"{legacy.address}. {legacy.reference_note}" if legacy.reference_note else legacy.address
        if any((street, legacy.sector, legacy.barrio, legacy.city, extra)):
            db.add(
                CustomerAddress(
                    tenant_id=tenant,
                    customer_id=profile.id,
                    type="residence",
                    is_primary=True,
                    province=legacy.province,
                    municipality=legacy.city,
                    sector=legacy.sector,
                    barrio=legacy.barrio,
                    street=street,
                    number=legacy.house_number,
                    building=legacy.building,
                    apartment=legacy.apartment,
                    reference_note=extra,
                    latitude=legacy.latitude,
                    longitude=legacy.longitude,
                )
            )
        for ref in legacy.references or []:
            if isinstance(ref, str):
                try:
                    ref = json.loads(ref)
                except ValueError:
                    continue
            if not isinstance(ref, dict) or not ref.get("nombre"):
                continue
            extras = "; ".join(f"{k}: {ref[k]}" for k in ("cedula", "direccion") if ref.get(k))
            db.add(
                CustomerReference(
                    tenant_id=tenant,
                    customer_id=profile.id,
                    kind="personal",
                    name=collapse_spaces(ref["nombre"]),
                    phone=ref.get("telefono") or None,
                    notes=extras or None,
                )
            )
        db.flush()

        result = dedup.classify(
            db,
            tenant,
            document_type=None,
            document_normalized=None,
            phones=phones,
            emails=emails,
            name_normalized=normalize_name(legacy.full_name),
            birth_date=person.birth_date,
            exclude_person_id=person.id,
        )
        candidates = {c.customer_id: list(c.signals) for c in result.possible}
        if document_taken:
            holder = db.scalar(
                select(CustomerProfile.id)
                .join(Person, Person.id == CustomerProfile.person_id)
                .where(
                    Person.tenant_id == tenant,
                    Person.document_type == LEGACY_DOCUMENT_TYPE,
                    Person.document_number_normalized == doc_norm,
                )
            )
            if holder:
                candidates.setdefault(holder, []).append("document")
        for other, signals in candidates.items():
            db.add(
                CustomerDuplicateFlag(
                    tenant_id=tenant,
                    customer_id=profile.id,
                    candidate_customer_id=other,
                    signals=signals,
                    created_by=legacy.created_by_id,
                )
            )
            stats["possible_duplicates"] += 1
        stats["imported"] += 1
    db.flush()
    return stats
