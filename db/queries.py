"""
db/queries.py — Reusable async query helpers.

These wrap common patterns (get-or-404, paginated select, bulk insert)
with consistent error handling and logging so routers stay thin.
"""

from __future__ import annotations

import uuid
from typing import Any, Type, TypeVar

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

T = TypeVar("T", bound=DeclarativeBase)


async def get_or_404(
    session: AsyncSession,
    model: Type[T],
    record_id: uuid.UUID,
    tenant_id: str,
    tenant_column: str = "tenant_id",
) -> T:
    """
    Fetch a single row by PK + tenant scope, raise 404 if missing.
    Raises 503 on DB connectivity failure — never exposes internal errors.
    """
    try:
        result = await session.execute(
            select(model)
            .where(model.id == record_id)
            .where(getattr(model, tenant_column) == tenant_id)
        )
        row = result.scalar_one_or_none()
    except OperationalError as exc:
        logger.error("DB connectivity error in get_or_404 | model={} id={} error={}",
                     model.__name__, record_id, exc)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")
    except SQLAlchemyError as exc:
        logger.error("DB error in get_or_404 | model={} id={} error={}",
                     model.__name__, record_id, exc)
        raise HTTPException(status_code=500, detail="Internal server error")

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"{model.__tablename__} {record_id} not found",
        )
    return row


async def paginated_select(
    session: AsyncSession,
    query,
    page: int,
    page_size: int,
) -> tuple[list[Any], int]:
    """
    Execute a select with COUNT + paginated rows.
    Returns (rows, total_count).
    """
    count_q = select(func.count()).select_from(query.subquery())
    try:
        total = (await session.execute(count_q)).scalar_one()
        offset = (page - 1) * page_size
        rows = (
            await session.execute(query.offset(offset).limit(page_size))
        ).all()
    except OperationalError as exc:
        logger.error("DB connectivity error in paginated_select | error={}", exc)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")

    return rows, total
