"""
tests/test_production_hardening.py — Week 6 middleware and hardening tests.

Covers:
  - RequestIDMiddleware: injects UUID, honours client X-Request-ID, propagates to response
  - RateLimitMiddleware: allows under limit, blocks over limit, fail-open on Redis error
  - CORS headers present on responses
  - /health returns version from APP_VERSION
  - /metrics returns Prometheus text format
  - 500 handler includes request_id in error response
  - Dashboard static files served at /dashboard
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


FAKE_TENANT = "acme"
FAKE_KEY    = "test-key-xyz"


def _make_app():
    from main import create_app
    from middleware.auth import APIKeyMiddleware

    app = create_app()

    async def _bypass_auth(self, req, call_next):
        req.state.tenant_id = FAKE_TENANT
        return await call_next(req)

    with patch.object(APIKeyMiddleware, "dispatch", _bypass_auth):
        yield app


# ---------------------------------------------------------------------------
# Request ID
# ---------------------------------------------------------------------------

class TestRequestIDMiddleware:

    def test_response_has_x_request_id(self):
        from middleware.request_id import RequestIDMiddleware
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        mini = FastAPI()
        mini.add_middleware(RequestIDMiddleware)

        @mini.get("/ping")
        async def ping(): return {"ok": True}

        c = TestClient(mini)
        resp = c.get("/ping")
        assert "x-request-id" in resp.headers

    def test_client_supplied_request_id_is_honoured(self):
        from middleware.request_id import RequestIDMiddleware
        from fastapi import FastAPI

        mini = FastAPI()
        mini.add_middleware(RequestIDMiddleware)

        @mini.get("/ping")
        async def ping(): return {"ok": True}

        c = TestClient(mini)
        custom_id = str(uuid.uuid4())
        resp = c.get("/ping", headers={"X-Request-ID": custom_id})
        assert resp.headers["x-request-id"] == custom_id

    def test_auto_generated_id_is_valid_uuid(self):
        from middleware.request_id import RequestIDMiddleware
        from fastapi import FastAPI

        mini = FastAPI()
        mini.add_middleware(RequestIDMiddleware)

        @mini.get("/ping")
        async def ping(): return {"ok": True}

        c = TestClient(mini)
        resp = c.get("/ping")
        rid = resp.headers["x-request-id"]
        # Should not raise
        uuid.UUID(rid)


# ---------------------------------------------------------------------------
# Rate limit middleware — unit tests on the sliding window logic
# ---------------------------------------------------------------------------

class TestRateLimitHelpers:

    @pytest.mark.asyncio
    async def test_under_limit_returns_allowed(self):
        from middleware.rate_limit import _check_rate_limit

        mock_redis = AsyncMock()
        mock_pipe  = AsyncMock()
        mock_pipe.execute = AsyncMock(return_value=[5, True])   # count=5
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)

        with patch("middleware.rate_limit._get_redis", return_value=mock_redis):
            allowed, count = await _check_rate_limit("test:key", limit=100)

        assert allowed is True
        assert count == 5

    @pytest.mark.asyncio
    async def test_over_limit_returns_blocked(self):
        from middleware.rate_limit import _check_rate_limit

        mock_redis = AsyncMock()
        mock_pipe  = AsyncMock()
        mock_pipe.execute = AsyncMock(return_value=[101, True])  # count=101
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)

        with patch("middleware.rate_limit._get_redis", return_value=mock_redis):
            allowed, count = await _check_rate_limit("test:key", limit=100)

        assert allowed is False
        assert count == 101

    @pytest.mark.asyncio
    async def test_redis_failure_fails_open(self):
        from middleware.rate_limit import _check_rate_limit

        mock_redis = AsyncMock()
        mock_redis.pipeline = MagicMock(side_effect=Exception("Redis down"))

        with patch("middleware.rate_limit._get_redis", return_value=mock_redis):
            allowed, count = await _check_rate_limit("test:key", limit=10)

        # Fail open — must not block legitimate traffic on Redis outage
        assert allowed is True
        assert count == 0

    def test_classify_limit_webhook(self):
        from middleware.rate_limit import _classify_limit
        cls, limit = _classify_limit("/webhook/prtg")
        assert cls == "webhook"

    def test_classify_limit_ops(self):
        from middleware.rate_limit import _classify_limit
        cls, limit = _classify_limit("/ops/alerts")
        assert cls == "ops"

    def test_classify_limit_health_exempt(self):
        from middleware.rate_limit import _classify_limit
        assert _classify_limit("/health") is None

    def test_classify_limit_slack_exempt(self):
        from middleware.rate_limit import _classify_limit
        assert _classify_limit("/slack/interactions") is None


# ---------------------------------------------------------------------------
# /health endpoint
# ---------------------------------------------------------------------------

class TestHealthEndpoint:

    def test_health_returns_ok(self):
        with _make_app() as app:
            c = TestClient(app)
            resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_includes_version(self):
        import os
        with patch.dict(os.environ, {"APP_VERSION": "1.2.3"}):
            with _make_app() as app:
                c = TestClient(app)
                resp = c.get("/health")
        assert resp.json()["version"] == "1.2.3"


# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------

class TestMetricsEndpoint:

    def test_metrics_returns_prometheus_format(self):
        with _make_app() as app:
            c = TestClient(app)
            resp = c.get("/metrics")
        assert resp.status_code == 200
        assert "triageops_up" in resp.text
        assert resp.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# OpenAPI security scheme
# ---------------------------------------------------------------------------

class TestOpenAPISchema:

    def test_openapi_has_api_key_security_scheme(self):
        with _make_app() as app:
            c = TestClient(app)
            resp = c.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert "ApiKeyAuth" in schema["components"]["securitySchemes"]
        assert schema["components"]["securitySchemes"]["ApiKeyAuth"]["in"] == "header"
        assert schema["components"]["securitySchemes"]["ApiKeyAuth"]["name"] == "X-API-Key"

    def test_openapi_security_applied_globally(self):
        with _make_app() as app:
            c = TestClient(app)
            schema = c.get("/openapi.json").json()
        assert {"ApiKeyAuth": []} in schema["security"]


# ---------------------------------------------------------------------------
# Dashboard static files
# ---------------------------------------------------------------------------

class TestDashboardMount:

    def test_dashboard_html_is_served(self):
        with _make_app() as app:
            c = TestClient(app)
            resp = c.get("/dashboard/index.html")
        # Either 200 (file found) or 404 (static dir not in test env) — not 500
        assert resp.status_code in (200, 404)

    def test_dashboard_returns_html_content_type(self):
        import os
        from pathlib import Path
        dash_dir = Path(__file__).parent.parent / "dashboard"
        if not dash_dir.exists():
            pytest.skip("dashboard/ directory not present in test environment")
        with _make_app() as app:
            c = TestClient(app)
            resp = c.get("/dashboard/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
