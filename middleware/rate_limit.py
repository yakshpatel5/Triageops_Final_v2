"""
middleware/rate_limit.py — Per-tenant Redis token bucket rate limiting.

Applied to webhook ingest endpoints to prevent a single noisy tenant from
saturating the enrichment queue or the OpenAI budget.

Limits (all configurable via env):
  - Webhook ingest:   100 req/min per tenant  (RATE_LIMIT_WEBHOOK_PER_MIN)
  - Ops API:           60 req/min per tenant  (RATE_LIMIT_OPS_PER_MIN)
  - Global per-IP:    200 req/min             (RATE_LIMIT_IP_PER_MIN)

Algorithm: sliding window counter in Redis (INCR + EXPIRE).
Chosen over token bucket for simplicity — acceptable for these volumes.
True token bucket preferred for burst-sensitive endpoints (add in V1.2).

Returns 429 with Retry-After header on limit breach.
"""

from __future__ import annotations

import os
import time
from typing import Callable

import redis.asyncio as aioredis
import sentry_sdk
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Redis client — lazy singleton
# ---------------------------------------------------------------------------

_redis: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _redis = aioredis.from_url(url, encoding="utf-8", decode_responses=True)
    return _redis


# ---------------------------------------------------------------------------
# Limits config
# ---------------------------------------------------------------------------

_WEBHOOK_LIMIT = int(os.getenv("RATE_LIMIT_WEBHOOK_PER_MIN", "100"))
_OPS_LIMIT     = int(os.getenv("RATE_LIMIT_OPS_PER_MIN",     "60"))
_IP_LIMIT      = int(os.getenv("RATE_LIMIT_IP_PER_MIN",      "200"))

# Paths exempt from rate limiting
_EXEMPT = ("/health", "/docs", "/openapi", "/redoc", "/slack/interactions")


def _classify_limit(path: str) -> tuple[str, int] | None:
    """Return (endpoint_class, limit) or None if exempt."""
    if any(path.startswith(e) for e in _EXEMPT):
        return None
    if path.startswith("/webhook/"):
        return "webhook", _WEBHOOK_LIMIT
    if path.startswith("/ops/"):
        return "ops", _OPS_LIMIT
    return "general", _OPS_LIMIT


async def _check_rate_limit(key: str, limit: int, window_secs: int = 60) -> tuple[bool, int]:
    """
    Sliding window counter. Returns (allowed, current_count).
    Uses Redis INCR + EXPIRE — atomic enough for our SLO.
    """
    r = _get_redis()
    try:
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_secs)
        results = await pipe.execute()
        count = results[0]
        return count <= limit, count
    except Exception as exc:
        # Redis failure → fail open (don't block legitimate traffic)
        logger.warning("Rate limit Redis error (fail open): {}", exc)
        return True, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter.
    Keys: rl:{endpoint_class}:{tenant_id}:{minute_bucket}
    Applies tenant-level limits where tenant_id is known (post-auth),
    IP-level limits everywhere as a backstop.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        classification = _classify_limit(path)
        if classification is None:
            return await call_next(request)

        endpoint_class, limit = classification
        minute_bucket = int(time.time() // 60)
        client_ip = request.client.host if request.client else "unknown"

        # Tenant-level limit
        # Note: APIKeyMiddleware runs BEFORE RateLimitMiddleware in main.py,
        # so request.state.tenant_id should be available for authenticated routes.
        tenant_id = getattr(request.state, "tenant_id", None)
        
        # If tenant_id is not set but it's a webhook/ops path, it might be an unauth request
        # that will be caught by APIKeyMiddleware later, but we can still rate limit by IP.
        
        if tenant_id:
            key = f"rl:{endpoint_class}:{tenant_id}:{minute_bucket}"
            allowed, count = await _check_rate_limit(key, limit)
            if not allowed:
                logger.warning(
                    "Rate limit exceeded | tenant={} endpoint={} count={} limit={}",
                    tenant_id, endpoint_class, count, limit,
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded for {endpoint_class}: {limit} req/min"},
                    headers={"Retry-After": "60"},
                )

        # IP-level backstop (catches unauthenticated flood)
        ip_key = f"rl:ip:{client_ip}:{minute_bucket}"
        ip_allowed, ip_count = await _check_rate_limit(ip_key, _IP_LIMIT)
        if not ip_allowed:
            logger.warning(
                "IP rate limit exceeded | ip={} count={}", client_ip, ip_count
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={"Retry-After": "60"},
            )

        response = await call_next(request)
        # Expose limit headers for client visibility
        if tenant_id:
            response.headers["X-RateLimit-Limit"]  = str(limit)
            response.headers["X-RateLimit-Window"] = "60"
        return response
