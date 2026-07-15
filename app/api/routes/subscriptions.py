from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_db, require_admin, require_superadmin
from app.core.config import settings
from app.models.company import Company
from app.models.plan import Plan
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.subscription import SubscriptionRead
from app.services import paypal_billing
from app.services.paypal_client import base_url, get_access_token, is_configured
from app.services.subscription_service import record_subscription

router = APIRouter()


@router.get("", response_model=list[SubscriptionRead])
def list_subscriptions(
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
) -> list[Subscription]:
    """All subscription payments across every company — the owner's revenue log."""
    statement = (
        select(Subscription)
        .options(selectinload(Subscription.company), selectinload(Subscription.plan))
        .order_by(Subscription.created_at.desc())
    )
    return list(db.scalars(statement).all())

PAYPAL_BASE = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live":    "https://api-m.paypal.com",
}


@router.get("/current")
def current_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Plan actual y vencimiento de la empresa del usuario, para que el dueño
    sepa qué tiene contratado. ``status`` es active | expired | none."""
    if current_user.company_id is None:
        return {"plan_id": None, "plan_name": None, "monthly_price_usd": None,
                "subscription_expires_at": None, "status": "none", "days_remaining": None}

    company = db.get(Company, current_user.company_id)
    if company is None or company.plan_id is None:
        return {"plan_id": None, "plan_name": None, "monthly_price_usd": None,
                "subscription_expires_at": None, "status": "none", "days_remaining": None}

    plan = db.get(Plan, company.plan_id)
    expires = company.subscription_expires_at
    status_str = "active"
    days_remaining: int | None = None
    if expires is not None:
        exp = expires if expires.tzinfo is not None else expires.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        delta = exp - now
        days_remaining = delta.days
        status_str = "active" if exp > now else "expired"

    return {
        "plan_id": company.plan_id,
        "plan_name": plan.name if plan else None,
        "monthly_price_usd": str(plan.monthly_price_usd) if plan else None,
        "subscription_expires_at": expires.isoformat() if expires else None,
        "status": status_str,
        "days_remaining": days_remaining,
        "recurring": company.paypal_subscription_id is not None,
    }


@router.get("/paypal-config")
def paypal_config(_: User = Depends(get_current_user)) -> dict:
    """Public PayPal client config for the web/mobile checkout SDK.

    The client id is a publishable value; the secret never leaves the server.
    ``configured`` lets the clients show a friendly message when PayPal has
    not been set up yet (empty credentials in the environment).
    """
    return {
        "client_id": settings.paypal_client_id,
        "mode": settings.paypal_mode,
        "configured": bool(settings.paypal_client_id and settings.paypal_secret),
        "webhook_configured": bool(settings.paypal_webhook_id),
    }


async def _paypal_token() -> str:
    base = PAYPAL_BASE.get(settings.paypal_mode, PAYPAL_BASE["sandbox"])
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(settings.paypal_client_id, settings.paypal_secret),
        )
        r.raise_for_status()
        return r.json()["access_token"]


class CreateOrderInput(BaseModel):
    plan_id: int


class ConfirmOrderInput(BaseModel):
    order_id: str
    plan_id: int


@router.post("/create-order")
async def create_order(
    payload: CreateOrderInput,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    plan = db.get(Plan, payload.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan no encontrado.")

    base  = PAYPAL_BASE.get(settings.paypal_mode, PAYPAL_BASE["sandbox"])
    token = await _paypal_token()

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/v2/checkout/orders",
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "amount": {"currency_code": "USD", "value": str(plan.monthly_price_usd)},
                    "description": f"MoneyFast — Plan {plan.name}",
                }],
            },
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()

    return {"order_id": data["id"]}


@router.post("/confirm")
async def confirm_order(
    payload: ConfirmOrderInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    from app.models.company import Company

    plan = db.get(Plan, payload.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan no encontrado.")
    if current_user.company_id is None:
        raise HTTPException(status_code=400, detail="El usuario no pertenece a una empresa.")
    company = db.get(Company, current_user.company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Empresa no encontrada.")

    base  = PAYPAL_BASE.get(settings.paypal_mode, PAYPAL_BASE["sandbox"])
    token = await _paypal_token()

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/v2/checkout/orders/{payload.order_id}/capture",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        r.raise_for_status()
        capture = r.json()

    status = capture.get("status")
    if status != "COMPLETED":
        raise HTTPException(status_code=400, detail=f"Pago no completado: {status}")

    subscription = record_subscription(db, company=company, plan=plan, order_id=payload.order_id)
    db.commit()
    db.refresh(subscription)

    return {"status": "activated", "plan_id": payload.plan_id, "subscription_id": subscription.id}


@router.get("/checkout-page", response_class=HTMLResponse)
def checkout_page(
    plan_id: int = Query(...),
    mode: str = Query(default="once"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Self-hosted PayPal checkout page for the mobile WebView.

    ``mode=once`` → one-time order (PayPal or card). ``mode=sub`` → recurring
    monthly subscription (requires the plan's PayPal billing plan id). Served
    from the API origin so the fetch calls below are same-origin (no CORS). The
    auth token is injected by the mobile app into ``window.__TOKEN__`` before
    the page loads; it is never placed in the URL.
    """
    client_id = settings.paypal_client_id or "sb"
    plan = db.get(Plan, plan_id)
    paypal_plan_id = plan.paypal_plan_id if plan else None
    recurring = mode == "sub" and bool(paypal_plan_id)

    sdk_query = (
        f"client-id={client_id}&vault=true&intent=subscription"
        if recurring
        else f"client-id={client_id}&currency=USD&enable-funding=card&intent=capture"
    )
    html = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
  <style>
    body { font-family: -apple-system, Roboto, sans-serif; margin: 0; padding: 20px; background: #fff; color: #1a1a1a; }
    #status { text-align: center; color: #666; margin-top: 16px; font-size: 14px; }
    h3 { text-align: center; }
  </style>
</head>
<body>
  <h3>__TITLE__</h3>
  <div id="paypal-button-container"></div>
  <div id="status">Cargando PayPal…</div>
  <script src="https://www.paypal.com/sdk/js?__SDK_QUERY__"></script>
  <script>
    var PLAN_ID = __PLAN_ID__;
    var PAYPAL_PLAN_ID = "__PAYPAL_PLAN_ID__";
    var RECURRING = __RECURRING__;
    var TOKEN = window.__TOKEN__ || '';
    var statusEl = document.getElementById('status');
    function notify(msg) { if (window.ReactNativeWebView) window.ReactNativeWebView.postMessage(JSON.stringify(msg)); }
    function authHeaders() { return { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN }; }

    if (!window.paypal) {
      statusEl.textContent = 'No se pudo cargar PayPal. Revisa tu conexión.';
      notify({ type: 'error', message: 'sdk_failed' });
    } else {
      statusEl.textContent = '';
      var cfg = {
        style: { layout: 'vertical', shape: 'rect', label: RECURRING ? 'subscribe' : 'pay' },
        onCancel: function () { notify({ type: 'cancel' }); },
        onError: function (err) { statusEl.textContent = 'Error con PayPal.'; notify({ type: 'error', message: String(err) }); }
      };
      if (RECURRING) {
        cfg.createSubscription = function (data, actions) {
          return actions.subscription.create({ plan_id: PAYPAL_PLAN_ID });
        };
        cfg.onApprove = function (data) {
          statusEl.textContent = 'Activando suscripción…';
          return fetch('/api/v1/subscriptions/activate-recurring', {
            method: 'POST', headers: authHeaders(),
            body: JSON.stringify({ subscription_id: data.subscriptionID, plan_id: PLAN_ID })
          }).then(function (r) { if (!r.ok) throw new Error('activate_failed'); return r.json(); })
            .then(function () { notify({ type: 'success' }); });
        };
      } else {
        cfg.createOrder = function () {
          return fetch('/api/v1/subscriptions/create-order', {
            method: 'POST', headers: authHeaders(), body: JSON.stringify({ plan_id: PLAN_ID })
          }).then(function (r) { return r.json(); }).then(function (d) {
            if (!d.order_id) throw new Error('no_order');
            return d.order_id;
          });
        };
        cfg.onApprove = function (data) {
          statusEl.textContent = 'Confirmando pago…';
          return fetch('/api/v1/subscriptions/confirm', {
            method: 'POST', headers: authHeaders(),
            body: JSON.stringify({ order_id: data.orderID, plan_id: PLAN_ID })
          }).then(function (r) { if (!r.ok) throw new Error('confirm_failed'); return r.json(); })
            .then(function () { notify({ type: 'success' }); });
        };
      }
      paypal.Buttons(cfg).render('#paypal-button-container');
    }
  </script>
</body>
</html>"""
    html = (
        html.replace("__SDK_QUERY__", sdk_query)
        .replace("__TITLE__", "Suscripción mensual" if recurring else "Pago de suscripción")
        .replace("__PAYPAL_PLAN_ID__", paypal_plan_id or "")
        .replace("__RECURRING__", "true" if recurring else "false")
        .replace("__PLAN_ID__", str(plan_id))
    )
    return HTMLResponse(content=html)


# ─── Recurring subscriptions (PayPal Subscriptions API) ──────────────────────


class ActivateRecurringInput(BaseModel):
    subscription_id: str
    plan_id: int


@router.post("/setup-billing")
async def setup_billing(
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
) -> dict:
    """Create the PayPal product + a monthly billing plan per paid plan.

    Idempotent — safe to call again; only missing pieces are created. Run once
    (as the platform owner) after configuring PayPal, or after adding a plan.
    """
    if not is_configured():
        raise HTTPException(status_code=400, detail="PayPal no está configurado.")
    mapping = await paypal_billing.ensure_billing(db)
    return {"status": "ok", "plans": mapping}


@router.post("/activate-recurring")
async def activate_recurring(
    payload: ActivateRecurringInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> dict:
    """Confirm a PayPal recurring subscription the buyer just approved, and
    activate the plan for the company."""
    plan = db.get(Plan, payload.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan no encontrado.")
    if current_user.company_id is None:
        raise HTTPException(status_code=400, detail="El usuario no pertenece a una empresa.")
    company = db.get(Company, current_user.company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Empresa no encontrada.")

    token = await get_access_token()
    sub = await paypal_billing.get_subscription(token, payload.subscription_id)
    if sub.get("status") not in {"ACTIVE", "APPROVED"}:
        raise HTTPException(status_code=400, detail=f"Suscripción no activa: {sub.get('status')}")

    company.paypal_subscription_id = payload.subscription_id
    record_subscription(db, company=company, plan=plan, order_id=payload.subscription_id)
    db.commit()
    return {"status": "activated", "plan_id": plan.id, "recurring": True}


@router.post("/cancel-recurring")
async def cancel_recurring(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> dict:
    """Cancel the company's recurring subscription. The current period stays
    valid until it expires (then #1 downgrades to the free plan)."""
    if current_user.company_id is None:
        raise HTTPException(status_code=400, detail="El usuario no pertenece a una empresa.")
    company = db.get(Company, current_user.company_id)
    if company is None or not company.paypal_subscription_id:
        raise HTTPException(status_code=404, detail="No hay una suscripción recurrente activa.")

    token = await get_access_token()
    await paypal_billing.cancel_subscription(token, company.paypal_subscription_id)
    company.paypal_subscription_id = None
    db.commit()
    return {"status": "cancelled"}


@router.post("/paypal-webhook")
async def paypal_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """Receive PayPal subscription events to keep billing in sync.

    Renewals (PAYMENT.SALE.COMPLETED) extend the company's expiry by a month;
    cancellations/expirations clear the recurring id so the plan lapses to free.
    Requires a public URL in production — PayPal cannot reach localhost.
    """
    event = await request.json()

    # Verify the webhook signature when a webhook id is configured.
    if settings.paypal_webhook_id:
        verified = await _verify_webhook(request.headers, event)
        if not verified:
            raise HTTPException(status_code=400, detail="Firma de webhook inválida.")

    event_type = event.get("event_type", "")
    resource = event.get("resource", {}) or {}

    def _company_by_sub(sub_id: str | None) -> Company | None:
        if not sub_id:
            return None
        return db.scalar(select(Company).where(Company.paypal_subscription_id == sub_id))

    if event_type == "PAYMENT.SALE.COMPLETED":
        # Recurring charge succeeded — record it and roll the expiry forward.
        sub_id = resource.get("billing_agreement_id")
        company = _company_by_sub(sub_id)
        if company is not None and company.plan_id is not None:
            plan = db.get(Plan, company.plan_id)
            if plan is not None:
                record_subscription(db, company=company, plan=plan, order_id=sub_id)
                db.commit()
    elif event_type in {"BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED",
                        "BILLING.SUBSCRIPTION.SUSPENDED"}:
        company = _company_by_sub(resource.get("id"))
        if company is not None:
            company.paypal_subscription_id = None
            db.commit()

    return {"status": "ok"}


async def _verify_webhook(headers, event: dict) -> bool:
    payload = {
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "cert_url": headers.get("paypal-cert-url"),
        "auth_algo": headers.get("paypal-auth-algo"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "webhook_id": settings.paypal_webhook_id,
        "webhook_event": event,
    }
    try:
        token = await get_access_token()
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{base_url()}/v1/notifications/verify-webhook-signature",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            return r.json().get("verification_status") == "SUCCESS"
    except Exception:
        return False
