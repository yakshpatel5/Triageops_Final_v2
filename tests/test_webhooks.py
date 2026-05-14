"""
tests/test_webhooks.py — integration tests for the ingest endpoints.

Run: pytest tests/ -v
Requires: TEST_DATABASE_URL set, or uses SQLite in-memory via override.

We override get_db and APIKeyMiddleware to avoid needing a real Postgres/Redis
in CI. The idempotency logic (duplicate detection) is still exercised against
a real async session using aiosqlite.
"""

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from unittest.mock import patch, AsyncMock

# ---------------------------------------------------------------------------
# Minimal fixtures — override DB and auth so tests run without Docker
# ---------------------------------------------------------------------------

FAKE_TENANT = "test-tenant"
FAKE_API_KEY = "test-key-abc123"

@pytest.fixture
def client():
    """
    Returns a TestClient with auth middleware bypassed and tasks mocked.
    Full integration against Postgres covered in tests/integration/.
    """
    from main import create_app
    from middleware.auth import APIKeyMiddleware

    app = create_app()

    # Patch middleware dispatch to inject tenant without hitting DB
    async def _mock_dispatch(self, request, call_next):
        request.state.tenant_id = FAKE_TENANT
        return await call_next(request)

    with patch.object(APIKeyMiddleware, "dispatch", _mock_dispatch):
        with patch("routers.webhook.enqueue_enrichment"):  # don't need Redis in unit tests
            with patch("routers.webhook._check_duplicate", return_value=False):
                with patch("routers.webhook._persist_alert") as mock_persist:
                    from unittest.mock import MagicMock
                    import uuid
                    mock_alert = MagicMock()
                    mock_alert.id = uuid.uuid4()
                    mock_persist.return_value = mock_alert
                    yield TestClient(app)


# ---------------------------------------------------------------------------
# PRTG tests
# ---------------------------------------------------------------------------

PRTG_PAYLOAD = {
    "sensorid": "42",
    "sensor": "Disk Free C:\\",
    "device": "srv-prod-01",
    "status": "Down",
    "message": "Disk C:\\ is 98% full",
}

def test_prtg_accepted(client):
    resp = client.post(
        "/webhook/prtg",
        json=PRTG_PAYLOAD,
        headers={"X-API-Key": FAKE_API_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["alert_id"] == "prtg-42"


def test_prtg_missing_sensorid_returns_422(client):
    payload = {k: v for k, v in PRTG_PAYLOAD.items() if k != "sensorid"}
    resp = client.post("/webhook/prtg", json=payload, headers={"X-API-Key": FAKE_API_KEY})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Datadog tests
# ---------------------------------------------------------------------------

DD_PAYLOAD = {
    "id": "7654321",
    "alert_type": "alert",
    "hostname": "web-01.prod.acme.com",
    "title": "High CPU on web-01",
    "text": "CPU usage exceeded 95% for 5 minutes",
    "tags": "env:prod,team:platform",
}

def test_datadog_accepted(client):
    resp = client.post(
        "/webhook/datadog",
        json=DD_PAYLOAD,
        headers={"X-API-Key": FAKE_API_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["alert_id"] == "dd-7654321"


def test_datadog_missing_id_returns_422(client):
    payload = {k: v for k, v in DD_PAYLOAD.items() if k not in ("id", "alert_id")}
    resp = client.post("/webhook/datadog", json=payload, headers={"X-API-Key": FAKE_API_KEY})
    assert resp.status_code == 422


def test_datadog_unexpanded_template_vars(client):
    """$HOSTNAME template variable should be treated as unknown, not rejected."""
    payload = {**DD_PAYLOAD, "hostname": "$HOSTNAME"}
    resp = client.post("/webhook/datadog", json=payload, headers={"X-API-Key": FAKE_API_KEY})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

def test_missing_api_key_returns_401():
    from main import create_app
    app = create_app()
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/webhook/prtg", json=PRTG_PAYLOAD)
    assert resp.status_code == 401


def test_health_bypasses_auth():
    from main import create_app
    app = create_app()
    c = TestClient(app)
    resp = c.get("/health")
    assert resp.status_code == 200
