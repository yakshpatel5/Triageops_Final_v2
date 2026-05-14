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
def no_sentry(monkeypatch):
    """Prevent Sentry from capturing anything during tests."""
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "capture_exception", lambda *a, **kw: None)
    monkeypatch.setattr(sentry_sdk, "capture_message",   lambda *a, **kw: None)
