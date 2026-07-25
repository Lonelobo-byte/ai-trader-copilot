"""Focused tests for subscription payment safety rules."""
from __future__ import annotations

from datetime import timedelta

from app.auth import utcnow
from app.billing import callback_configuration_error
from app.db.models import Payment, Subscription
from app.routes.billing import _apply_provider_status, _pick_subscription
from app.settings import get_settings


def test_checkout_requires_a_public_https_callback_url() -> None:
    settings = get_settings()
    original = settings.public_base_url
    try:
        settings.public_base_url = "https://localhost"
        assert callback_configuration_error() is not None
        settings.public_base_url = "http://billing.example.com"
        assert callback_configuration_error() is not None
        settings.public_base_url = "https://billing.example.com"
        assert callback_configuration_error() is None
    finally:
        settings.public_base_url = original


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
