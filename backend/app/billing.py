"""Provider boundary for crypto checkout; entitlement logic remains internal."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from .auth import as_utc, utcnow
from .settings import get_settings

PLANS: dict[str, dict[str, Any]] = {
    "monthly": {"amount": 19.0, "days": 31},
    "quarterly": {"amount": 49.0, "days": 93},
    "half_yearly": {"amount": 89.0, "days": 186},
    "annual": {"amount": 159.0, "days": 366},
}


class PaymentProviderError(RuntimeError):
    """A safe, actionable error returned by the payment provider."""


def callback_configuration_error() -> str | None:
    """Return a customer-safe explanation when a provider cannot call us."""
    settings = get_settings()
    parsed = urlparse(settings.public_base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or host in {"localhost", "127.0.0.1", "::1"}:
        # A sandbox invoice is useful for exercising the local checkout UI.
        # It cannot grant access from the redirect: only the signed IPN path
        # can activate a subscription, and a local server is unreachable to
        # the provider. Production/live payments always retain the HTTPS gate.
        if settings.nowpayments_sandbox and settings.app_env.lower() in {"local", "development", "test"}:
            return None
        return "Crypto checkout needs a public HTTPS PUBLIC_BASE_URL before it can receive verified payment updates."
    return None


def plan_details(code: str) -> dict[str, Any]:
    if code not in PLANS:
        raise ValueError("Unknown subscription plan.")
    return PLANS[code]


async def create_nowpayments_invoice(order_id: str, plan_code: str) -> dict[str, Any]:
    settings = get_settings()
    if settings.payment_provider != "nowpayments" or not settings.nowpayments_api_key:
        raise RuntimeError("Crypto checkout is not configured.")
    plan = plan_details(plan_code)
    base_url = settings.public_base_url.rstrip("/")
    payload = {"price_amount": plan["amount"], "price_currency": settings.billing_currency, "order_id": order_id, "order_description": f"AI Trader Copilot {plan_code} subscription", "ipn_callback_url": f"{base_url}/billing/webhooks/nowpayments", "success_url": f"{base_url}/dashboard?payment=success", "cancel_url": f"{base_url}/dashboard?payment=cancelled"}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{settings.nowpayments_api_base_url.rstrip('/')}/invoice", json=payload, headers={"x-api-key": settings.nowpayments_api_key})
        if response.is_error:
            # Do not put the provider response in a client error: it can
            # contain account-specific data.  It is retained in application logs.
            raise PaymentProviderError(f"NOWPayments invoice request failed ({response.status_code}): {response.text[:500]}")
        invoice = response.json()
        if not isinstance(invoice, dict) or not invoice.get("invoice_url") or not invoice.get("id"):
            raise PaymentProviderError("NOWPayments returned an incomplete invoice response.")
        return invoice


async def fetch_nowpayments_payment(payment_id: str) -> dict[str, Any]:
    """Fetch the authoritative provider status for a known payment ID."""
    settings = get_settings()
    if not settings.nowpayments_api_key:
        raise PaymentProviderError("NOWPayments API key is missing.")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{settings.nowpayments_api_base_url.rstrip('/')}/payment/{payment_id}",
            headers={"x-api-key": settings.nowpayments_api_key},
        )
    if response.is_error:
        raise PaymentProviderError(f"NOWPayments status request failed ({response.status_code}).")
    payload = response.json()
    if not isinstance(payload, dict) or str(payload.get("payment_id", "")) != str(payment_id):
        raise PaymentProviderError("NOWPayments returned an invalid payment status response.")
    return payload


def verify_nowpayments_ipn(raw: bytes, signature: str | None) -> dict[str, Any]:
    secret = get_settings().nowpayments_ipn_secret
    expected = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest() if secret else ""
    if not signature or not hmac.compare_digest(expected, signature):
        raise ValueError("Webhook signature is invalid.")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Webhook payload is invalid.")
    return payload


def complete_subscription(subscription: Any) -> None:
    plan = plan_details(subscription.plan_code)
    now = utcnow()
    existing_end = as_utc(subscription.ends_at)
    base = existing_end if existing_end and existing_end > now else now
    subscription.status, subscription.starts_at, subscription.ends_at, subscription.grace_ends_at = "active", subscription.starts_at or now, base + timedelta(days=plan["days"]), None
