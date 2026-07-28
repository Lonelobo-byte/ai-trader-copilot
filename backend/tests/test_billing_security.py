"""Focused tests for subscription payment safety rules."""
from __future__ import annotations

from datetime import timedelta
import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import billing
from app.auth import utcnow
from app.billing import PaymentProviderError, callback_configuration_error, nowpayments_api_base_url, payment_qr_data_uri, validate_nowpayments_payment_route
from app.db.models import Payment, Subscription
from app.routes.billing import _apply_provider_status, _checkout_view, _pick_subscription, _reconcile_pending_payment
from app.settings import get_settings


def test_checkout_requires_a_public_https_callback_url() -> None:
    settings = get_settings()
    original = settings.public_base_url, settings.nowpayments_sandbox, settings.app_env
    try:
        settings.nowpayments_sandbox = False
        settings.app_env = "production"
        settings.public_base_url = "https://localhost"
        assert callback_configuration_error() is not None
        settings.public_base_url = "http://billing.example.com"
        assert callback_configuration_error() is not None
        settings.public_base_url = "https://billing.example.com"
        assert callback_configuration_error() is None
    finally:
        settings.public_base_url, settings.nowpayments_sandbox, settings.app_env = original


def test_local_sandbox_can_exercise_checkout_without_weakening_production_gate() -> None:
    settings = get_settings()
    original = settings.public_base_url, settings.nowpayments_sandbox, settings.app_env
    try:
        settings.public_base_url = "http://localhost:8000"
        settings.nowpayments_sandbox = True
        settings.app_env = "local"
        assert callback_configuration_error() is None
        settings.app_env = "production"
        assert callback_configuration_error() is not None
    finally:
        settings.public_base_url, settings.nowpayments_sandbox, settings.app_env = original


def test_sandbox_uses_the_sandbox_api_without_a_custom_endpoint() -> None:
    settings = get_settings()
    original_sandbox, original_url = settings.nowpayments_sandbox, settings.nowpayments_api_base_url
    try:
        settings.nowpayments_sandbox = True
        settings.nowpayments_api_base_url = "https://api.nowpayments.io/v1"
        assert nowpayments_api_base_url() == "https://api-sandbox.nowpayments.io/v1"
        settings.nowpayments_api_base_url = "https://sandbox-proxy.example/v1"
        assert nowpayments_api_base_url() == "https://sandbox-proxy.example/v1"
    finally:
        settings.nowpayments_sandbox, settings.nowpayments_api_base_url = original_sandbox, original_url


def test_currency_picker_uses_only_merchant_checked_coins() -> None:
    settings = get_settings()
    original = settings.payment_provider, settings.nowpayments_api_key, settings.nowpayments_sandbox, settings.nowpayments_api_base_url
    original_cache = billing._CURRENCY_CACHE
    requested_urls: list[str] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, *, headers):
            requested_urls.append(url)
            if url.endswith("/merchant/coins"):
                return httpx.Response(200, json={"selectedCurrencies": ["BTC", "USDTERC20"]})
            return httpx.Response(200, json=[
                {"code": "BTC", "name": "Bitcoin", "ticker": "BTC", "network": "btc", "logo_url": "/images/coins/btc.svg"},
                {"code": "USDTERC20", "name": "Tether USD", "ticker": "USDT", "network": "eth", "logo_url": "/images/coins/usdt.svg"},
            ])

    try:
        settings.payment_provider = "nowpayments"
        settings.nowpayments_api_key = "test-key"
        settings.nowpayments_sandbox = True
        settings.nowpayments_api_base_url = "https://api.nowpayments.io/v1"
        billing._CURRENCY_CACHE = None
        with patch("app.billing.httpx.AsyncClient", return_value=FakeClient()):
            currencies = asyncio.run(billing.fetch_nowpayments_currencies())
    finally:
        settings.payment_provider, settings.nowpayments_api_key, settings.nowpayments_sandbox, settings.nowpayments_api_base_url = original
        billing._CURRENCY_CACHE = original_cache

    assert requested_urls == [
        "https://api-sandbox.nowpayments.io/v1/merchant/coins",
        "https://api-sandbox.nowpayments.io/v1/full-currencies",
    ]
    assert [{"code": item["code"], "label": item["label"]} for item in currencies] == [
        {"code": "btc", "label": "Bitcoin"},
        {"code": "usdterc20", "label": "USDT · Ethereum"},
    ]
    assert currencies[0]["logo_url"] == "https://nowpayments.io/images/coins/btc.svg"
    assert currencies[1]["name"] == "Tether USD"


def test_active_subscription_wins_over_newer_pending_checkout() -> None:
    active = Subscription(id="active", user_id="user", plan_code="monthly", status="active", ends_at=utcnow() + timedelta(days=1))
    pending = Subscription(id="pending", user_id="user", plan_code="annual", status="pending")
    assert _pick_subscription([pending, active]) is active


def test_only_matching_provider_payment_activates_subscription() -> None:
    subscription = Subscription(id="sub", user_id="user", plan_code="monthly", status="pending")
    payment = Payment(id="pay", user_id="user", subscription_id="sub", provider="nowpayments", order_id="atc-order", status="waiting", amount=19.0, currency="usd")
    payload = {"payment_id": "provider-pay", "payment_status": "finished", "order_id": "atc-order", "price_amount": 19.0, "price_currency": "usd"}
    _apply_provider_status(payment, subscription, payload)
    assert payment.status == "finished"
    assert subscription.status == "active"


def test_pending_invoice_is_reconciled_before_another_checkout() -> None:
    subscription = Subscription(id="sub", user_id="user", plan_code="monthly", status="pending")
    payment = Payment(id="pay", user_id="user", subscription_id="sub", provider="nowpayments", provider_invoice_id="invoice-1", order_id="atc-order", status="waiting", amount=19.0, currency="usd", raw_payload={"invoice_url": "https://nowpayments.example/invoice"})
    provider_payment = {"payment_id": "provider-pay", "payment_status": "failed", "order_id": "atc-order", "price_amount": 19.0, "price_currency": "usd"}
    with patch("app.routes.billing.fetch_nowpayments_invoice_payments", AsyncMock(return_value=[provider_payment])):
        assert asyncio.run(_reconcile_pending_payment(payment, subscription)) is True
    assert payment.provider_payment_id == "provider-pay"
    assert payment.status == "failed"
    assert subscription.status == "expired"


def test_orphaned_legacy_checkout_is_released_only_after_provider_lookup() -> None:
    subscription = Subscription(id="sub", user_id="user", plan_code="monthly", status="pending")
    payment = Payment(id="pay", user_id="user", subscription_id="sub", provider="nowpayments", provider_invoice_id="invoice-1", order_id="atc-order", status="waiting", amount=19.0, currency="usd", raw_payload={})
    with patch("app.routes.billing.fetch_nowpayments_invoice_payments", AsyncMock(return_value=[])):
        assert asyncio.run(_reconcile_pending_payment(payment, subscription)) is True
    assert payment.status == "expired"
    assert subscription.status == "expired"


def test_unused_legacy_hosted_checkout_is_superseded_after_provider_lookup() -> None:
    subscription = Subscription(id="sub", user_id="user", plan_code="monthly", status="pending")
    payment = Payment(
        id="pay", user_id="user", subscription_id="sub", provider="nowpayments",
        provider_invoice_id="invoice-1", order_id="atc-order", status="waiting",
        amount=19.0, currency="usd", raw_payload={"invoice_url": "https://nowpayments.example/invoice"},
    )
    with patch("app.routes.billing.fetch_nowpayments_invoice_payments", AsyncMock(return_value=[])):
        assert asyncio.run(_reconcile_pending_payment(payment, subscription)) is True
    assert payment.status == "expired"
    assert subscription.status == "expired"


def test_legacy_checkout_with_an_ambiguous_provider_payment_stays_locked() -> None:
    subscription = Subscription(id="sub", user_id="user", plan_code="monthly", status="pending")
    payment = Payment(
        id="pay", user_id="user", subscription_id="sub", provider="nowpayments",
        provider_invoice_id="invoice-1", order_id="atc-order", status="waiting",
        amount=19.0, currency="usd", raw_payload={"invoice_url": "https://nowpayments.example/invoice"},
    )
    unrelated = {"payment_id": "provider-pay", "payment_status": "waiting", "order_id": "unexpected-order", "price_amount": 19.0, "price_currency": "usd"}
    with patch("app.routes.billing.fetch_nowpayments_invoice_payments", AsyncMock(return_value=[unrelated])):
        assert asyncio.run(_reconcile_pending_payment(payment, subscription)) is False
    assert payment.status == "waiting"
    assert subscription.status == "pending"


def test_local_sandbox_releases_legacy_invoice_when_provider_lookup_uses_the_wrong_environment() -> None:
    settings = get_settings()
    original_sandbox, original_environment = settings.nowpayments_sandbox, settings.app_env
    subscription = Subscription(id="sub", user_id="user", plan_code="monthly", status="pending")
    payment = Payment(
        id="pay", user_id="user", subscription_id="sub", provider="nowpayments",
        provider_invoice_id="invoice-1", order_id="atc-order", status="waiting",
        amount=19.0, currency="usd", raw_payload={"invoice_url": "https://nowpayments.example/invoice"},
    )
    try:
        settings.nowpayments_sandbox, settings.app_env = True, "local"
        with patch("app.routes.billing.fetch_nowpayments_invoice_payments", AsyncMock(side_effect=PaymentProviderError("401"))):
            assert asyncio.run(_reconcile_pending_payment(payment, subscription)) is True
    finally:
        settings.nowpayments_sandbox, settings.app_env = original_sandbox, original_environment
    assert payment.status == "expired"
    assert subscription.status == "expired"


def test_in_page_checkout_generates_its_qr_without_a_third_party_url() -> None:
    assert payment_qr_data_uri("TExamplePaymentAddress").startswith("data:image/png;base64,")


def test_checkout_view_returns_only_the_bound_provider_instructions() -> None:
    subscription = Subscription(id="sub", user_id="user", plan_code="monthly", status="pending")
    payment = Payment(
        id="pay", user_id="user", subscription_id="sub", provider="nowpayments",
        provider_payment_id="provider-pay", order_id="atc-order", status="waiting",
        amount=5.99, currency="usd", pay_currency="usdttrc20", pay_amount="5.99",
        payment_address="TExamplePaymentAddress",
        raw_payload={"network": "TRON", "expiration_estimate_date": "2030-01-01T00:00:00Z"},
    )
    view = _checkout_view(payment, subscription, reused=False)
    assert view["pay_address"] == "TExamplePaymentAddress"
    assert view["pay_currency"] == "USDTTRC20"
    assert view["plan_days"] == 30
    assert view["qr_data_uri"].startswith("data:image/png;base64,")


def test_provider_currency_must_match_the_user_selected_route() -> None:
    provider_payload = {"payment_id": "provider", "payment_status": "waiting", "pay_address": "btc-address", "pay_amount": "0.001", "pay_currency": "btc"}
    with pytest.raises(PaymentProviderError, match="did not return the token"):
        validate_nowpayments_payment_route(provider_payload, "usdttrc20")
