"""Pytest configuration and shared test fixtures."""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.database
from app.db.database import Base
from app.db.models import AnalysisSession, TradeSignal, User  # noqa: F401
from app.settings import get_settings

# Legacy numerical tests exercise public research algorithms, not SaaS access.
# Production keeps this enabled (the Settings default is True).
get_settings().subscription_enforcement_enabled = False

# Setup a clean, thread-safe in-memory SQLite database for testing
test_engine = create_async_engine(
    "sqlite+aiosqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

# Intercept database engine and sessionmaker globally before any import
app.db.database.engine = test_engine
app.db.database.AsyncSessionLocal = TestSessionLocal

# The legacy test suite verifies research calculations and endpoint payloads.
# Explicitly bypass the premium dependency here; subscription behavior has its
# own smoke coverage and remains enabled by default in application settings.
from app.auth import current_user, require_active_subscription
from app.main import app


async def _test_subscription_bypass():
    return User(id="test-user", email="test@example.com", password_hash="test", role="member", is_active=True)


app.dependency_overrides[require_active_subscription] = _test_subscription_bypass
app.dependency_overrides[current_user] = _test_subscription_bypass


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async tests."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_test_db() -> AsyncGenerator[None, None]:
    """Automatically create all tables before each test and drop them after."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
