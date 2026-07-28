"""Provider boundary for crypto checkout; entitlement logic remains internal."""
from __future__ import annotations

import hashlib
import hmac
import json
from base64 import b64encode
from datetime import timedelta
from io import BytesIO
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import qrcode

from .auth import as_utc, utcnow
from .settings import get_settings

PLANS: dict[str, dict[str, Any]] = {
    "monthly": {"amount": 5.99, "days": 30, "list_amount": 5.99},
    "quarterly": {"amount": 15.99, "days": 90, "list_amount": 17.97},
    "half_yearly": {"amount": 29.99, "days": 180, "list_amount": 35.94},
    "annual": {"amount": 55.99, "days": 365, "list_amount": 71.88},
}
_CURRENCY_CACHE: tuple[str, float, list[dict[str, str]]] | None = None
_LIVE_NOWPAYMENTS_API_URL = "https://api.nowpayments.io/v1"
_SANDBOX_NOWPAYMENTS_API_URL = "https://api-sandbox.nowpayments.io/v1"


class PaymentProviderError(RuntimeError):
    """A safe, actionable error returned by the payment provider."""


def nowpayments_api_base_url() -> str:
    """Use the sandbox API automatically unless a custom endpoint was supplied."""
    settings = get_settings()
    if settings.nowpayments_sandbox and settings.nowpayments_api_base_url.rstrip("/") == _LIVE_NOWPAYMENTS_API_URL:
        return _SANDBOX_NOWPAYMENTS_API_URL
    return settings.nowpayments_api_base_url.rstrip("/")


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


def _currency_label(code: str) -> str:
    labels = {
        "usdttrc20": "USDT · TRON",
        "usdtbsc": "USDT · BNB Smart Chain",
        "usdterc20": "USDT · Ethereum",
        "btc": "Bitcoin",
        "eth": "Ethereum",
        "sol": "Solana",
    }
    return labels.get(code, code.upper())


def _nowpayments_logo_url(value: Any) -> str | None:
    """Convert a provider logo path into a safe, usable public image URL."""
    if not isinstance(value, str) or not value.strip():
        return None
    return urljoin("https://nowpayments.io", value.strip())


async def fetch_nowpayments_currencies() -> list[dict[str, str]]:
    """Return only currencies enabled in this merchant's Coin Settings.

    ``/currencies`` and ``/full-currencies`` describe NOWPayments' global
    catalog, not the coins enabled by this merchant. ``/merchant/coins`` is
    the source of truth for the checkout picker on both live and sandbox.
    """
    global _CURRENCY_CACHE
    api_url = nowpayments_api_base_url()
    if _CURRENCY_CACHE and _CURRENCY_CACHE[0] == api_url and monotonic() - _CURRENCY_CACHE[1] < 300:
        return _CURRENCY_CACHE[2]
    settings = get_settings()
    if settings.payment_provider != "nowpayments" or not settings.nowpayments_api_key:
        raise RuntimeError("Crypto checkout is not configured.")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{api_url}/merchant/coins",
            headers={"x-api-key": settings.nowpayments_api_key},
        )
        metadata_response = await client.get(
            f"{api_url}/full-currencies",
            headers={"x-api-key": settings.nowpayments_api_key},
        )
    if response.is_error:
        raise PaymentProviderError(f"NOWPayments merchant currency lookup failed ({response.status_code}).")
    body = response.json()
    raw_codes = body.get("selectedCurrencies", []) if isinstance(body, dict) else None
    if not isinstance(raw_codes, list):
        raise PaymentProviderError("NOWPayments returned an invalid merchant currency catalog.")
    codes = sorted({str(code).strip().lower() for code in raw_codes if isinstance(code, str) and code.strip()})
    metadata_rows: list[Any] = []
    if not metadata_response.is_error:
        try:
            metadata_body = metadata_response.json()
            metadata_rows = metadata_body.get("currencies", metadata_body) if isinstance(metadata_body, dict) else metadata_body
        except ValueError:
            metadata_rows = []
    details_by_code = {
        str(item.get("code", "")).strip().lower(): item
        for item in metadata_rows
        if isinstance(item, dict) and str(item.get("code", "")).strip()
    } if isinstance(metadata_rows, list) else {}
    currencies = []
    for code in codes:
        details = details_by_code.get(code, {})
        name = str(details.get("name") or _currency_label(code)).strip()
        ticker = str(details.get("ticker") or code).strip().upper()
        network = str(details.get("network") or "").strip().upper()
        currencies.append({
            "code": code,
            "label": _currency_label(code),
            "name": name,
            "ticker": ticker,
            "network": network,
            "logo_url": _nowpayments_logo_url(details.get("logo_url")),
        })
    if not currencies:
        raise PaymentProviderError("NOWPayments currently has no available payment currencies.")
    _CURRENCY_CACHE = (api_url, monotonic(), currencies)
    return currencies


async def create_nowpayments_payment(order_id: str, plan_code: str, pay_currency: str) -> dict[str, Any]:
    """Create a provider payment route whose address can be rendered in our modal."""
    settings = get_settings()
    if settings.payment_provider != "nowpayments" or not settings.nowpayments_api_key:
        raise RuntimeError("Crypto checkout is not configured.")
    permitted = {item["code"] for item in await fetch_nowpayments_currencies()}
    if pay_currency.lower() not in permitted:
        raise ValueError("Choose one of the available payment currencies.")
    plan = plan_details(plan_code)
    base_url = settings.public_base_url.rstrip("/")
    payload = {
        "price_amount": plan["amount"],
        "price_currency": settings.billing_currency,
        "pay_currency": pay_currency.lower(),
        "order_id": order_id,
        "order_description": f"AI Trader Copilot {plan_code} subscription",
        "ipn_callback_url": f"{base_url}/billing/webhooks/nowpayments",
        "is_fixed_rate": True,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{nowpayments_api_base_url()}/payment",
            json=payload,
            headers={"x-api-key": settings.nowpayments_api_key},
        )
        if response.is_error:
            raise PaymentProviderError(f"NOWPayments payment request failed ({response.status_code}): {response.text[:500]}")
        payment = response.json()
    validate_nowpayments_payment_route(payment, pay_currency)
    return payment


def validate_nowpayments_payment_route(payment: Any, requested_currency: str) -> None:
    """Never present provider instructions for a currency other than selected."""
    required = ("payment_id", "payment_status", "pay_address", "pay_amount", "pay_currency")
    if not isinstance(payment, dict) or any(payment.get(key) in (None, "") for key in required):
        raise PaymentProviderError("NOWPayments returned incomplete payment instructions.")
    if str(payment["pay_currency"]).lower() != requested_currency.lower():
        raise PaymentProviderError(
            "NOWPayments did not return the token/network you selected. No payment instructions were shown; choose another route and try again."
        )


def payment_qr_data_uri(payment_address: str) -> str:
    """Create the QR in-process; a payment address never leaves our domain."""
    image = qrcode.make(payment_address)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + b64encode(buffer.getvalue()).decode("ascii")


async def fetch_nowpayments_payment(payment_id: str) -> dict[str, Any]:
    """Fetch the authoritative provider status for a known payment ID."""
    settings = get_settings()
    if not settings.nowpayments_api_key:
        raise PaymentProviderError("NOWPayments API key is missing.")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{nowpayments_api_base_url()}/payment/{payment_id}",
            headers={"x-api-key": settings.nowpayments_api_key},
        )
    if response.is_error:
        raise PaymentProviderError(f"NOWPayments status request failed ({response.status_code}).")
    payload = response.json()
    if not isinstance(payload, dict) or str(payload.get("payment_id", "")) != str(payment_id):
        raise PaymentProviderError("NOWPayments returned an invalid payment status response.")
    return payload


async def fetch_nowpayments_invoice_payments(invoice_id: str) -> list[dict[str, Any]]:
    """Find provider payments created from one hosted NOWPayments invoice.

    A hosted invoice has its own ``id``.  The customer only receives a
    ``payment_id`` after choosing a coin/payment route, so invoice lookup is
    the safe way to reconcile an older checkout that has not sent us an IPN.
    """
    settings = get_settings()
    if not settings.nowpayments_api_key:
        raise PaymentProviderError("NOWPayments API key is missing.")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{nowpayments_api_base_url()}/payment/",
            # NOWPayments documents this query name in lowercase.  Query keys
            # are case-sensitive at the provider edge.
            params={"invoiceid": invoice_id, "limit": 50, "page": 0, "sortBy": "created_at", "orderBy": "desc"},
            headers={"x-api-key": settings.nowpayments_api_key},
        )
    if response.is_error:
        raise PaymentProviderError(f"NOWPayments invoice lookup failed ({response.status_code}).")
    payload = response.json()
    records = payload.get("data", payload.get("payments", [])) if isinstance(payload, dict) else payload
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise PaymentProviderError("NOWPayments returned an invalid invoice payment list.")
    return records


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
