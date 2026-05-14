"""
API key authentication middleware.

Flow:
  1. Extract X-API-Key header from every request to /webhook/*
  2. Hash the key (SHA-256) and look up in api_keys table
  3. Attach tenant_id to request.state for downstream use
  4. Return 401/403 on failure — never leak whether a key exists

Security notes:
  - Keys are stored as SHA-256 hashes. The raw key is never logged.
  - Timing-safe comparison is handled by the DB lookup (not hmac.compare_digest)
    which is acceptable for V1. Upgrade to constant-time comparison in V1.1.
  - Rate limiting (per-key) is a TODO — add Redis token bucket in V1.2.
"""

import hashlib
import os
from typing import Callable

import sentry_sdk
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from db.session import AsyncSessionLocal
from models import ApiKey

# Paths that bypass auth (health checks, docs)
_AUTH_EXEMPT_PREFIXES = ("/health", "/docs", "/openapi", "/redoc")

# Seed a static master key from env for bootstrapping (e.g. creating first tenant).
# In prod, rotate this and store in a secrets manager.
_BOOTSTRAP_API_KEY_HASH: str | None = None
if _raw := os.getenv("BOOTSTRAP_API_KEY"):
    _BOOTSTRAP_API_KEY_HASH = hashlib.sha256(_raw.encode()).hexdigest()


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def _resolve_tenant(key_hash: str, session: AsyncSession) -> str | None:
    """Return tenant_id for an active key hash, or None if not found/inactive."""
    result = await session.execute(
        select(ApiKey.tenant_id)
        .where(ApiKey.key_hash == key_hash)
        .where(ApiKey.is_active == "1")
        .limit(1)
    )
    row = result.first()
    return row[0] if row else None


class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware — runs before every request.
    Attaches `request.state.tenant_id` for authenticated routes.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip auth for exempt paths
        if any(request.url.path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            return await call_next(request)

        raw_key = request.headers.get("X-API-Key", "").strip()
        if not raw_key:
            logger.warning(
                "Rejected request — missing X-API-Key | path={} ip={}",
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "X-API-Key header required"},
            )

        key_hash = _hash_key(raw_key)

        # Fast path: bootstrap key bypasses DB lookup
        if _BOOTSTRAP_API_KEY_HASH and key_hash == _BOOTSTRAP_API_KEY_HASH:
            request.state.tenant_id = "bootstrap"
            logger.debug("Bootstrap API key used | path={}", request.url.path)
            return await call_next(request)

        try:
            async with AsyncSessionLocal() as session:
                tenant_id = await _resolve_tenant(key_hash, session)
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            logger.error("Auth DB lookup failed: {}", exc)
            return JSONResponse(
                status_code=503,
                content={"detail": "Authentication service temporarily unavailable"},
            )

        if not tenant_id:
            logger.warning(
                "Rejected request — invalid or inactive API key | path={} ip={}",
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid or inactive API key"},
            )

        request.state.tenant_id = tenant_id
        logger.debug(
            "Authenticated | tenant={} path={}",
            tenant_id,
            request.url.path,
        )
        return await call_next(request)
