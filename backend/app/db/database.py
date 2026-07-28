import os
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

class Base(AsyncAttrs, DeclarativeBase):
    pass

async def init_db():
    # Import models to guarantee registration with Base.metadata before creation
    from .models import (
        AnalysisSession, TradeSignal, User, UserAIConnection, Subscription,
        Payment, RefreshToken, AuditEvent, ScannerConfiguration, PlatformAIUsage,
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}", exc_info=True)
