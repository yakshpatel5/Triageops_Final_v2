"""
tests/test_escalation.py — Full escalation layer test suite (Week 4).

Covers:
  - PagerDuty trigger: happy path, rate limit retry, server error, rejection
  - OpsGenie trigger: happy path, priority mapping, responder passthrough
  - Webhook trigger: happy path, HMAC signing, 4xx non-retry, 5xx retry
  - Dispatcher: provider resolution (env vars), SKIPPED path, EscalationEvent persistence
  - Cron: expire_stale_approvals marks PENDING→EXPIRED, re-notifies CRITICAL
  - Cron: escalate_unactioned_criticals skips already-escalated alerts
  - models: EscalationEvent repr
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import respx
import httpx


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

ALERT_DB_ID   = str(uuid.uuid4())
APPROVAL_ID   = str(uuid.uuid4())
TENANT_ID     = "acme"
SLACK_USER    = "U12345678"

def _make_mock_alert(severity="CRITICAL"):
    a = MagicMock()
    a.id        = uuid.UUID(ALERT_DB_ID)
    a.alert_id  = "prtg-42"
    a.host      = "srv-prod-01"
    a.message   = "Disk C:\\ is 98% full"
    a.severity  = MagicMock(value=severity)
    a.tenant_id = TENANT_ID
    return a

def _make_mock_enrichment(decision="CRITICAL"):
    e = MagicMock()
    e.id                = uuid.uuid4()
    e.triage_decision   = decision
    e.confidence_score  = "0.9700"
    e.llm_reasoning     = "Disk at 98% — imminent failure."
    e.suggested_action  = "Verify before executing: df -h /"
    return e

def _make_mock_approval(action="ESCALATE", status="ACTIONED"):
    a = MagicMock()
    a.id             = uuid.UUID(APPROVAL_ID)
    a.alert_db_id    = uuid.UUID(ALERT_DB_ID)
    a.enrichment_id  = uuid.uuid4()
    a.tenant_id      = TENANT_ID
    a.slack_channel  = "#noc-critical"
    a.slack_ts       = "1700000000.123456"
    a.status         = status
    a.action         = action
    a.actioned_by    = SLACK_USER
    a.actioned_by_name = "jane.doe"
    a.actioned_at    = datetime.now(timezone.utc)
    a.expires_at     = datetime.now(timezone.utc) + timedelta(hours=24)
    a.slack_message_blocks = []
    return a


# ===========================================================================
# PagerDuty
# ===========================================================================

class TestPagerDuty:

    COMMON = dict(
        routing_key    = "test-routing-key",
        alert_db_id    = ALERT_DB_ID,
        alert_id       = "prtg-42",
        host           = "srv-prod-01",
        message        = "Disk full",
        severity       = "CRITICAL",
        triage_decision = "CRITICAL",
        llm_reasoning  = "Disk at 98%",
        suggested_action = "df -h",
        triggered_by   = SLACK_USER,
    )

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_success(self):
        from escalation.pagerduty import trigger_incident
        respx.post("https://events.pagerduty.com/v2/enqueue").mock(
            return_value=httpx.Response(202, json={"status": "success", "dedup_key": f"triageops-{ALERT_DB_ID}"})
        )
        result = await trigger_incident(**self.COMMON)
        assert result["response"]["status"] == "success"
        assert f"triageops-{ALERT_DB_ID}" in result["request"]["dedup_key"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_sets_dedup_key(self):
        from escalation.pagerduty import trigger_incident
        captured = {}
        def capture(req):
            captured["body"] = json.loads(req.content)
            return httpx.Response(202, json={"status": "success"})
        respx.post("https://events.pagerduty.com/v2/enqueue").mock(side_effect=capture)
        await trigger_incident(**self.COMMON)
        assert captured["body"]["dedup_key"] == f"triageops-{ALERT_DB_ID}"

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_includes_hallucination_caveat(self):
        from escalation.pagerduty import trigger_incident
        captured = {}
        def capture(req):
            captured["body"] = json.loads(req.content)
            return httpx.Response(202, json={"status": "success"})
        respx.post("https://events.pagerduty.com/v2/enqueue").mock(side_effect=capture)
        await trigger_incident(**self.COMMON)
        details = captured["body"]["payload"]["custom_details"]
        assert "verify before executing" in details["suggested_action"].lower()

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_retries_on_500(self):
        from escalation.pagerduty import trigger_incident, PagerDutyError
        # Always 500 — should exhaust 3 retries
        respx.post("https://events.pagerduty.com/v2/enqueue").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        with pytest.raises(PagerDutyError, match="server error"):
            await trigger_incident(**self.COMMON)

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_no_retry_on_400(self):
        from escalation.pagerduty import trigger_incident, PagerDutyError
        respx.post("https://events.pagerduty.com/v2/enqueue").mock(
            return_value=httpx.Response(400, json={"errors": ["invalid routing key"]})
        )
        with pytest.raises(PagerDutyError):
            await trigger_incident(**self.COMMON)

    @pytest.mark.asyncio
    @respx.mock
    async def test_severity_mapping_critical(self):
        from escalation.pagerduty import trigger_incident
        captured = {}
        def capture(req):
            captured["body"] = json.loads(req.content)
            return httpx.Response(202, json={"status": "success"})
        respx.post("https://events.pagerduty.com/v2/enqueue").mock(side_effect=capture)
        await trigger_incident(**self.COMMON)
        assert captured["body"]["payload"]["severity"] == "critical"


# ===========================================================================
# OpsGenie
# ===========================================================================

class TestOpsGenie:

    COMMON = dict(
        api_key        = "test-og-key",
        alert_db_id    = ALERT_DB_ID,
        alert_id       = "prtg-42",
        host           = "srv-prod-01",
        message        = "Disk full",
        severity       = "CRITICAL",
        triage_decision = "CRITICAL",
        llm_reasoning  = "Disk at 98%",
        suggested_action = "df -h",
        triggered_by   = SLACK_USER,
    )

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_success(self):
        from escalation.opsgenie import trigger_alert
        respx.post("https://api.opsgenie.com/v2/alerts").mock(
            return_value=httpx.Response(202, json={"result": "Request will be processed", "requestId": "abc-123"})
        )
        result = await trigger_alert(**self.COMMON)
        assert result["response"]["requestId"] == "abc-123"

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_sets_alias(self):
        from escalation.opsgenie import trigger_alert
        captured = {}
        def capture(req):
            captured["body"] = json.loads(req.content)
            return httpx.Response(202, json={"result": "ok"})
        respx.post("https://api.opsgenie.com/v2/alerts").mock(side_effect=capture)
        await trigger_alert(**self.COMMON)
        assert captured["body"]["alias"] == f"triageops-{ALERT_DB_ID}"

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_passes_responders(self):
        from escalation.opsgenie import trigger_alert
        captured = {}
        def capture(req):
            captured["body"] = json.loads(req.content)
            return httpx.Response(202, json={"result": "ok"})
        respx.post("https://api.opsgenie.com/v2/alerts").mock(side_effect=capture)
        responders = [{"name": "noc-team", "type": "team"}]
        await trigger_alert(**self.COMMON, responders=responders)
        assert captured["body"]["responders"] == responders

    @pytest.mark.asyncio
    @respx.mock
    async def test_priority_critical_maps_to_p1(self):
        from escalation.opsgenie import trigger_alert
        captured = {}
        def capture(req):
            captured["body"] = json.loads(req.content)
            return httpx.Response(202, json={"result": "ok"})
        respx.post("https://api.opsgenie.com/v2/alerts").mock(side_effect=capture)
        await trigger_alert(**self.COMMON)
        assert captured["body"]["priority"] == "P1"

    @pytest.mark.asyncio
    @respx.mock
    async def test_priority_noise_maps_to_p5(self):
        from escalation.opsgenie import trigger_alert
        captured = {}
        def capture(req):
            captured["body"] = json.loads(req.content)
            return httpx.Response(202, json={"result": "ok"})
        respx.post("https://api.opsgenie.com/v2/alerts").mock(side_effect=capture)
        await trigger_alert(**{**self.COMMON, "severity": "NOISE"})
        assert captured["body"]["priority"] == "P5"

    @pytest.mark.asyncio
    @respx.mock
    async def test_eu_endpoint_respected(self):
        from escalation.opsgenie import trigger_alert
        respx.post("https://api.eu.opsgenie.com/v2/alerts").mock(
            return_value=httpx.Response(202, json={"result": "ok"})
        )
        with patch("escalation.opsgenie.OPSGENIE_ALERTS_ENDPOINT",
                   "https://api.eu.opsgenie.com/v2/alerts"):
            result = await trigger_alert(**self.COMMON)
        assert result["response"]["result"] == "ok"


# ===========================================================================
# Generic Webhook
# ===========================================================================

class TestWebhook:

    COMMON = dict(
        url            = "https://example.com/escalation",
        secret         = "my-webhook-secret",
        alert_db_id    = ALERT_DB_ID,
        alert_id       = "prtg-42",
        host           = "srv-prod-01",
        message        = "Disk full",
        severity       = "CRITICAL",
        triage_decision = "CRITICAL",
        llm_reasoning  = "Disk at 98%",
        suggested_action = "df -h",
        triggered_by   = SLACK_USER,
    )

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_success(self):
        from escalation.webhook import trigger_webhook
        respx.post("https://example.com/escalation").mock(
            return_value=httpx.Response(200)
        )
        result = await trigger_webhook(**self.COMMON)
        assert result["response"]["status_code"] == 200

    @pytest.mark.asyncio
    @respx.mock
    async def test_hmac_signature_header_present(self):
        from escalation.webhook import trigger_webhook
        captured_headers = {}
        def capture(req):
            captured_headers.update(dict(req.headers))
            return httpx.Response(200)
        respx.post("https://example.com/escalation").mock(side_effect=capture)
        await trigger_webhook(**self.COMMON)
        assert "x-triageops-signature" in captured_headers
        assert captured_headers["x-triageops-signature"].startswith("sha256=")

    @pytest.mark.asyncio
    @respx.mock
    async def test_hmac_signature_is_valid(self):
        from escalation.webhook import trigger_webhook
        captured = {}
        def capture(req):
            captured["body"]    = req.content
            captured["headers"] = dict(req.headers)
            return httpx.Response(200)
        respx.post("https://example.com/escalation").mock(side_effect=capture)
        await trigger_webhook(**self.COMMON)

        secret = self.COMMON["secret"]
        expected_sig = "sha256=" + hmac.new(
            secret.encode(), captured["body"], hashlib.sha256
        ).hexdigest()
        assert captured["headers"]["x-triageops-signature"] == expected_sig

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_secret_omits_signature_header(self):
        from escalation.webhook import trigger_webhook
        captured_headers = {}
        def capture(req):
            captured_headers.update(dict(req.headers))
            return httpx.Response(200)
        respx.post("https://example.com/escalation").mock(side_effect=capture)
        await trigger_webhook(**{**self.COMMON, "secret": None})
        assert "x-triageops-signature" not in captured_headers

    @pytest.mark.asyncio
    @respx.mock
    async def test_hallucination_caveat_in_payload(self):
        from escalation.webhook import trigger_webhook
        captured = {}
        def capture(req):
            captured["body"] = json.loads(req.content)
            return httpx.Response(200)
        respx.post("https://example.com/escalation").mock(side_effect=capture)
        await trigger_webhook(**self.COMMON)
        action = captured["body"]["alert"]["suggested_action"]
        assert "verify before executing" in action.lower()

    @pytest.mark.asyncio
    @respx.mock
    async def test_4xx_raises_non_retryable(self):
        from escalation.webhook import trigger_webhook, WebhookError
        respx.post("https://example.com/escalation").mock(
            return_value=httpx.Response(403, text="Forbidden")
        )
        with pytest.raises(WebhookError):
            await trigger_webhook(**self.COMMON)

    @pytest.mark.asyncio
    @respx.mock
    async def test_5xx_retries(self):
        from escalation.webhook import trigger_webhook, WebhookError
        respx.post("https://example.com/escalation").mock(
            return_value=httpx.Response(503, text="Service Unavailable")
        )
        with pytest.raises(WebhookError, match="server error"):
            await trigger_webhook(**self.COMMON)


# ===========================================================================
# Dispatcher — provider resolution
# ===========================================================================

class TestDispatcherProviderResolution:

    def test_pagerduty_wins_over_opsgenie(self):
        from escalation.dispatcher import _resolve_provider
        with patch.dict("os.environ", {
            "PAGERDUTY_ROUTING_KEY": "pd-key",
            "OPSGENIE_API_KEY": "og-key",
        }):
            provider, cfg = _resolve_provider("acme")
        assert provider == "PAGERDUTY"
        assert cfg["routing_key"] == "pd-key"

    def test_opsgenie_used_when_no_pagerduty(self):
        from escalation.dispatcher import _resolve_provider
        env = {"OPSGENIE_API_KEY": "og-key"}
        # Make sure PAGERDUTY_ROUTING_KEY is not set
        with patch.dict("os.environ", env, clear=False):
            import os
            os.environ.pop("PAGERDUTY_ROUTING_KEY", None)
            provider, cfg = _resolve_provider("acme")
        assert provider == "OPSGENIE"

    def test_webhook_used_when_no_pd_or_og(self):
        from escalation.dispatcher import _resolve_provider
        with patch.dict("os.environ", {
            "ESCALATION_WEBHOOK_URL": "https://hooks.example.com/alert",
        }, clear=False):
            import os
            os.environ.pop("PAGERDUTY_ROUTING_KEY", None)
            os.environ.pop("OPSGENIE_API_KEY", None)
            provider, cfg = _resolve_provider("acme")
        assert provider == "WEBHOOK"
        assert cfg["url"] == "https://hooks.example.com/alert"

    def test_skipped_when_nothing_configured(self):
        from escalation.dispatcher import _resolve_provider
        with patch.dict("os.environ", {}, clear=True):
            # clear=True removes all env vars — isolated test
            import os
            for k in ["PAGERDUTY_ROUTING_KEY", "OPSGENIE_API_KEY", "ESCALATION_WEBHOOK_URL"]:
                os.environ.pop(k, None)
            provider, cfg = _resolve_provider("acme")
        assert provider == "SKIPPED"

    def test_tenant_specific_key_overrides_global(self):
        from escalation.dispatcher import _resolve_provider
        with patch.dict("os.environ", {
            "PAGERDUTY_ROUTING_KEY": "global-key",
            "PAGERDUTY_ROUTING_KEY_ACME": "tenant-key",
        }):
            provider, cfg = _resolve_provider("acme")
        assert cfg["routing_key"] == "tenant-key"


# ===========================================================================
# Dispatcher — full dispatch flow
# ===========================================================================

class TestDispatcherDispatch:

    @pytest.mark.asyncio
    async def test_dispatch_pagerduty_success(self):
        from escalation.dispatcher import dispatch_escalation

        approval   = _make_mock_approval()
        alert      = _make_mock_alert()
        enrichment = _make_mock_enrichment()

        mock_result = {"request": {"routing_key": "key"}, "response": {"status": "success"}}

        with patch.dict("os.environ", {"PAGERDUTY_ROUTING_KEY": "test-key"}):
            with patch("escalation.dispatcher.pd_trigger", new=AsyncMock(return_value=mock_result)):
                with patch("escalation.dispatcher.get_session") as mock_session_ctx:
                    # Mock the async context manager
                    mock_session = AsyncMock()
                    mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                    mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

                    event = await dispatch_escalation(approval, alert, enrichment)

        assert event.status == "TRIGGERED"
        assert event.provider == "PAGERDUTY"
        assert event.tenant_id == TENANT_ID

    @pytest.mark.asyncio
    async def test_dispatch_skipped_persists_event(self):
        from escalation.dispatcher import dispatch_escalation
        from escalation.pagerduty import PagerDutyError

        approval   = _make_mock_approval()
        alert      = _make_mock_alert()
        enrichment = _make_mock_enrichment()

        with patch.dict("os.environ", {}, clear=True):
            import os
            for k in ["PAGERDUTY_ROUTING_KEY", "OPSGENIE_API_KEY", "ESCALATION_WEBHOOK_URL"]:
                os.environ.pop(k, None)

            with patch("escalation.dispatcher.get_session") as mock_session_ctx:
                mock_session = AsyncMock()
                mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

                event = await dispatch_escalation(approval, alert, enrichment)

        assert event.status == "SKIPPED"
        mock_session.add.assert_called_once()   # EscalationEvent still persisted

    @pytest.mark.asyncio
    async def test_dispatch_failed_on_provider_error(self):
        from escalation.dispatcher import dispatch_escalation
        from escalation.pagerduty import PagerDutyError

        approval   = _make_mock_approval()
        alert      = _make_mock_alert()
        enrichment = _make_mock_enrichment()

        with patch.dict("os.environ", {"PAGERDUTY_ROUTING_KEY": "key"}):
            with patch("escalation.dispatcher.pd_trigger",
                       new=AsyncMock(side_effect=PagerDutyError("API error", retryable=False))):
                with patch("escalation.dispatcher.get_session") as mock_session_ctx:
                    mock_session = AsyncMock()
                    mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                    mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

                    event = await dispatch_escalation(approval, alert, enrichment)

        assert event.status == "FAILED"
        assert "API error" in event.error_detail


# ===========================================================================
# Cron tasks
# ===========================================================================

class TestCronExpireStaleApprovals:

    @pytest.mark.asyncio
    async def test_marks_pending_as_expired(self):
        from cron import _run_expire_stale_approvals

        expired_approval = _make_mock_approval(status="PENDING")
        expired_approval.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        # non-CRITICAL enrichment so no re-notify
        expired_approval.alert_db_id = uuid.uuid4()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [expired_approval]

        with patch("cron.get_session") as mock_ctx:
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("cron._get_triage_decision", new=AsyncMock(return_value="NOISE")):
                result = await _run_expire_stale_approvals()

        assert expired_approval.status == "EXPIRED"
        assert result["expired"] == 1

    @pytest.mark.asyncio
    async def test_no_expired_approvals_returns_zero(self):
        from cron import _run_expire_stale_approvals

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []

        with patch("cron.get_session") as mock_ctx:
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _run_expire_stale_approvals()

        assert result == {"expired": 0, "renotified": 0}

    @pytest.mark.asyncio
    async def test_critical_expired_sends_renotification(self):
        from cron import _run_expire_stale_approvals

        expired_approval = _make_mock_approval(status="PENDING")
        expired_approval.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [expired_approval]

        with patch("cron.get_session") as mock_ctx:
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("cron._get_triage_decision", new=AsyncMock(return_value="CRITICAL")):
                with patch("cron._send_expiry_renotification", new=AsyncMock()) as mock_notify:
                    result = await _run_expire_stale_approvals()

        mock_notify.assert_called_once_with(expired_approval)
        assert result["renotified"] == 1


class TestCronEscalateUnactionedCriticals:

    @pytest.mark.asyncio
    async def test_skips_already_escalated(self):
        from cron import _run_escalate_unactioned_criticals

        mock_result = MagicMock()
        mock_result.all.return_value = [
            (_make_mock_approval(), _make_mock_alert(), _make_mock_enrichment())
        ]

        with patch("cron.get_session") as mock_ctx:
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("cron._has_escalation_event", new=AsyncMock(return_value=True)):
                result = await _run_escalate_unactioned_criticals()

        assert result["skipped"] == 1
        assert result["escalated"] == 0

    @pytest.mark.asyncio
    async def test_escalates_unactioned_critical(self):
        from cron import _run_escalate_unactioned_criticals

        approval   = _make_mock_approval()
        alert      = _make_mock_alert()
        enrichment = _make_mock_enrichment()

        mock_result = MagicMock()
        mock_result.all.return_value = [(approval, alert, enrichment)]

        mock_event        = MagicMock()
        mock_event.status = "TRIGGERED"
        mock_event.provider = "PAGERDUTY"

        with patch("cron.get_session") as mock_ctx:
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("cron._has_escalation_event", new=AsyncMock(return_value=False)):
                with patch("cron.dispatch_escalation", new=AsyncMock(return_value=mock_event)):
                    result = await _run_escalate_unactioned_criticals()

        assert result["escalated"] == 1
        assert result["skipped"] == 0


# ===========================================================================
# Model smoke test
# ===========================================================================

class TestEscalationEventModel:

    def test_repr(self):
        from models import EscalationEvent
        import uuid as _uuid
        e = EscalationEvent(
            id=_uuid.uuid4(),
            approval_id=_uuid.uuid4(),
            alert_db_id=_uuid.UUID(ALERT_DB_ID),
            tenant_id=TENANT_ID,
            provider="PAGERDUTY",
            status="TRIGGERED",
            triggered_at=datetime.now(timezone.utc),
        )
        r = repr(e)
        assert "PAGERDUTY" in r
        assert "TRIGGERED" in r
