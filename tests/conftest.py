"""
conftest.py — shared pytest fixtures for all tests.

Sets required environment variables so tests can import app modules
without crashing on missing config. No real services are started.
"""

import os
import pytest

# Set env vars before any app imports happen
os.environ.setdefault("DATABASE_URL",      "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL",         "redis://localhost:6379/0")
os.environ.setdefault("OPENAI_API_KEY",    "sk-test-key")
os.environ.setdefault("SLACK_BOT_TOKEN",   "xoxb-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret-32chars-here!")
os.environ.setdefault("BOOTSTRAP_API_KEY", "test-bootstrap-key")
os.environ.setdefault("APP_ENV",           "development")
os.environ.setdefault("SENTRY_DSN",        "")


@pytest.fixture(autouse=True)
def clear_prometheus_registry():
    from prometheus_client import REGISTRY
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        try:
            REGISTRY.unregister(collector)
        except (KeyError, ValueError):
            pass
    yield

@pytest.fixture(autouse=True)
def mock_metrics():
    """Patch metrics functions to return MagicMocks and avoid Prometheus registry issues in tests."""
    from unittest.mock import AsyncMock, MagicMock, patch
    
    # Create a mock that behaves like a metric (supports .labels().inc(), etc.)
    mock_metric = MagicMock()
    mock_metric.labels.return_value = mock_metric
    
    with patch("metrics.instrumentation.get_alerts_total", return_value=mock_metric), \
         patch("metrics.instrumentation.get_enrichment_duration", return_value=mock_metric), \
         patch("metrics.instrumentation.get_enrichment_errors_total", return_value=mock_metric), \
         patch("metrics.instrumentation.get_queue_depth", return_value=mock_metric), \
         patch("metrics.instrumentation.get_circuit_breaker_state", return_value=mock_metric), \
         patch("metrics.instrumentation.get_metrics_app", return_value=MagicMock()), \
         patch("main.get_metrics_app", return_value=MagicMock()), \
         patch("db.session.init_db", new=AsyncMock()), \
         patch("db.session.close_db", new=AsyncMock()), \
         patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=MagicMock()), \
         patch("db.session.engine", new=MagicMock()), \
         patch("db.session.AsyncSessionLocal", new=MagicMock()), \
         patch("db.session.engine.begin", new=AsyncMock(return_value=AsyncMock())), \
         patch("db.session.engine.dispose", new=AsyncMock()), \
         patch("prometheus_client.REGISTRY", new=MagicMock()):
        yield

@pytest.fixture(autouse=True)
def no_sentry(monkeypatch):
    """Prevent Sentry from capturing anything during tests."""
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "capture_exception", lambda *a, **kw: None)
    monkeypatch.setattr(sentry_sdk, "capture_message",   lambda *a, **kw: None)
