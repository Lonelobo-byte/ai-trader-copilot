"""Secure per-user OpenRouter connection helpers."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from .ai_client import AIRequestConfig
from .db.database import AsyncSessionLocal
from .db.models import UserAIConnection
from .settings import get_settings

MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


class UserAIConnectionError(ValueError):
    """A stored connection exists but cannot safely be used."""


def validate_model_id(model: str) -> str:
    normalized = model.strip()
    if not MODEL_ID_PATTERN.fullmatch(normalized):
        raise UserAIConnectionError("Enter a valid OpenRouter model ID, for example openai/gpt-4.1-mini.")
    return normalized


def _fernet() -> Fernet:
    settings = get_settings()
    secret = settings.user_secrets_encryption_key or settings.auth_jwt_secret
    # Local development remains usable before an auth secret is configured.
    # The ephemeral secret intentionally makes saved keys unreadable after a
    # restart, which is safer than persisting them with a known default.
    secret = secret or os.environ.setdefault("AI_TRADER_EPHEMERAL_USER_SECRET", secrets.token_urlsafe(48))
    key = base64.urlsafe_b64encode(hashlib.sha256(f"atc:user-ai:{secret}".encode("utf-8")).digest())
    return Fernet(key)


def encrypt_api_key(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode("utf-8")).decode("utf-8")


def decrypt_api_key(encrypted_api_key: str) -> str:
    try:
        return _fernet().decrypt(encrypted_api_key.encode("utf-8")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise UserAIConnectionError("Saved AI connection cannot be decrypted. Update the API key to continue.") from exc


def key_hint(key_suffix: str) -> str:
    return f"••••{key_suffix}" if key_suffix else "••••"


def ai_cache_identity(user_id: str, config: AIRequestConfig | None) -> str:
    """A non-secret cache partition that changes when model or key changes."""
    if config is None:
        return f"{user_id}:deterministic"
    material = f"{user_id}:{config.model}:{config.api_key}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def public_connection(connection: UserAIConnection | None) -> dict[str, object]:
    if connection is None:
        return {"connected": False, "provider": "openrouter", "model": None, "key_hint": None}
    return {
        "connected": True,
        "provider": connection.provider,
        "model": connection.model,
        "key_hint": key_hint(connection.key_suffix),
        "updated_at": connection.updated_at.isoformat() if connection.updated_at else None,
    }


async def get_user_connection(user_id: str) -> UserAIConnection | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserAIConnection).where(UserAIConnection.user_id == user_id))
        return result.scalar_one_or_none()


async def resolve_user_ai_config(user_id: str) -> AIRequestConfig | None:
    connection = await get_user_connection(user_id)
    if connection is None:
        return None
    api_key = decrypt_api_key(connection.encrypted_api_key).strip()
    if not api_key:
        raise UserAIConnectionError("Saved AI connection has no usable key. Update the API key to continue.")
    return AIRequestConfig(provider="openrouter", api_key=api_key, model=connection.model)


async def save_openrouter_connection(user_id: str, *, api_key: str, model: str) -> dict[str, object]:
    model = validate_model_id(model)
    api_key = api_key.strip()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserAIConnection).where(UserAIConnection.user_id == user_id))
        connection = result.scalar_one_or_none()
        if connection is None:
            if len(api_key) < 8:
                raise UserAIConnectionError("Enter your OpenRouter API key before saving this connection.")
            connection = UserAIConnection(
                id=secrets.token_hex(18), user_id=user_id, provider="openrouter",
                encrypted_api_key=encrypt_api_key(api_key), key_suffix=api_key[-4:], model=model,
            )
            session.add(connection)
        else:
            connection.model = model
            if api_key:
                if len(api_key) < 8:
                    raise UserAIConnectionError("The OpenRouter API key looks incomplete.")
                connection.encrypted_api_key = encrypt_api_key(api_key)
                connection.key_suffix = api_key[-4:]
        await session.commit()
        await session.refresh(connection)
        return public_connection(connection)


async def delete_user_connection(user_id: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserAIConnection).where(UserAIConnection.user_id == user_id))
        connection = result.scalar_one_or_none()
        if connection is not None:
            await session.delete(connection)
            await session.commit()
