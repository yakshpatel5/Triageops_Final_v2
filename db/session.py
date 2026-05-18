"""
Async SQLAlchemy session management.
Uses asyncpg driver for maximum throughput on webhook ingest path.
"""

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from models import Base

# ---------------------------------------------------------------------------
# Engine — created once at startup, shared across the process
# ---------------------------------------------------------------------------
# asyncpg connection pool defaults are deliberately conservative for V1.
# Tune pool_size / max_overflow based on observed concurrency.

DATABASE_URL: str = os.environ["DATABASE_URL"]
# FastAPI/SQLAlchemy needs postgresql+asyncpg://...
# Accept both formats to be tolerant of Railway / Render env var formats.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,          # drop stale connections before handing to app
    pool_recycle=3600,           # recycle connections hourly
    echo=os.getenv("SQL_ECHO", "false").lower() == "true",  # verbose only in dev
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,      # keep objects usable after commit without re-fetch
    autoflush=False,
)


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Yields a transactional async session.
    Rolls back and re-raises on any SQLAlchemy error so the caller
    (webhook router) can catch IntegrityError for dedup handling.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
            raise
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Depends()-compatible wrapper around get_session."""
    async with get_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Table initialisation — called at app startup (not for production migrations)
# ---------------------------------------------------------------------------

from sqlalchemy import text

async def init_db() -> None:
    """
    Verify database connection.
    For production, schema changes must be handled by Alembic migrations.
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Database connection verified")
    except SQLAlchemyError as exc:
        logger.critical("Failed to verify database connection: {}", exc)
        raise


async def close_db() -> None:
    """Dispose engine connection pool on shutdown."""
    await engine.dispose()
    logger.info("Database connection pool disposed")
