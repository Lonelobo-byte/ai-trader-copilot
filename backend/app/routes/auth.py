"""Account registration and login endpoints."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select

from ..auth import as_utc, create_access_token, hash_password, utcnow, verify_password
from ..db.database import AsyncSessionLocal
from ..db.models import AuditEvent, RefreshToken, User
from ..rate_limit import enforce_rate_limit
from ..settings import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=256)


def _email(value: str) -> str:
    value = value.strip().lower()
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise HTTPException(status_code=422, detail="A valid email address is required.")
    return value


def _view(user: User) -> dict:
    return {"id": user.id, "email": user.email, "role": user.role}


async def _audit(event_type: str, request: Request, user_id: str | None = None, metadata: dict | None = None) -> None:
    async with AsyncSessionLocal() as session:
        session.add(AuditEvent(id=str(uuid.uuid4()), user_id=user_id, event_type=event_type, ip_address=request.client.host if request.client else None, metadata_json=metadata))
        await session.commit()


async def _issue_tokens(user: User, response: Response) -> dict:
    settings = get_settings()
    token = secrets.token_urlsafe(48)
    async with AsyncSessionLocal() as session:
        session.add(RefreshToken(id=str(uuid.uuid4()), user_id=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=utcnow() + timedelta(days=settings.auth_refresh_token_days)))
        await session.commit()
    response.set_cookie(
        "refresh_token",
        token,
        httponly=True,
        secure=settings.app_env.lower() not in {"local", "test", "development"},
        samesite="strict",
        max_age=settings.auth_refresh_token_days * 86400,
        path="/auth",
    )
    return {"access_token": create_access_token(user), "token_type": "bearer", "user": _view(user)}


@router.post("/register", status_code=201)
async def register(body: Credentials, response: Response, request: Request):
    enforce_rate_limit(request, "register", limit=5, window_seconds=15 * 60)
    settings = get_settings()
    if not settings.allow_public_signup:
        raise HTTPException(status_code=403, detail="Public sign-up is disabled.")
    email = _email(body.email)
    async with AsyncSessionLocal() as session:
        if await session.scalar(select(User).where(User.email == email)):
            raise HTTPException(status_code=409, detail="Account already exists.")
        admins = {item.lower() for item in settings.bootstrap_admin_emails}
        user = User(id=str(uuid.uuid4()), email=email, password_hash=hash_password(body.password), role="admin" if email in admins else "member")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    await _audit("account_registered", request, user.id)
    return await _issue_tokens(user, response)


@router.post("/login")
async def login(body: Credentials, response: Response, request: Request):
    enforce_rate_limit(request, "login", limit=10, window_seconds=15 * 60)
    email = _email(body.email)
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if not user or not user.is_active or not verify_password(body.password, user.password_hash):
            await _audit("login_failed", request, metadata={"email": email})
            raise HTTPException(status_code=401, detail="Invalid email or password.")
        user.last_login_at = utcnow()
        await session.commit()
    await _audit("login_succeeded", request, user.id)
    return await _issue_tokens(user, response)


@router.post("/refresh")
async def refresh(request: Request, response: Response, refresh_token: str | None = Cookie(default=None)):
    enforce_rate_limit(request, "refresh", limit=20, window_seconds=15 * 60)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token is missing.")
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    async with AsyncSessionLocal() as session:
        now = utcnow()
        await session.execute(
            delete(RefreshToken).where(
                or_(
                    RefreshToken.expires_at <= now,
                    RefreshToken.revoked_at <= now - timedelta(days=1),
                )
            )
        )
        stored = await session.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .with_for_update()
        )
        if not stored or stored.revoked_at or as_utc(stored.expires_at) <= utcnow():
            await session.commit()
            raise HTTPException(status_code=401, detail="Refresh token is invalid or expired.")
        user = await session.get(User, stored.user_id)
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="Account is unavailable.")
        stored.revoked_at = utcnow()  # rotation prevents replay of the old cookie.
        await session.commit()
    return await _issue_tokens(user, response)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, refresh_token: str | None = Cookie(default=None)):
    enforce_rate_limit(request, "logout", limit=30, window_seconds=15 * 60)
    if refresh_token:
        async with AsyncSessionLocal() as session:
            stored = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hashlib.sha256(refresh_token.encode()).hexdigest()))
            if stored:
                stored.revoked_at = utcnow()
                await session.commit()
    response.delete_cookie("refresh_token", path="/auth")
