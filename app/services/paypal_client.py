"""Thin shared PayPal REST helper (base URL + OAuth token)."""
import httpx

from app.core.config import settings

PAYPAL_BASE = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


def base_url() -> str:
    return PAYPAL_BASE.get(settings.paypal_mode, PAYPAL_BASE["sandbox"])


def is_configured() -> bool:
    return bool(settings.paypal_client_id and settings.paypal_secret)


async def get_access_token() -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{base_url()}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(settings.paypal_client_id, settings.paypal_secret),
        )
        r.raise_for_status()
        return r.json()["access_token"]
