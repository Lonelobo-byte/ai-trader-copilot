"""Server-side authentication and subscription checks.

An access token only identifies the caller. Every protected request reads the
current entitlement, so a stale token cannot bypass a cancellation or ban.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from .db.database import AsyncSessionLocal
from .db.models import Subscription, User
from .settings import get_settings

bearer_scheme = HTTPBearer(auto_error=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive timestamps despite timezone=True; normalize once."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=16_384, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(derived).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return algorithm == "scrypt" and hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _secret() -> bytes:
    configured = get_settings().auth_jwt_secret
    # Local development still works without a secret, but sessions intentionally
    # disappear at restart. Docker production refuses to start without one.
    configured = configured or os.environ.setdefault("AI_TRADER_EPHEMERAL_AUTH_SECRET", secrets.token_urlsafe(48))
    return configured.encode()


def create_access_token(user: User) -> str:
    now = int(utcnow().timestamp())
    payload = {"sub": user.id, "role": user.role, "iat": now, "exp": now + get_settings().auth_access_token_minutes * 60, "iss": "ai-trader-copilot", "aud": "ai-trader-api", "jti": str(uuid.uuid4())}
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
    return f"{signing_input}.{_b64(hmac.new(_secret(), signing_input.encode(), hashlib.sha256).digest())}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        header, encoded_payload, signature = token.split(".")
        expected = _b64(hmac.new(_secret(), f"{header}.{encoded_payload}".encode(), hashlib.sha256).digest())
        payload = json.loads(_unb64(encoded_payload))
        valid = hmac.compare_digest(signature, expected) and payload.get("iss") == "ai-trader-copilot" and payload.get("aud") == "ai-trader-api" and isinstance(payload.get("sub"), str) and int(payload["exp"]) > int(utcnow().timestamp())
        if not valid:
            raise ValueError("invalid token")
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired access token.")


async def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required.", headers={"WWW-Authenticate": "Bearer"})
    claims = decode_access_token(credentials.credentials)
    async with AsyncSessionLocal() as session:
        user = await session.get(User, claims["sub"])
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Account is unavailable.")
        return user


def subscription_is_active(subscription: Subscription) -> bool:
    now = utcnow()
    if subscription.status in {"active", "trial"}:
        return subscription.ends_at is None or as_utc(subscription.ends_at) > now
    return subscription.status == "grace_period" and subscription.grace_ends_at is not None and as_utc(subscription.grace_ends_at) > now


async def require_active_subscription(user: User = Depends(current_user)) -> User:
    if not get_settings().subscription_enforcement_enabled or user.role == "admin":
        return user
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Subscription).where(Subscription.user_id == user.id))
        if not any(subscription_is_active(item) for item in result.scalars()):
            raise HTTPException(status_code=403, detail="An active subscription is required.")
    return user


async def websocket_subscription(websocket: WebSocket) -> User | None:
    try:
        # Browser WebSockets cannot set Authorization headers.  Carry the
        # token in a requested subprotocol instead of the URL: URLs are often
        # written to reverse-proxy and Uvicorn access logs.
        protocols = websocket.scope.get("subprotocols", [])
        token = protocols[1] if len(protocols) >= 2 and protocols[0] == "atc-auth" else ""
        claims = decode_access_token(token)
    except HTTPException:
        await websocket.close(code=4401)
        return None
    async with AsyncSessionLocal() as session:
        user = await session.get(User, claims["sub"])
        if user is None or not user.is_active:
            await websocket.close(code=4401)
            return None
        if get_settings().subscription_enforcement_enabled and user.role != "admin":
            result = await session.execute(select(Subscription).where(Subscription.user_id == user.id))
            if not any(subscription_is_active(item) for item in result.scalars()):
                await websocket.close(code=4403)
                return None
        return user
