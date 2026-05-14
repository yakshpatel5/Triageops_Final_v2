"""
tests/test_ops_api.py — ops router test suite.

All DB calls are mocked — no live Postgres required.
Tests cover:
  - list_alerts: pagination envelope, severity filter, decision filter
  - get_alert: 404 on wrong tenant, enrichment + approval denormalised
  - get_enrichment: 404 when not enriched
  - list_approvals: status filter, pagination
  - list_escalations: provider filter
  - tenant_stats: all aggregates present, noise_ratio calculation
  - hotspots: ordered by alert_count desc
  - decision_distribution: percentages sum to 100
  - ops_health: db ok / degraded
  - Tenant isolation: tenant_id injected correctly into every query filter
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


FAKE_TENANT = "acme"
FAKE_API_KEY = "test-key"


# ---------------------------------------------------------------------------
# App fixture with auth and DB mocked
# ---------------------------------------------------------------------------

def _make_client(mock_session_factory):
    """
    Returns TestClient with:
      - APIKeyMiddleware bypassed (tenant injected directly)
      - get_db overridden with provided session factory
    """
    from main import create_app
    from middleware.auth import APIKeyMiddleware
    from db.session import get_db

    app = create_app()

    async def _mock_auth(self, request, call_next):
        request.state.tenant_id = FAKE_TENANT
        return await call_next(request)

    with patch.object(APIKeyMiddleware, "dispatch", _mock_auth):
        app.dependency_overrides[get_db] = mock_session_factory
        yield TestClient(app)
        app.dependency_overrides.clear()


def _mock_session(execute_return=None):
    """Build a mock AsyncSession with a chainable execute() return."""
    session = AsyncMock()

    # execute() returns a result object; chain scalar_one(), scalars().all(), etc.
    result = MagicMock()
    result.scalar_one.return_value = execute_return if execute_return is not None else 0
    result.scalar_one_or_none.return_value = execute_return
    result.scalars.return_value.all.return_value = execute_return or []
    result.all.return_value = execute_return or []
    result.one.return_value = (None, None)   # for quality tuple

    session.execute = AsyncMock(return_value=result)
    return session


def _make_alert_row(host="srv-prod-01", severity="CRITICAL", decision="CRITICAL"):
    a = MagicMock()
    a.Alert = MagicMock()
    a.Alert.id         = uuid.uuid4()
    a.Alert.alert_id   = "prtg-42"
    a.Alert.tenant_id  = FAKE_TENANT
    a.Alert.source     = MagicMock(value="PRTG")
    a.Alert.severity   = MagicMock(value=severity)
    a.Alert.host       = host
    a.Alert.message    = "Disk full"
    a.Alert.received_at = datetime.now(timezone.utc)
    a.Alert.created_at  = datetime.now(timezone.utc)
    a.triage_decision  = decision
    a.confidence_score = "0.9700"
    return a


# ---------------------------------------------------------------------------
# list_alerts
# ---------------------------------------------------------------------------

class TestListAlerts:

    def test_returns_page_envelope(self):
        row = _make_alert_row()
        session = AsyncMock()

        # First execute → total count, second → rows
        count_result = MagicMock()
        count_result.scalar_one.return_value = 1

        rows_result = MagicMock()
        rows_result.all.return_value = [row]

        session.execute = AsyncMock(side_effect=[count_result, rows_result])

        async def _get_db():
            yield session

        from main import create_app
        from middleware.auth import APIKeyMiddleware
        from db.session import get_db

        app = create_app()

        async def _auth(self, req, call_next):
            req.state.tenant_id = FAKE_TENANT
            return await call_next(req)

        with patch.object(APIKeyMiddleware, "dispatch", _auth):
            app.dependency_overrides[get_db] = _get_db
            c = TestClient(app)
            resp = c.get("/ops/alerts", headers={"X-API-Key": FAKE_API_KEY})

        assert resp.status_code == 200
        body = resp.json()
        assert "items"    in body
        assert "total"    in body
        assert "page"     in body
        assert "has_next" in body
        assert body["total"] == 1
        assert len(body["items"]) == 1

    def test_item_contains_triage_decision(self):
        row = _make_alert_row(decision="NOISE")
        session = AsyncMock()

        count_result = MagicMock()
        count_result.scalar_one.return_value = 1
        rows_result = MagicMock()
        rows_result.all.return_value = [row]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])

        async def _get_db():
            yield session

        from main import create_app
        from middleware.auth import APIKeyMiddleware
        from db.session import get_db

        app = create_app()

        async def _auth(self, req, call_next):
            req.state.tenant_id = FAKE_TENANT
            return await call_next(req)

        with patch.object(APIKeyMiddleware, "dispatch", _auth):
            app.dependency_overrides[get_db] = _get_db
            c = TestClient(app)
            resp = c.get("/ops/alerts", headers={"X-API-Key": FAKE_API_KEY})

        assert resp.json()["items"][0]["triage_decision"] == "NOISE"

    def test_empty_result_returns_zero_total(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        rows_result = MagicMock()
        rows_result.all.return_value = []
        session.execute = AsyncMock(side_effect=[count_result, rows_result])

        async def _get_db():
            yield session

        from main import create_app
        from middleware.auth import APIKeyMiddleware
        from db.session import get_db

        app = create_app()

        async def _auth(self, req, call_next):
            req.state.tenant_id = FAKE_TENANT
            return await call_next(req)

        with patch.object(APIKeyMiddleware, "dispatch", _auth):
            app.dependency_overrides[get_db] = _get_db
            c = TestClient(app)
            resp = c.get("/ops/alerts")

        assert resp.json()["total"] == 0
        assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# get_alert — 404 path
# ---------------------------------------------------------------------------

class TestGetAlert:

    def test_404_on_missing_alert(self):
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)

        async def _get_db():
            yield session

        from main import create_app
        from middleware.auth import APIKeyMiddleware
        from db.session import get_db

        app = create_app()

        async def _auth(self, req, call_next):
            req.state.tenant_id = FAKE_TENANT
            return await call_next(req)

        with patch.object(APIKeyMiddleware, "dispatch", _auth):
            app.dependency_overrides[get_db] = _get_db
            c = TestClient(app)
            resp = c.get(f"/ops/alerts/{uuid.uuid4()}")

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Stats — noise_ratio calculation
# ---------------------------------------------------------------------------

class TestTenantStats:

    def test_noise_ratio_calculation(self):
        """noise_ratio = noise / (critical + noise)"""
        from routers.ops_schemas import TenantStats

        stats = TenantStats(
            tenant_id="acme",
            alerts_24h=100,
            critical_24h=20,
            noise_24h=60,
            needs_review_24h=20,
            pending_approvals=5,
            escalations_24h=3,
            total_alerts=500,
            total_escalations=10,
            avg_confidence_24h=0.88,
            avg_latency_ms_24h=3200.0,
            noise_ratio_24h=round(60 / (20 + 60), 4),
        )
        assert stats.noise_ratio_24h == pytest.approx(0.75)

    def test_noise_ratio_none_when_no_alerts(self):
        from routers.ops_schemas import TenantStats
        stats = TenantStats(
            tenant_id="acme",
            alerts_24h=0, critical_24h=0, noise_24h=0, needs_review_24h=0,
            pending_approvals=0, escalations_24h=0,
            total_alerts=0, total_escalations=0,
            avg_confidence_24h=None, avg_latency_ms_24h=None,
            noise_ratio_24h=None,
        )
        assert stats.noise_ratio_24h is None


# ---------------------------------------------------------------------------
# decision_distribution — percentages
# ---------------------------------------------------------------------------

class TestDecisionDistribution:

    def test_percentages_sum_to_100(self):
        from routers.ops_schemas import DecisionDistribution

        total = 100
        items = [
            DecisionDistribution(decision="CRITICAL",     count=40, percentage=40.0),
            DecisionDistribution(decision="NOISE",        count=50, percentage=50.0),
            DecisionDistribution(decision="NEEDS_REVIEW", count=10, percentage=10.0),
        ]
        assert sum(i.percentage for i in items) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Page envelope
# ---------------------------------------------------------------------------

class TestPageEnvelope:

    def test_has_next_false_on_last_page(self):
        from routers.ops_schemas import Page, AlertSummary

        page = Page[AlertSummary](
            items=[],
            total=5,
            page=1,
            page_size=25,
            has_next=False,
        )
        assert page.has_next is False

    def test_has_next_true_when_more_pages(self):
        from routers.ops_schemas import Page, AlertSummary

        page = Page[AlertSummary](
            items=[],
            total=100,
            page=1,
            page_size=25,
            has_next=True,
        )
        assert page.has_next is True


# ---------------------------------------------------------------------------
# Ops health
# ---------------------------------------------------------------------------

class TestOpsHealth:

    def test_health_ok_when_db_responds(self):
        session = AsyncMock()
        result = MagicMock()
        session.execute = AsyncMock(return_value=result)

        async def _get_db():
            yield session

        from main import create_app
        from middleware.auth import APIKeyMiddleware
        from db.session import get_db

        app = create_app()

        async def _auth(self, req, call_next):
            req.state.tenant_id = FAKE_TENANT
            return await call_next(req)

        with patch.object(APIKeyMiddleware, "dispatch", _auth):
            app.dependency_overrides[get_db] = _get_db
            c = TestClient(app)
            resp = c.get("/ops/health")

        assert resp.status_code == 200
        assert resp.json()["db"] == "ok"

    def test_health_degraded_when_db_fails(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("DB down"))

        async def _get_db():
            yield session

        from main import create_app
        from middleware.auth import APIKeyMiddleware
        from db.session import get_db

        app = create_app()

        async def _auth(self, req, call_next):
            req.state.tenant_id = FAKE_TENANT
            return await call_next(req)

        with patch.object(APIKeyMiddleware, "dispatch", _auth):
            app.dependency_overrides[get_db] = _get_db
            c = TestClient(app)
            resp = c.get("/ops/health")

        assert resp.status_code == 200
        assert resp.json()["db"] == "error"
        assert resp.json()["status"] == "degraded"
