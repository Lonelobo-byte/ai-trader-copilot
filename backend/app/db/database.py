import asyncio
import logging
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from app.settings import get_settings

logger = logging.getLogger(__name__)

# Dynamically resolve absolute path to workspace root data directory
project_root = Path(__file__).resolve().parents[3]
db_dir = project_root / "data"
db_dir.mkdir(exist_ok=True)
db_path = db_dir / "apex.db"

DATABASE_URL = get_settings().database_url or f"sqlite+aiosqlite:///{db_path.as_posix()}"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
_initialized = False
_initialization_lock = asyncio.Lock()

class Base(AsyncAttrs, DeclarativeBase):
    pass

async def init_db():
    global _initialized
    if _initialized:
        return
    # Import models to guarantee registration with Base.metadata before creation
    from .models import (
        AnalysisSession, TradeSignal, User, UserAIConnection, Subscription,
        Payment, RefreshToken, AuditEvent, ScannerConfiguration, PlatformAIUsage,
        ResearchSlot, RadarSnapshot,
    )
    async with _initialization_lock:
        if _initialized:
            return
        try:
            async with engine.begin() as conn:
                if get_settings().app_env.lower() in {"local", "test", "development"}:
                    # Local SQLite remains zero-setup. Production schema changes
                    # are owned exclusively by the Alembic migrate container.
                    await conn.run_sync(Base.metadata.create_all)
                else:
                    await conn.execute(text("SELECT 1"))
            _initialized = True
            logger.info("Database readiness verified successfully.")
        except Exception:
            # Startup must fail closed. Swallowing this exception let Docker
            # report a healthy application with an unusable database.
            logger.exception("Failed to initialize or verify the database.")
            raise
