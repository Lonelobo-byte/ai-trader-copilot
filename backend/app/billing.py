"""Provider boundary for crypto checkout; entitlement logic remains internal."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from base64 import b64encode
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_UP
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
_MINIMUM_CACHE: dict[str, tuple[float, float | None]] = {}
_MINIMUM_CACHE_IDENTITY = ""
_MINIMUM_CACHE_LOCK = asyncio.Lock()
_PROVIDER_CACHE_SECONDS = 300
_LIVE_NOWPAYMENTS_API_URL = "https://api.nowpayments.io/v1"
_SANDBOX_NOWPAYMENTS_API_URL = "https://api-sandbox.nowpayments.io/v1"


class PaymentProviderError(RuntimeError):
    """A safe, actionable error returned by the payment provider."""


class PaymentProviderBusyError(PaymentProviderError):
    """The provider is healthy but temporarily throttling requests."""


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


def _provider_cache_identity() -> str:
    """Separate cached merchant data by environment, currency, and API key."""
    settings = get_settings()
    key_fingerprint = hashlib.sha256(settings.nowpayments_api_key.encode()).hexdigest()[:16]
    return f"{nowpayments_api_base_url()}:{settings.billing_currency.lower()}:{key_fingerprint}"


async def fetch_nowpayments_currencies() -> list[dict[str, str]]:
    """Return only currencies enabled in this merchant's Coin Settings.

    ``/currencies`` and ``/full-currencies`` describe NOWPayments' global
    catalog, not the coins enabled by this merchant. ``/merchant/coins`` is
    the source of truth for the checkout picker on both live and sandbox.
    """
    global _CURRENCY_CACHE
    api_url = nowpayments_api_base_url()
    cache_identity = _provider_cache_identity()
    if _CURRENCY_CACHE and _CURRENCY_CACHE[0] == cache_identity and monotonic() - _CURRENCY_CACHE[1] < _PROVIDER_CACHE_SECONDS:
        return _CURRENCY_CACHE[2]
    settings = get_settings()
    if settings.payment_provider != "nowpayments" or not settings.nowpayments_api_key:
        raise RuntimeError("Crypto checkout is not configured.")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{api_url}/merchant/coins",
                headers={"x-api-key": settings.nowpayments_api_key},
            )
            metadata_response = await client.get(
                f"{api_url}/full-currencies",
                headers={"x-api-key": settings.nowpayments_api_key},
            )
    except httpx.HTTPError as exc:
        raise PaymentProviderError("NOWPayments merchant currency lookup is temporarily unavailable.") from exc
    if response.is_error:
        if response.status_code == 429:
            raise PaymentProviderBusyError("NOWPayments is temporarily rate limiting currency lookups.")
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
    _CURRENCY_CACHE = (cache_identity, monotonic(), currencies)
    return currencies


def _minimum_lookup_error(response: httpx.Response) -> PaymentProviderError:
    return PaymentProviderError(
        f"NOWPayments minimum amount lookup failed ({response.status_code}): {response.text[:300]}"
    )


async def fetch_nowpayments_minimum_amount(currency_code: str, *, force_refresh: bool = False) -> float:
    """Return the fiat minimum for one selected asset without provider fan-out."""
    global _MINIMUM_CACHE_IDENTITY
    settings = get_settings()
    if settings.payment_provider != "nowpayments" or not settings.nowpayments_api_key:
        raise RuntimeError("Crypto checkout is not configured.")
    code = str(currency_code).strip().lower()
    if not code:
        raise ValueError("Choose a payment currency.")
    identity = _provider_cache_identity()

    async with _MINIMUM_CACHE_LOCK:
        if _MINIMUM_CACHE_IDENTITY != identity:
            _MINIMUM_CACHE.clear()
            _MINIMUM_CACHE_IDENTITY = identity
        if force_refresh:
            _MINIMUM_CACHE.pop(code, None)

        now = monotonic()
        cached = _MINIMUM_CACHE.get(code)
        if cached is not None and now - cached[0] < _PROVIDER_CACHE_SECONDS and cached[1] is not None:
            return float(cached[1])

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{nowpayments_api_base_url()}/min-amount",
                    params={
                        "currency_from": settings.billing_currency.lower(),
                        "currency_to": code,
                        "fiat_equivalent": settings.billing_currency.lower(),
                        "is_fixed_rate": "true",
                        "is_fee_paid_by_user": "false",
                    },
                    headers={"x-api-key": settings.nowpayments_api_key},
                )
        except httpx.HTTPError as exc:
            raise PaymentProviderError(
                f"NOWPayments minimum amount lookup failed for {code.upper()}: {exc.__class__.__name__}."
            ) from exc
        if response.is_error:
            if response.status_code == 429:
                raise PaymentProviderBusyError("NOWPayments is temporarily rate limiting payment checks.")
            if 400 <= response.status_code < 500 and response.status_code not in {401, 403}:
                raise ValueError(
                    f"NOWPayments cannot currently route {code.upper()}. Choose another enabled payment asset."
                )
            raise _minimum_lookup_error(response)
        try:
            body = response.json()
            minimum = Decimal(str(body.get("min_amount"))) if isinstance(body, dict) else Decimal("NaN")
        except (ValueError, TypeError, InvalidOperation):
            minimum = Decimal("NaN")
        if not minimum.is_finite() or minimum <= 0:
            raise PaymentProviderError(f"NOWPayments returned an invalid minimum amount for {code.upper()}.")
        value = float(minimum)
        _MINIMUM_CACHE[code] = (monotonic(), value)
        return value


def _minimum_amount_message(pay_currency: str, minimum: float) -> str:
    settings = get_settings()
    rounded_up = Decimal(str(minimum)).quantize(Decimal("0.01"), rounding=ROUND_UP)
    return (
        f"{pay_currency.upper()} currently requires a checkout of at least "
        f"{settings.billing_currency.upper()} {rounded_up:.2f} at NOWPayments. "
        "Choose another compatible payment asset or a higher-value plan."
    )


async def create_nowpayments_payment(order_id: str, plan_code: str, pay_currency: str) -> dict[str, Any]:
    """Create a provider payment route whose address can be rendered in our modal."""
    settings = get_settings()
    if settings.payment_provider != "nowpayments" or not settings.nowpayments_api_key:
        raise RuntimeError("Crypto checkout is not configured.")
    normalized_currency = pay_currency.lower()
    permitted = {item["code"] for item in await fetch_nowpayments_currencies()}
    if normalized_currency not in permitted:
        raise ValueError("Choose one of the available payment currencies.")
    plan = plan_details(plan_code)
    minimum = await fetch_nowpayments_minimum_amount(normalized_currency)
    if Decimal(str(plan["amount"])) < Decimal(str(minimum)):
        raise ValueError(_minimum_amount_message(normalized_currency, minimum))
    base_url = settings.public_base_url.rstrip("/")
    payload = {
        "price_amount": plan["amount"],
        "price_currency": settings.billing_currency,
        "pay_currency": normalized_currency,
        "order_id": order_id,
        "order_description": f"AI Trader Copilot {plan_code} subscription",
        "ipn_callback_url": f"{base_url}/billing/webhooks/nowpayments",
        "is_fixed_rate": True,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{nowpayments_api_base_url()}/payment",
                json=payload,
                headers={"x-api-key": settings.nowpayments_api_key},
            )
            if response.is_error:
                provider_error = response.text.lower()
                if response.status_code == 400 and any(
                    marker in provider_error for marker in ("amount_minimal", "amountto is too small", "less than minimal")
                ):
                    refreshed_minimum = await fetch_nowpayments_minimum_amount(
                        normalized_currency, force_refresh=True
                    )
                    if Decimal(str(plan["amount"])) < Decimal(str(refreshed_minimum)):
                        raise ValueError(_minimum_amount_message(normalized_currency, refreshed_minimum))
                if response.status_code == 429:
                    raise PaymentProviderBusyError("NOWPayments is temporarily rate limiting checkout creation.")
                if response.status_code in {400, 404, 409, 422}:
                    try:
                        error_body = response.json()
                        provider_message = str(error_body.get("message", "")).strip() if isinstance(error_body, dict) else ""
                    except ValueError:
                        provider_message = ""
                    detail = f" Provider response: {provider_message[:180]}" if provider_message else ""
                    raise ValueError(
                        f"NOWPayments rejected the {normalized_currency.upper()} payment route. "
                        f"Choose another enabled asset.{detail}"
                    )
                raise PaymentProviderError(f"NOWPayments payment request failed ({response.status_code}): {response.text[:500]}")
            try:
                payment = response.json()
            except ValueError as exc:
                raise PaymentProviderError("NOWPayments returned an invalid payment response.") from exc
    except httpx.HTTPError as exc:
        raise PaymentProviderError("NOWPayments payment creation is temporarily unavailable.") from exc
    validate_nowpayments_payment_route(payment, normalized_currency)
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
