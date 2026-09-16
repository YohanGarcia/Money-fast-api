"""Shared client identity rules and atomic application/profile updates."""
import json

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.models.customer import Customer
from app.schemas.customer import CustomerCreate, CustomerReference
from app.services.plan_limits import enforce_can_create

PERSONAL_FIELDS = ("full_name", "document_id", "phone", "address", "email",
                   "home_phone", "birth_date", "marital_status", "nationality", "city")


def document_key(value):
    return "".join(c for c in (value or "").upper() if c.isalnum()) or None


def check_identity(db, company_id, document_id, exclude_id=None):
    key = document_key(document_id)
    if key:
        # Also detect historical duplicates, whose migration leaves the key NULL.
        rows = db.scalars(select(Customer).where(Customer.company_id == company_id)).all()
        if any(c.id != exclude_id and document_key(c.document_id) == key for c in rows):
            raise HTTPException(409, "Ya existe un cliente con esta cédula. Búscalo y selecciónalo; no se creó otro registro.")
    return key


def identity_for_update(db, company_id, document_id, customer=None):
    # Preserve pre-existing duplicate identities when editing the same selected
    # person. A new identity or a new customer must still pass the collision check.
    if customer and document_key(document_id) == document_key(customer.document_id):
        return customer.document_key
    return check_identity(db, company_id, document_id, customer.id if customer else None)


def legacy_references(notes):
    try:
        values = json.loads(notes or "[]")
        if not isinstance(values, list):
            return []
        return [CustomerReference.model_validate(r).model_dump() for r in values[:3]]
    except (ValueError, TypeError, ValidationError):
        return []


def commit_customer(db, customer):
    try:
        db.commit()
    except (StaleDataError, IntegrityError):
        db.rollback()
        raise HTTPException(409, "La ficha cambió o la cédula ya está registrada. Recarga antes de continuar.")
    db.refresh(customer)
    return customer


def sync_application_customer(db, user, payload, item):
    if payload.create_customer and payload.customer_id:
        raise HTTPException(422, "Selecciona un cliente existente o crea uno nuevo.")
    if item.customer_id and payload.customer_id != item.customer_id:
        raise HTTPException(409, "No se puede cambiar el titular de una solicitud vinculada.")
    if not payload.customer_id and not payload.create_customer:
        raise HTTPException(422, "Selecciona o registra al cliente antes de guardar la solicitud.")
    customer = None
    if payload.customer_id:
        customer = db.scalar(select(Customer).where(Customer.id == payload.customer_id,
                                                    Customer.company_id == user.company_id))
        if not customer:
            raise HTTPException(404, "Cliente no encontrado en esta empresa.")
        if payload.customer_version != customer.version:
            raise HTTPException(409, "La ficha del cliente cambió. Revisa la ficha actual antes de guardar.")
    # Application personal fields are a snapshot; only this draft updates the master.
    values = {k: payload.data.get(k) or None for k in PERSONAL_FIELDS}
    refs = list(customer.references or []) if customer else []
    updated_refs = []
    for index, prefix in enumerate(("reference", "reference2")):
        name, phone = payload.data.get(prefix + "_name", ""), payload.data.get(prefix + "_phone", "")
        if name or phone:
            previous = refs[index] if index < len(refs) else {}
            updated_refs.append({**previous, "nombre": name, "telefono": phone})
    updated_refs.extend(refs[2:])
    try:
        validated = CustomerCreate(**values, references=updated_refs)
    except ValidationError as exc:
        raise HTTPException(422, "Completa la ficha básica del cliente: " + "; ".join(
            f'{e["loc"][0]}: {e["msg"]}' for e in exc.errors()))
    key = identity_for_update(db, user.company_id, validated.document_id, customer)
    if customer is None:
        enforce_can_create(db, user.company_id, "customer")
        customer = Customer(company_id=user.company_id, created_by_id=user.id)
        db.add(customer)
    for k in PERSONAL_FIELDS:
        setattr(customer, k, getattr(validated, k))
    customer.references = [r.model_dump() for r in validated.references]
    customer.document_key = key
    # Flush within the caller's transaction. A later failure rolls back both records.
    try:
        db.flush()
    except (StaleDataError, IntegrityError):
        db.rollback()
        raise HTTPException(409, "La ficha cambió o la cédula ya está registrada. Revisa la ficha actual.")
    item.customer_id = customer.id
    item.customer_version = customer.version
