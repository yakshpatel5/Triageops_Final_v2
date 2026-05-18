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

import bcrypt
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


def _hash_key_sha256(raw_key: str) -> str:
    """Legacy SHA-256 hashing."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _hash_key_bcrypt(raw_key: str) -> str:
    """New bcrypt hashing."""
    return bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()


def _verify_key_bcrypt(raw_key: str, hashed_key: str) -> bool:
    """Verify a raw key against a bcrypt hash."""
    try:
        return bcrypt.checkpw(raw_key.encode(), hashed_key.encode())
    except Exception:
        return False


async def _resolve_tenant(raw_key: str, session: AsyncSession) -> str | None:
    """Return tenant_id for an active key, or None if not found/inactive."""
    # 1. Try legacy SHA-256 first (fast path for old keys)
    legacy_hash = _hash_key_sha256(raw_key)
    result = await session.execute(
        select(ApiKey.tenant_id, ApiKey.key_hash)
        .where(ApiKey.is_active == "1")
    )
    
    # This is a bit inefficient if there are thousands of keys, 
    # but for a V1 it's safer to check all active keys.
    # In a real production system, we'd store the hash type in the DB.
    for row in result.all():
        tenant_id, stored_hash = row
        
        # Check legacy SHA-256
        if stored_hash == legacy_hash:
            return tenant_id
            
        # Check bcrypt
        if stored_hash.startswith("$2b$") and _verify_key_bcrypt(raw_key, stored_hash):
            return tenant_id
            
    return None


class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware — runs before every request.
    Attaches `request.state.tenant_id` for authenticated routes.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip auth for exempt paths
        if any(request.url.path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            return await call_next(request)

        # 1. Try X-API-Key header (standard for webhooks)
        raw_key = request.headers.get("X-API-Key", "").strip()
        
        # 2. Fallback to httpOnly cookie (standard for dashboard)
        if not raw_key:
            raw_key = request.cookies.get("triageops_session", "").strip()

        if not raw_key:
            logger.warning(
                "Rejected request — missing authentication | path={} ip={}",
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required (X-API-Key header or session cookie)"},
            )

        # Fast path: bootstrap key bypasses DB lookup (still using SHA-256 for the env var)
        if _BOOTSTRAP_API_KEY_HASH and _hash_key_sha256(raw_key) == _BOOTSTRAP_API_KEY_HASH:
            request.state.tenant_id = "bootstrap"
            logger.debug("Bootstrap API key used | path={}", request.url.path)
            return await call_next(request)

        try:
            async with AsyncSessionLocal() as session:
                tenant_id = await _resolve_tenant(raw_key, session)
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
