"""Checkout and signed payment-webhook endpoints."""
from __future__ import annotations

import logging
import uuid
import asyncio
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from ..auth import current_user, subscription_is_active, utcnow
from ..billing import (
    PLANS,
    PaymentProviderBusyError,
    PaymentProviderError,
    callback_configuration_error,
    complete_subscription,
    fetch_nowpayments_currencies,
    fetch_nowpayments_invoice_payments,
    create_nowpayments_payment,
    fetch_nowpayments_payment,
    plan_details,
    payment_qr_data_uri,
    verify_nowpayments_ipn,
)
from ..db.database import AsyncSessionLocal
from ..db.models import AuditEvent, Payment, Subscription, User
from ..rate_limit import enforce_rate_limit
from ..settings import get_settings

router = APIRouter(prefix="/billing", tags=["billing"])
logger = logging.getLogger(__name__)

_ACTIVE_PROVIDER_STATUSES = {"waiting", "confirming", "sending", "partially_paid"}
_TERMINAL_PROVIDER_STATUSES = {"finished", "confirmed", "failed", "expired", "refunded"}
_CHECKOUT_LOCKS: dict[str, asyncio.Lock] = {}
_MAX_CHECKOUT_LOCKS = 2_000


class CheckoutRequest(BaseModel):
    plan_code: str
    pay_currency: str


@router.get("/plans")
async def plans():
    currency = get_settings().billing_currency.upper()
    return {
        "currency": currency,
        "plans": [
            {
                "code": code,
                "amount": item["amount"],
                "days": item["days"],
                "list_amount": item.get("list_amount", item["amount"]),
                "savings_pct": max(0, round((1 - item["amount"] / item.get("list_amount", item["amount"])) * 100)),
                "savings_basis": "launch_price",
            }
            for code, item in PLANS.items()
        ],
        "checkout_available": callback_configuration_error() is None,
        "checkout_message": callback_configuration_error(),
    }


@router.get("/payment-currencies")
async def payment_currencies(request: Request, plan_code: str, user: User = Depends(current_user)):
    """Return merchant-enabled assets without fan-out calls to the provider."""
    enforce_rate_limit(request, "payment_currencies", limit=20, window_seconds=60)
    try:
        plan_details(plan_code)
        currencies = await fetch_nowpayments_currencies()
        return {
            "currencies": currencies,
            "plan_code": plan_code,
            "minimum_check": "selected_asset_at_checkout",
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except PaymentProviderBusyError as exc:
        logger.warning("NOWPayments throttled the currency catalog: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="The payment network is briefly busy. Wait a few seconds and reopen checkout.",
            headers={"Retry-After": "10"},
        ) from exc
    except (PaymentProviderError, RuntimeError) as exc:
        logger.warning("Could not load compatible NOWPayments currency catalog: %s", exc)
        raise HTTPException(status_code=502, detail="NOWPayments could not verify compatible payment assets. Please try again shortly.") from exc


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
        "order_id": payment.order_id,
        "provider_invoice_id": payment.provider_invoice_id,
        "provider_payment_id": payment.provider_payment_id,
        "pay_currency": payment.pay_currency,
        "pay_amount": payment.pay_amount,
        "pay_address": payment.payment_address,
        "payment_address": payment.payment_address,
        "confirmations": payment.confirmations,
        "payment_expires_at": (payment.raw_payload or {}).get("expiration_estimate_date"),
        "network": (payment.raw_payload or {}).get("network"),
        "payment_extra_id": (payment.raw_payload or {}).get("payment_extra_id"),
        "transaction_hash": payment.transaction_hash,
        "subscription": _subscription_view(subscription),
        "awaiting_provider_callback": payment.provider_payment_id is None and payment.status in {"waiting", "confirming"},
    }


def _checkout_view(payment: Payment, subscription: Subscription, *, reused: bool, verification_pending: bool = False) -> dict:
    """Only expose payment instructions belonging to the authenticated order."""
    payload = payment.raw_payload or {}
    plan = plan_details(subscription.plan_code)
    address = payment.payment_address or payload.get("pay_address")
    pay_amount = payment.pay_amount or payload.get("pay_amount")
    pay_currency = payment.pay_currency or payload.get("pay_currency")
    if address and pay_amount and pay_currency:
        return {
            "payment_id": payment.id,
            "provider_payment_id": payment.provider_payment_id,
            "order_id": payment.order_id,
            "plan_code": subscription.plan_code,
            "plan_days": plan["days"],
            "plan_amount": plan["amount"],
            "price_currency": payment.currency.upper(),
            "payment_status": payment.status,
            "pay_address": address,
            "pay_amount": str(pay_amount),
            "pay_currency": str(pay_currency).upper(),
            "network": payload.get("network"),
            "payment_extra_id": payload.get("payment_extra_id"),
            "expires_at": payload.get("expiration_estimate_date"),
            "qr_data_uri": payment_qr_data_uri(str(address)),
            "reused": reused,
            "verification_pending": verification_pending,
        }
    raise HTTPException(status_code=409, detail="The existing payment has no usable payment instructions. Create a new checkout after its provider status is reconciled.")


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


def _expire_pending_checkout(payment: Payment, subscription: Subscription) -> None:
    """Retire a checkout that has no provider payment route."""
    payment.status = "expired"
    if subscription.status == "pending":
        subscription.status = "expired"


def _can_release_unverified_sandbox_legacy_checkout(payment: Payment) -> bool:
    """A local sandbox may contain checkouts created under an older API mode.

    Some rows have only an invoice URL, some have an invoice ID, and neither
    shape has a provider payment route. Those test checkouts cannot move real
    funds, and NOWPayments may reject a cross-environment lookup. Allow a
    developer to start a clean sandbox checkout, but never use this bypass for
    live payments.
    """
    settings = get_settings()
    return (
        not payment.provider_payment_id
        and settings.nowpayments_sandbox
        and settings.app_env.lower() in {"local", "development", "test"}
    )


async def _reconcile_pending_payment(payment: Payment, subscription: Subscription) -> bool:
    """Synchronize one pending checkout from NOWPayments without trusting UI redirects.

    Hosted invoices do not have a payment ID until the customer selects a
    payment route.  We therefore use the stored invoice ID to discover that
    provider payment first, then reconcile its authoritative status.
    """
    try:
        if payment.provider_payment_id:
            provider_payload = await fetch_nowpayments_payment(payment.provider_payment_id)
            _apply_provider_status(payment, subscription, provider_payload)
            return True

        if not payment.provider_invoice_id:
            if (
                not (payment.raw_payload or {}).get("invoice_url")
                or _can_release_unverified_sandbox_legacy_checkout(payment)
            ):
                _expire_pending_checkout(payment, subscription)
                return True
            return False
        provider_payments = await fetch_nowpayments_invoice_payments(payment.provider_invoice_id)
        matching = next((item for item in provider_payments if _provider_payload_is_expected(payment, item)), None)
        if matching:
            _apply_provider_status(payment, subscription, matching)
            return True

        # A pre-workspace hosted invoice has no payment route until the
        # customer chooses a coin on NOWPayments.  Once NOWPayments confirms
        # that it has no route/payment at all, retire it locally and allow a
        # clean in-page checkout.  If the provider returned any non-matching
        # record, keep the lock: replacing an ambiguous payment could charge
        # the customer twice.
        if provider_payments:
            # Sandbox databases frequently outlive the provider test
            # environment or contain hosted-invoice records created by an
            # older integration. A non-matching sandbox record cannot move
            # real funds and must not permanently lock local checkout. Live
            # payments deliberately retain the lock because a mismatched
            # provider record is ambiguous and replacing it could duplicate a
            # real charge.
            if _can_release_unverified_sandbox_legacy_checkout(payment):
                logger.info(
                    "Releasing ambiguous legacy sandbox checkout %s after provider lookup",
                    payment.id,
                )
                _expire_pending_checkout(payment, subscription)
                return True
            return False
        _expire_pending_checkout(payment, subscription)
        return True
    except (PaymentProviderError, ValueError) as exc:
        if _can_release_unverified_sandbox_legacy_checkout(payment):
            logger.info("Releasing legacy sandbox checkout %s after cross-environment provider lookup failed", payment.id)
            _expire_pending_checkout(payment, subscription)
            return True
        logger.warning("Could not reconcile NOWPayments payment %s: %s", payment.id, exc)
        return False


@router.get("/me")
async def my_subscription(user: User = Depends(current_user)):
    async with AsyncSessionLocal() as session:
        rows = await session.scalars(select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.created_at.desc()))
        subscription = _pick_subscription(list(rows))
        access_granted = (
            not get_settings().subscription_enforcement_enabled
            or user.role == "admin"
            or (subscription is not None and subscription_is_active(subscription))
        )
        return {"role": user.role, "subscription": _subscription_view(subscription), "access_granted": access_granted}


@router.post("/checkout")
async def checkout(body: CheckoutRequest, request: Request, user: User = Depends(current_user)):
    try:
        plan = plan_details(body.plan_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if get_settings().payment_provider != "nowpayments":
        raise HTTPException(status_code=503, detail="Set PAYMENT_PROVIDER=nowpayments and configure its API credentials before checkout.")
    configuration_error = callback_configuration_error()
    if configuration_error:
        raise HTTPException(status_code=503, detail=configuration_error)

    # Lock the user row for the entire provider create-and-persist sequence.
    # The in-process lock protects SQLite/local runs, while FOR UPDATE makes
    # the same guarantee across Docker workers backed by PostgreSQL.
    if len(_CHECKOUT_LOCKS) >= _MAX_CHECKOUT_LOCKS:
        for stale_user, stale_lock in list(_CHECKOUT_LOCKS.items()):
            if not stale_lock.locked():
                _CHECKOUT_LOCKS.pop(stale_user, None)
            if len(_CHECKOUT_LOCKS) < _MAX_CHECKOUT_LOCKS:
                break
    lock = _CHECKOUT_LOCKS.setdefault(user.id, asyncio.Lock())
    async with lock:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.scalar(select(User).where(User.id == user.id).with_for_update())
                existing = await session.scalar(
                    select(Payment)
                    .join(Subscription, Payment.subscription_id == Subscription.id)
                    .where(Payment.user_id == user.id, Subscription.status == "pending", Payment.status.in_(_ACTIVE_PROVIDER_STATUSES))
                    .order_by(Payment.created_at.desc())
                    .with_for_update()
                )
                reconciled = False
                if existing:
                    subscription = await session.get(Subscription, existing.subscription_id, with_for_update=True)
                    reconciled = await _reconcile_pending_payment(existing, subscription)
                    if reconciled:
                        session.add(AuditEvent(id=str(uuid.uuid4()), user_id=user.id, event_type="payment_checkout_reconciled", metadata_json={"order_id": existing.order_id, "status": existing.status}))
                    if existing.status not in _ACTIVE_PROVIDER_STATUSES:
                        existing = None
                if existing:
                    if existing.provider_invoice_id and not existing.provider_payment_id:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "The previous provider checkout could not be verified. "
                                "Confirm that the NOWPayments API key and sandbox/live mode match the original payment, then retry."
                            ),
                        )
                    if existing.pay_currency and existing.pay_currency.lower() != body.pay_currency.lower():
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"A {existing.pay_currency.upper()} payment is still {existing.status}. "
                                "Complete it or wait for it to expire before choosing another token, so you cannot be charged twice."
                            ),
                        )
                    return _checkout_view(existing, subscription, reused=True, verification_pending=not reconciled)

                # Only genuine creation attempts consume checkout quota.
                enforce_rate_limit(request, "checkout", limit=5, window_seconds=15 * 60)
                subscription_id, payment_id, order_id = str(uuid.uuid4()), str(uuid.uuid4()), f"atc-{uuid.uuid4().hex}"
                try:
                    provider_payment = await create_nowpayments_payment(order_id, body.plan_code, body.pay_currency)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                except PaymentProviderBusyError as exc:
                    logger.warning("NOWPayments throttled checkout creation: %s", exc)
                    raise HTTPException(
                        status_code=503,
                        detail="The payment network is briefly busy. Wait 10 seconds and retry this checkout.",
                        headers={"Retry-After": "10"},
                    ) from exc
                except PaymentProviderError as exc:
                    logger.warning("NOWPayments checkout failed: %s", exc)
                    raise HTTPException(status_code=502, detail="NOWPayments could not create payment instructions. Try another compatible asset or retry shortly.") from exc
                subscription = Subscription(id=subscription_id, user_id=user.id, plan_code=body.plan_code, status="pending")
                payment = Payment(
                    id=payment_id, user_id=user.id, subscription_id=subscription_id,
                    provider="nowpayments", provider_invoice_id=None,
                    provider_payment_id=str(provider_payment["payment_id"]), order_id=order_id,
                    status=str(provider_payment["payment_status"]).lower(),
                    amount=plan["amount"], currency=get_settings().billing_currency,
                    pay_currency=str(provider_payment["pay_currency"]),
                    pay_amount=str(provider_payment["pay_amount"]),
                    payment_address=str(provider_payment["pay_address"]),
                    raw_payload=provider_payment,
                )
                # A sandbox success case can already be finished in the create
                # response. Apply the same provider/order validation used by
                # IPN and status polling before granting any entitlement.
                _apply_provider_status(payment, subscription, provider_payment)
                session.add_all([subscription, payment])
                return _checkout_view(payment, subscription, reused=False)


@router.get("/payment-status")
async def payment_status(request: Request, user: User = Depends(current_user)):
    """Return the latest payment and recheck known provider transaction IDs.

    This endpoint is called after the hosted checkout redirects back.  It never
    trusts the redirect: activation happens only after a signed IPN followed by
    an authoritative NOWPayments payment-status response.
    """
    enforce_rate_limit(request, f"payment_status:{user.id}", limit=18, window_seconds=60)
    async with AsyncSessionLocal() as session:
        payment = await session.scalar(select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at.desc()))
        if payment is None:
            return {"payment": None}
        subscription = await session.get(Subscription, payment.subscription_id)
        verification_pending = payment.status in _ACTIVE_PROVIDER_STATUSES
        if verification_pending:
            reconciled = await _reconcile_pending_payment(payment, subscription)
            verification_pending = not reconciled
            if reconciled:
                session.add(AuditEvent(id=str(uuid.uuid4()), user_id=user.id, event_type="payment_status_reconciled", metadata_json={"order_id": payment.order_id, "status": payment.status}))
                await session.commit()
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
        payment = await session.scalar(select(Payment).where(Payment.order_id == order_id).with_for_update())
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment order not found.")
        subscription = await session.get(Subscription, payment.subscription_id, with_for_update=True)
        try:
            _apply_provider_status(payment, subscription, provider_payload)
        except ValueError as exc:
            logger.warning("Rejected NOWPayments webhook for order %s: %s", order_id, exc)
            raise HTTPException(status_code=409, detail="Payment verification did not match this order.") from exc
        session.add(AuditEvent(id=str(uuid.uuid4()), user_id=payment.user_id, event_type="payment_webhook", metadata_json={"order_id": order_id, "status": payment.status}))
        await session.commit()
