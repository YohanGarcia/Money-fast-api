import base64
import binascii
from datetime import UTC, date, datetime
from urllib.parse import quote
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db, require_admin_manager
from app.models.customer import Customer
from app.models.loan_application import LoanApplication, ApplicationDocument
from app.models.user import User
from app.schemas.loan_application import (ApplicationInput, TransitionInput, DocumentInput,
    ReviewInput, CustomerLinkInput, SECTIONS, DOCUMENTS, validate_complete)
from app.schemas.loan import LoanCreate
from app.services.loan_service import create_loan
from app.services.plan_limits import enforce_can_create
from app.services.customer_profile import sync_application_customer, document_key, check_identity

router = APIRouter()


def get_application(db, user, application_id):
    item = db.scalar(select(LoanApplication).where(LoanApplication.id == application_id,
                      LoanApplication.company_id == user.company_id))
    if item is None:
        raise HTTPException(404, "Solicitud no encontrada.")
    return item


def customer_for(db, user, customer_id):
    customer = db.scalar(select(Customer).where(Customer.id == customer_id, Customer.company_id == user.company_id))
    if customer is None:
        raise HTTPException(404, "Cliente no encontrado en esta empresa.")
    return customer


def check_version(item, version):
    if item.version != version:
        raise HTTPException(409, "La solicitud cambió. Recarga antes de continuar.")


def record(item, user, action, notes=""):
    item.history = [*item.history, dict(action=action, user=user.full_name, user_id=user.id,
        at=datetime.now(UTC).isoformat(), notes=notes)]


def save(db, item):
    try:
        db.commit()
    except (StaleDataError, IntegrityError):
        db.rollback()
        raise HTTPException(409, "La solicitud cambió. Recarga antes de continuar.")
    db.refresh(item)
    return serialize(item)


def serialize(item):
    return dict(id=item.id, modality=item.modality, status=item.status, data=item.data,
        terms=item.terms, customer_id=item.customer_id, customer_version=item.customer_version, loan_id=item.loan_id,
        version=item.version, created_at=item.created_at, history=item.history,
        documents=[dict(id=d.id, category=d.category, filename=d.filename, size=d.size,
                       verified=d.verified, media_type=d.media_type) for d in item.documents])


@router.get("/form")
def form(_: User = Depends(require_admin_manager)):
    return dict(sections=SECTIONS, documents=DOCUMENTS)


@router.get("")
def listing(customer_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(require_admin_manager)):
    statement = select(LoanApplication).where(LoanApplication.company_id == user.company_id)
    if customer_id is not None:
        customer_for(db, user, customer_id)
        statement = statement.where(LoanApplication.customer_id == customer_id)
    rows = db.scalars(statement.order_by(LoanApplication.id.desc())).all()
    return [serialize(row) for row in rows]


@router.post("", status_code=201)
def create(payload: ApplicationInput, db: Session = Depends(get_db), user: User = Depends(require_admin_manager)):
    if user.company_id is None:
        raise HTTPException(403, "Se requiere una empresa.")
    item = LoanApplication(company_id=user.company_id, created_by_id=user.id,
        modality=payload.modality, data=payload.data, history=[])
    sync_application_customer(db, user, payload, item)
    record(item, user, "created")
    db.add(item)
    return save(db, item)


@router.get("/{application_id}")
def detail(application_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin_manager)):
    return serialize(get_application(db, user, application_id))


@router.get("/{application_id}/print")
def print_application(application_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin_manager)):
    from app.models.company import Company
    item = get_application(db, user, application_id)
    company = db.get(Company, item.company_id)
    parts = []
    index = 0
    for section in SECTIONS:
        if section.get("secured") and item.modality != "secured":
            continue
        index += 1
        fields = []
        for f in section["fields"]:
            if f.get("when") and item.data.get(f["when"]) != f["equals"]:
                continue
            value = item.data.get(f["key"], "")
            if f["kind"] == "checkbox":
                value = "Aceptada" if value else "Pendiente"
            fields.append(f'<div><b>{escape(f["label"])}</b><p>{escape(str(value)) or "________________"}</p></div>')
        parts.append(f'<section><h2>{index}. {escape(section["title"])}</h2><div class="grid">{"".join(fields)}</div></section>')
    modality = "CON GARANTÍA" if item.modality == "secured" else "SIN GARANTÍA"
    html = f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Solicitud #{item.id}</title>
    <style>@page{{size:A4;margin:14mm}}body{{font:12px Arial,sans-serif;color:#17334f}}h1{{font-size:20px}}h2{{font-size:13px;background:#edf3fa;padding:7px}}section{{break-inside:avoid;margin-bottom:10px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px 20px}}b{{font-size:10px}}p{{margin:5px 0;white-space:pre-wrap;overflow-wrap:anywhere}}.signature{{margin-top:45px;border-top:1px solid #333;padding-top:8px;width:70%}}</style></head><body>
    <p>{escape(company.name if company else "MoneyFast")}</p><h1>SOLICITUD DE PRÉSTAMO {modality}</h1>
    <p>Expediente #{item.id}{" · BORRADOR" if item.status == "draft" else ""}</p>{"".join(parts)}
    <section><p class="signature">Firma del solicitante</p><p>Fecha: __________________</p></section></body></html>'''
    return {"html": html}


@router.post("/{application_id}/customer")
def link_legacy_customer(application_id: int, payload: CustomerLinkInput,
                         db: Session = Depends(get_db), user: User = Depends(require_admin_manager)):
    item = get_application(db, user, application_id)
    check_version(item, payload.version)
    if item.customer_id or item.status not in ("receiving", "evaluation", "approved", "signed"):
        raise HTTPException(409, "Solo se pueden vincular solicitudes históricas en trámite sin cliente.")
    customer = customer_for(db, user, payload.customer_id)
    if customer.version != payload.customer_version:
        raise HTTPException(409, "La ficha cambió. Vuelve a seleccionar al cliente.")
    if not document_key(customer.document_id) or document_key(customer.document_id) != document_key(item.data.get("document_id")):
        raise HTTPException(422, "La cédula del cliente debe coincidir con la solicitud histórica.")
    item.customer_id, item.customer_version = customer.id, customer.version
    record(item, user, "customer_linked", "Cliente vinculado sin modificar los datos históricos.")
    return save(db, item)


@router.put("/{application_id}")
def edit(application_id: int, payload: ApplicationInput, db: Session = Depends(get_db), user: User = Depends(require_admin_manager)):
    item = get_application(db, user, application_id)
    check_version(item, payload.version)
    if item.status != "draft":
        raise HTTPException(409, "Devuelve la solicitud a borrador antes de modificar los datos.")
    sync_application_customer(db, user, payload, item)
    if payload.modality != item.modality:
        for document in list(item.documents):
            if document.category in {d["key"] for d in DOCUMENTS if d.get("secured")}:
                item.documents.remove(document)
    item.modality, item.data = payload.modality, payload.data
    # A changed application must be signed and checked again.
    for document in item.documents:
        document.verified = False
    record(item, user, "edited")
    return save(db, item)


@router.post("/{application_id}/documents/{category}")
def upload(application_id: int, category: str, payload: DocumentInput,
           db: Session = Depends(get_db), user: User = Depends(require_admin_manager)):
    item = get_application(db, user, application_id)
    check_version(item, payload.version)
    allowed = {d["key"] for d in DOCUMENTS if not d.get("secured") or item.modality == "secured"}
    if category not in allowed or (category == "contract" and item.status != "approved") or (category != "contract" and item.status not in ("draft", "receiving")):
        raise HTTPException(409, "Este documento no se puede adjuntar en esta etapa.")
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, "Archivo inválido.")
    if not content or len(content) > 5 * 1024 * 1024:
        raise HTTPException(422, "El archivo debe tener entre 1 byte y 5 MB.")
    media = "application/pdf" if content.startswith(b"%PDF-") else "image/png" if content.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if content.startswith(b"\xff\xd8\xff") else None
    if media is None:
        raise HTTPException(422, "Solo se admiten documentos PDF, PNG o JPEG.")
    previous = next((d for d in item.documents if d.category == category), None)
    if previous:
        item.documents.remove(previous)
    item.documents.append(ApplicationDocument(category=category, filename=payload.filename,
        content=content, size=len(content), media_type=media, verified=False))
    record(item, user, "document_uploaded", category)
    return save(db, item)


@router.get("/{application_id}/documents/{document_id}/content")
def download(application_id: int, document_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin_manager)):
    item = get_application(db, user, application_id)
    document = next((d for d in item.documents if d.id == document_id), None)
    if document is None:
        raise HTTPException(404, "Documento no encontrado.")
    return Response(document.content, media_type=document.media_type, headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(document.filename),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post("/{application_id}/documents/{document_id}/review")
def review(application_id: int, document_id: int, payload: ReviewInput,
           db: Session = Depends(get_db), user: User = Depends(require_admin_manager)):
    item = get_application(db, user, application_id)
    check_version(item, payload.version)
    doc = next((d for d in item.documents if d.id == document_id), None)
    if doc is None:
        raise HTTPException(404, "Documento no encontrado.")
    if item.status not in ("draft", "receiving") and not (item.status == "approved" and doc.category == "contract"):
        raise HTTPException(409, "La revisión documental está cerrada en esta etapa.")
    doc.verified = payload.verified
    record(item, user, "document_verified" if payload.verified else "document_unverified", doc.category)
    return save(db, item)


@router.post("/{application_id}/transition")
def transition(application_id: int, payload: TransitionInput,
               db: Session = Depends(get_db), user: User = Depends(require_admin_manager)):
    item = get_application(db, user, application_id)
    check_version(item, payload.version)
    transitions = {"submit": ("draft", "receiving"), "evaluate": ("receiving", "evaluation"),
                   "approve": ("evaluation", "approved"), "sign": ("approved", "signed"),
                   "disburse": ("signed", "disbursed")}
    action = payload.action
    if action in ("return", "reject"):
        if item.status not in ("receiving", "evaluation", "approved") or not payload.notes.strip():
            raise HTTPException(409, "Indica el motivo; solo se puede devolver o rechazar antes de la firma.")
        item.status = "draft" if action == "return" else "rejected"
        item.terms = {}
        for doc in list(item.documents):
            if doc.category == "contract":
                item.documents.remove(doc)
    else:
        expected, target = transitions[action]
        if item.status != expected:
            raise HTTPException(409, "La acción no corresponde a la etapa actual.")
        if action == "submit" and not item.customer_id:
            raise HTTPException(422, "Vincula el cliente antes de presentar la solicitud.")
        try:
            validate_complete(item)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if action == "evaluate":
            required = {d["key"] for d in DOCUMENTS if d["required"] and (not d.get("secured") or item.modality == "secured")}
            verified = {d.category for d in item.documents if d.verified}
            missing = required - verified
            if missing:
                labels = [d["label"] for d in DOCUMENTS if d["key"] in missing]
                raise HTTPException(
                    422,
                    "Faltan documentos por adjuntar y verificar: " + ", ".join(labels) + ".",
                )
        if action == "approve":
            if payload.terms is None or not payload.notes.strip():
                raise HTTPException(422, "Registra la evaluación y las condiciones aprobadas.")
            item.terms = payload.terms.model_dump(mode="json")
        if action == "sign" and not any(d.category == "contract" and d.verified for d in item.documents):
            raise HTTPException(422, "Adjunta y verifica el contrato y pagaré firmados.")
        if action == "disburse":
            from app.services.cash_service import enabled, lock_company
            lock_company(db, user.company_id)
            if enabled(db, user.company_id):
                raise HTTPException(409, "Con Caja habilitada, registra el desembolso desde Caja.")
            if not payload.first_payment_date or payload.first_payment_date < date.today() or not payload.disbursement_reference.strip():
                raise HTTPException(422, "Indica la referencia del desembolso y una primera fecha de pago desde hoy.")
            enforce_can_create(db, user.company_id, "loan")
            if item.customer_id:
                customer = customer_for(db, user, item.customer_id)
            else:
                matches = [c for c in db.scalars(select(Customer).where(Customer.company_id == user.company_id)).all()
                           if document_key(c.document_id) == document_key(item.data["document_id"])]
                if len(matches) > 1:
                    raise HTTPException(409, "Hay varios clientes históricos con esta cédula. Selecciona y vincula la ficha correcta antes de desembolsar.")
                customer = matches[0] if matches else None
                if customer is None:
                    enforce_can_create(db, user.company_id, "customer")
                    customer = Customer(company_id=user.company_id, created_by_id=user.id,
                        full_name=item.data["full_name"], document_id=item.data["document_id"],
                        document_key=check_identity(db, user.company_id, item.data["document_id"]),
                        phone=item.data["phone"], address=item.data["address"])
                    db.add(customer)
                    db.flush()
            frequency = {"Semanal": "weekly", "Quincenal": "biweekly", "Mensual": "monthly"}[item.data["payment_frequency"]]
            loan = create_loan(db, LoanCreate(customer_id=customer.id, principal_amount=item.data["requested_amount"],
                payment_frequency=frequency, start_date=payload.first_payment_date,
                auto_approve=True, requires_promissory_note=True, **item.terms), user.id)
            item.customer_id, item.loan_id = customer.id, loan.id
            item.customer_version = customer.version
            item.terms = {**item.terms, "disbursement_reference": payload.disbursement_reference,
                          "disbursement_date": date.today().isoformat(), "first_payment_date": str(payload.first_payment_date)}
        item.status = target
    record(item, user, action, payload.notes.strip())
    return save(db, item)
