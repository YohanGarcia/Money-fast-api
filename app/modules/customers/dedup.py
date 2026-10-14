"""Duplicate detection (T-004 §12). Classification only: nothing here merges, rewrites or deletes.

EXACT_MATCH    the same normalised identity document (type + number) already exists in the tenant.
POSSIBLE_MATCH at least one weak-but-meaningful signal matches another customer of the tenant:
               an active phone, an active e-mail, or the same normalised full name + birth date.
NO_MATCH       nothing else. A shared name, address or surname alone is deliberately NOT a signal.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.customers.models import CustomerContact, CustomerProfile
from app.modules.identity.models import Person

EXACT_MATCH = "EXACT_MATCH"
POSSIBLE_MATCH = "POSSIBLE_MATCH"
NO_MATCH = "NO_MATCH"


@dataclass
class Candidate:
    person_id: int
    customer_id: int | None  # None when the person exists but has no customer profile yet
    signals: list[str] = field(default_factory=list)


@dataclass
class DuplicateResult:
    classification: str
    exact: list[Candidate]
    possible: list[Candidate]


def classify(
    db: Session,
    tenant_id: int,
    *,
    document_type: str | None,
    document_normalized: str | None,
    phones: list[str],
    emails: list[str],
    name_normalized: str,
    birth_date: date | None,
    exclude_person_id: int | None = None,
) -> DuplicateResult:
    exact: list[Candidate] = []
    possible: dict[int, Candidate] = {}
    profile_by_person = {
        pid: cid
        for pid, cid in db.execute(
            select(CustomerProfile.person_id, CustomerProfile.id).where(CustomerProfile.tenant_id == tenant_id)
        )
    }

    if document_normalized:
        for person in db.scalars(
            select(Person).where(
                Person.tenant_id == tenant_id,
                Person.document_type == document_type,
                Person.document_number_normalized == document_normalized,
            )
        ):
            if person.id != exclude_person_id:
                exact.append(Candidate(person.id, profile_by_person.get(person.id), ["document"]))
    exact_people = {c.person_id for c in exact}

    def add(person_id: int, signal: str) -> None:
        if person_id == exclude_person_id or person_id in exact_people:
            return
        cand = possible.setdefault(person_id, Candidate(person_id, profile_by_person.get(person_id)))
        if signal not in cand.signals:
            cand.signals.append(signal)

    for ctype, values, signal in (("phone", phones, "phone"), ("mobile", phones, "phone"), ("email", emails, "email")):
        if not values:
            continue
        rows = db.execute(
            select(CustomerProfile.person_id)
            .join(CustomerContact, CustomerContact.customer_id == CustomerProfile.id)
            .where(
                CustomerProfile.tenant_id == tenant_id,
                CustomerContact.type == ctype,
                CustomerContact.status == "active",
                CustomerContact.normalized_value.in_(values),
            )
        ).all()
        for (person_id,) in rows:
            add(person_id, signal)

    if name_normalized and birth_date is not None:
        for (person_id,) in db.execute(
            select(Person.id).where(
                Person.tenant_id == tenant_id, Person.search_name == name_normalized, Person.birth_date == birth_date
            )
        ):
            add(person_id, "name_birth_date")

    candidates = [c for c in possible.values() if c.customer_id is not None]  # only customers are review candidates
    if exact:
        return DuplicateResult(EXACT_MATCH, exact, candidates)
    return DuplicateResult(POSSIBLE_MATCH if candidates else NO_MATCH, [], candidates)
