"""Focused tests for subscription payment safety rules."""
from __future__ import annotations

from datetime import timedelta
import asyncio
from unittest.mock import AsyncMock, patch

from app.auth import utcnow
from app.billing import callback_configuration_error, payment_qr_data_uri
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
