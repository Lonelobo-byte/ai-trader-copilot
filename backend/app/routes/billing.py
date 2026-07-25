"""Checkout and signed payment-webhook endpoints."""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from ..auth import current_user, subscription_is_active, utcnow
from ..billing import (
    PLANS,
    PaymentProviderError,
    callback_configuration_error,
    complete_subscription,
    create_nowpayments_invoice,
    fetch_nowpayments_payment,
    plan_details,
    verify_nowpayments_ipn,
)
from ..db.database import AsyncSessionLocal
from ..db.models import AuditEvent, Payment, Subscription, User
from ..rate_limit import enforce_rate_limit
from ..settings import get_settings

router = APIRouter(prefix="/billing", tags=["billing"])
logger = logging.getLogger(__name__)


class CheckoutRequest(BaseModel):
    plan_code: str


@router.get("/plans")
async def plans():
    currency = get_settings().billing_currency.upper()
    return {
        "currency": currency,
        "plans": [{"code": code, "amount": item["amount"], "days": item["days"]} for code, item in PLANS.items()],
        "checkout_available": callback_configuration_error() is None,
        "checkout_message": callback_configuration_error(),
    }


def _subscription_view(subscription: Subscription | None) -> dict | None:
    if subscription is None:
        return None
    return {
        "plan_code": subscription.plan_code,
        "status": subscription.status,
        "ends_at": subscription.ends_at,
        "active": subscription_is_active(subscription),
    }


def _pick_subscription(subscriptions: list[Subscription]) -> Subscription | None:
    """An active entitlement always wins over a newer abandoned checkout."""
    return next((item for item in subscriptions if subscription_is_active(item)), subscriptions[0] if subscriptions else None)


def _payment_view(payment: Payment | None, subscription: Subscription | None) -> dict | None:
    if payment is None:
        return None
    return {
        "status": payment.status,
        "provider_payment_id": payment.provider_payment_id,
        "transaction_hash": payment.transaction_hash,
        "subscription": _subscription_view(subscription),
        "awaiting_provider_callback": payment.provider_payment_id is None and payment.status in {"waiting", "confirming"},
    }


def _provider_payload_is_expected(payment: Payment, payload: dict) -> bool:
    """Reject a provider record that does not belong to this internal order."""
    if str(payload.get("order_id", "")) != payment.order_id:
        return False
    if str(payload.get("price_currency", "")).lower() != payment.currency.lower():
        return False
    try:
        return Decimal(str(payload.get("price_amount"))) == Decimal(str(payment.amount))
    except (InvalidOperation, TypeError):
        return False


def _apply_provider_status(payment: Payment, subscription: Subscription, payload: dict) -> None:
    provider_id = str(payload.get("payment_id", ""))
    status_value = str(payload.get("payment_status", "")).lower()
    if not provider_id or not status_value:
        raise ValueError("Payment provider response is missing identifiers.")
    if payment.provider_payment_id and payment.provider_payment_id != provider_id:
        raise ValueError("Payment identifier mismatch.")
    if not _provider_payload_is_expected(payment, payload):
        raise ValueError("Payment provider response does not match the expected order.")
    payment.provider_payment_id, payment.status, payment.raw_payload = provider_id, status_value, payload
    payment.pay_currency = payload.get("pay_currency")
    payment.pay_amount = str(payload.get("pay_amount")) if payload.get("pay_amount") is not None else None
    payment.payment_address = payload.get("pay_address")
    payment.transaction_hash = payload.get("payin_hash") or payload.get("transaction_hash")
    payment.confirmations = int(payload.get("confirmations") or 0)
    if status_value in {"finished", "confirmed"}:
        if payment.completed_at is None:
            complete_subscription(subscription)
            payment.completed_at = utcnow()
    elif status_value in {"failed", "expired", "refunded"} and subscription.status == "pending":
        subscription.status = "cancelled" if status_value == "refunded" else "expired"


@router.get("/me")
async def my_subscription(user: User = Depends(current_user)):
    async with AsyncSessionLocal() as session:
        rows = await session.scalars(select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.created_at.desc()))
        subscription = _pick_subscription(list(rows))
        return {"role": user.role, "subscription": _subscription_view(subscription)}


@router.post("/checkout")
async def checkout(body: CheckoutRequest, request: Request, user: User = Depends(current_user)):
    enforce_rate_limit(request, "checkout", limit=5, window_seconds=15 * 60)
    try:
        plan = plan_details(body.plan_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if get_settings().payment_provider != "nowpayments":
        raise HTTPException(status_code=503, detail="Set PAYMENT_PROVIDER=nowpayments and configure its API credentials before checkout.")
    configuration_error = callback_configuration_error()
    if configuration_error:
        raise HTTPException(status_code=503, detail=configuration_error)

    # Do not let a double-click or refresh create multiple live invoices for
    # the same user.  The original invoice remains payable and is reusable.
    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(Payment)
            .join(Subscription, Payment.subscription_id == Subscription.id)
            .where(Payment.user_id == user.id, Subscription.status == "pending", Payment.status.in_({"waiting", "confirming"}))
            .order_by(Payment.created_at.desc())
        )
        if existing:
            invoice_url = (existing.raw_payload or {}).get("invoice_url")
            if invoice_url:
                return {"payment_id": existing.id, "invoice_url": invoice_url, "order_id": existing.order_id, "reused": True}
            raise HTTPException(status_code=409, detail="A payment is already being processed. Wait for confirmation before starting another checkout.")

    subscription_id, payment_id, order_id = str(uuid.uuid4()), str(uuid.uuid4()), f"atc-{uuid.uuid4().hex}"
    try:
        invoice = await create_nowpayments_invoice(order_id, body.plan_code)
    except PaymentProviderError as exc:
        logger.warning("NOWPayments checkout failed: %s", exc)
        raise HTTPException(status_code=502, detail="NOWPayments could not create an invoice. Verify your API credentials and try again.") from exc
    async with AsyncSessionLocal() as session:
        subscription = Subscription(id=subscription_id, user_id=user.id, plan_code=body.plan_code, status="pending")
        # An invoice can create one or more provider payment IDs. Bind the first
        # signed IPN ID, rather than incorrectly treating the invoice ID as one.
        payment = Payment(id=payment_id, user_id=user.id, subscription_id=subscription_id, provider="nowpayments", provider_invoice_id=str(invoice["id"]), provider_payment_id=None, order_id=order_id, status="waiting", amount=plan["amount"], currency=get_settings().billing_currency, raw_payload=invoice)
        session.add_all([subscription, payment])
        await session.commit()
    return {"payment_id": payment_id, "invoice_url": invoice.get("invoice_url"), "order_id": order_id}


@router.get("/payment-status")
async def payment_status(user: User = Depends(current_user)):
    """Return the latest payment and recheck known provider transaction IDs.

    This endpoint is called after the hosted checkout redirects back.  It never
    trusts the redirect: activation happens only after a signed IPN followed by
    an authoritative NOWPayments payment-status response.
    """
    async with AsyncSessionLocal() as session:
        payment = await session.scalar(select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at.desc()))
        if payment is None:
            return {"payment": None}
        subscription = await session.get(Subscription, payment.subscription_id)
        verification_pending = False
        if payment.provider_payment_id:
            try:
                provider_payload = await fetch_nowpayments_payment(payment.provider_payment_id)
                _apply_provider_status(payment, subscription, provider_payload)
                session.add(AuditEvent(id=str(uuid.uuid4()), user_id=user.id, event_type="payment_status_reconciled", metadata_json={"order_id": payment.order_id, "status": payment.status}))
                await session.commit()
            except (PaymentProviderError, ValueError) as exc:
                logger.warning("Could not reconcile NOWPayments payment %s: %s", payment.id, exc)
                verification_pending = True
        return {"payment": _payment_view(payment, subscription), "verification_pending": verification_pending}


@router.post("/webhooks/nowpayments", status_code=204)
async def nowpayments_webhook(request: Request):
    raw = await request.body()
    try:
        payload = verify_nowpayments_ipn(raw, request.headers.get("x-nowpayments-sig"))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid payment webhook.") from exc
    order_id = str(payload.get("order_id", ""))
    provider_id = str(payload.get("payment_id", ""))
    if not order_id or not provider_id:
        raise HTTPException(status_code=422, detail="Payment webhook is missing identifiers.")
    try:
        # A signed webhook identifies the event.  The provider API is then
        # queried server-to-server before an entitlement is granted.
        provider_payload = await fetch_nowpayments_payment(provider_id)
    except PaymentProviderError as exc:
        logger.warning("NOWPayments webhook reconciliation delayed for %s: %s", provider_id, exc)
        raise HTTPException(status_code=503, detail="Payment verification is temporarily unavailable; retry will occur.") from exc
    async with AsyncSessionLocal() as session:
        payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment order not found.")
        subscription = await session.get(Subscription, payment.subscription_id)
        try:
            _apply_provider_status(payment, subscription, provider_payload)
        except ValueError as exc:
            logger.warning("Rejected NOWPayments webhook for order %s: %s", order_id, exc)
            raise HTTPException(status_code=409, detail="Payment verification did not match this order.") from exc
        session.add(AuditEvent(id=str(uuid.uuid4()), user_id=payment.user_id, event_type="payment_webhook", metadata_json={"order_id": order_id, "status": payment.status}))
        await session.commit()
