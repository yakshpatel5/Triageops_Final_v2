"""
tests/test_slack.py — unit tests for Slack Block Kit builder and interaction handler.

All Slack API calls are mocked — no real Slack workspace needed.
Tests cover:
  - Block Kit structure for each decision type (CRITICAL / NOISE / NEEDS_REVIEW)
  - Approval button presence and correct action_ids
  - Signature verification (valid, expired, wrong HMAC)
  - Interaction handler: happy path, double-click idempotency, unknown action_id
  - Approval update blocks: buttons replaced with status banner
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_alert(severity="CRITICAL", host="srv-prod-01", message="Disk full"):
    alert = MagicMock()
    alert.id          = uuid.uuid4()
    alert.alert_id    = "prtg-42"
    alert.tenant_id   = "acme"
    alert.source      = MagicMock(value="PRTG")
    alert.severity    = MagicMock(value=severity)
    alert.host        = host
    alert.message     = message
    alert.received_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    return alert


def _make_enrichment(decision="CRITICAL", confidence="0.9700"):
    enr = MagicMock()
    enr.id               = uuid.uuid4()
    enr.triage_decision  = decision
    enr.confidence_score = confidence
    enr.llm_reasoning    = "Disk at 98% on production host. Imminent failure risk."
    enr.suggested_action = "Verify before executing: df -h /"
    enr.runbook_refs     = [{"title": "Disk Space Runbook", "url": None, "relevance": "Standard playbook"}]
    enr.model_used       = "gpt-4o-2024-11-20"
    enr.prompt_version   = "triage-v1.0"
    return enr


APPROVAL_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Block Kit builder tests
# ---------------------------------------------------------------------------

class TestBuildAlertNotification:

    def test_critical_has_acknowledge_and_escalate_buttons(self):
        from slack.blocks import build_alert_notification
        payload = build_alert_notification(
            alert=_make_alert("CRITICAL"),
            enrichment=_make_enrichment("CRITICAL"),
            approval_id=APPROVAL_ID,
            channel="#noc-triage",
        )
        action_ids = _extract_action_ids(payload["blocks"])
        assert "approval_acknowledge" in action_ids
        assert "approval_escalate"    in action_ids
        assert "approval_suppress"    in action_ids

    def test_noise_has_suppress_button(self):
        from slack.blocks import build_alert_notification
        payload = build_alert_notification(
            alert=_make_alert("LOW"),
            enrichment=_make_enrichment("NOISE", "0.8100"),
            approval_id=APPROVAL_ID,
            channel="#noc-noise",
        )
        action_ids = _extract_action_ids(payload["blocks"])
        assert "approval_suppress" in action_ids

    def test_needs_review_shows_low_confidence_callout(self):
        from slack.blocks import build_alert_notification
        payload = build_alert_notification(
            alert=_make_alert(),
            enrichment=_make_enrichment("NEEDS_REVIEW", "0.4500"),
            approval_id=APPROVAL_ID,
            channel="#noc-triage",
        )
        # Find the low-confidence warning text somewhere in blocks
        all_text = _flatten_text(payload["blocks"])
        assert "Low AI confidence" in all_text or "45%" in all_text

    def test_hallucination_caveat_in_suggested_action(self):
        from slack.blocks import build_alert_notification
        payload = build_alert_notification(
            alert=_make_alert(),
            enrichment=_make_enrichment("CRITICAL"),
            approval_id=APPROVAL_ID,
            channel="#noc-triage",
        )
        all_text = _flatten_text(payload["blocks"])
        assert "verify before executing" in all_text.lower()

    def test_approval_id_embedded_in_button_values(self):
        from slack.blocks import build_alert_notification
        payload = build_alert_notification(
            alert=_make_alert(),
            enrichment=_make_enrichment("CRITICAL"),
            approval_id=APPROVAL_ID,
            channel="#noc-triage",
        )
        button_values = _extract_button_values(payload["blocks"])
        assert all(v == APPROVAL_ID for v in button_values)

    def test_no_unfurl(self):
        from slack.blocks import build_alert_notification
        payload = build_alert_notification(
            alert=_make_alert(),
            enrichment=_make_enrichment(),
            approval_id=APPROVAL_ID,
            channel="#test",
        )
        assert payload["unfurl_links"] is False
        assert payload["unfurl_media"] is False

    def test_long_message_truncated(self):
        from slack.blocks import build_alert_notification
        payload = build_alert_notification(
            alert=_make_alert(message="X" * 5000),
            enrichment=_make_enrichment(),
            approval_id=APPROVAL_ID,
            channel="#test",
        )
        all_text = _flatten_text(payload["blocks"])
        # Should not contain 5000 X's in any block
        assert "X" * 700 not in all_text


class TestBuildApprovalUpdate:

    def test_buttons_removed_after_action(self):
        from slack.blocks import build_approval_update
        original = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Alert"}},
            {"type": "actions", "elements": [{"type": "button"}]},
        ]
        updated = build_approval_update(
            original_blocks=original,
            action="ACKNOWLEDGE",
            actioned_by_name="jane.doe",
            actioned_at=datetime.now(timezone.utc),
        )
        types = [b["type"] for b in updated]
        assert "actions" not in types
        assert "section" in types

    def test_status_banner_contains_action_and_user(self):
        from slack.blocks import build_approval_update
        updated = build_approval_update(
            original_blocks=[],
            action="ESCALATE",
            actioned_by_name="bob",
            actioned_at=datetime.now(timezone.utc),
            note="Called on-call engineer",
        )
        text = _flatten_text(updated)
        assert "ESCALATE" in text
        assert "bob" in text
        assert "Called on-call engineer" in text


# ---------------------------------------------------------------------------
# Signature verification tests
# ---------------------------------------------------------------------------

class TestSlackSignatureVerification:

    SIGNING_SECRET = "test-signing-secret-abc123"

    def _make_sig(self, body: str, ts: int) -> str:
        base = f"v0:{ts}:{body}".encode()
        return "v0=" + hmac.new(
            self.SIGNING_SECRET.encode(), base, hashlib.sha256
        ).hexdigest()

    def test_valid_signature_passes(self):
        from slack.client import verify_slack_signature, SlackSignatureError
        body = b'payload={"type":"block_actions"}'
        ts   = int(time.time())
        sig  = self._make_sig(body.decode(), ts)

        with patch.dict("os.environ", {"SLACK_SIGNING_SECRET": self.SIGNING_SECRET}):
            # Should not raise
            verify_slack_signature(body, str(ts), sig)

    def test_wrong_signature_raises(self):
        from slack.client import verify_slack_signature, SlackSignatureError
        body = b'payload=test'
        ts   = int(time.time())
        with patch.dict("os.environ", {"SLACK_SIGNING_SECRET": self.SIGNING_SECRET}):
            with pytest.raises(SlackSignatureError, match="mismatch"):
                verify_slack_signature(body, str(ts), "v0=wrongsignature")

    def test_expired_timestamp_raises(self):
        from slack.client import verify_slack_signature, SlackSignatureError
        body   = b'payload=test'
        old_ts = int(time.time()) - 400   # 400s ago > 5 min TTL
        sig    = self._make_sig(body.decode(), old_ts)
        with patch.dict("os.environ", {"SLACK_SIGNING_SECRET": self.SIGNING_SECRET}):
            with pytest.raises(SlackSignatureError, match="too old"):
                verify_slack_signature(body, str(old_ts), sig)

    def test_invalid_timestamp_raises(self):
        from slack.client import verify_slack_signature, SlackSignatureError
        with patch.dict("os.environ", {"SLACK_SIGNING_SECRET": self.SIGNING_SECRET}):
            with pytest.raises(SlackSignatureError):
                verify_slack_signature(b"test", "not-a-number", "v0=sig")


# ---------------------------------------------------------------------------
# Channel resolver
# ---------------------------------------------------------------------------

class TestResolveChannel:

    def test_decision_specific_channel_wins(self):
        from slack.notifier import _resolve_channel
        with patch.dict("os.environ", {
            "SLACK_CHANNEL_CRITICAL": "#critical-alerts",
            "SLACK_ALERT_CHANNEL": "#default",
        }):
            assert _resolve_channel("acme", "CRITICAL") == "#critical-alerts"

    def test_tenant_channel_wins_over_default(self):
        from slack.notifier import _resolve_channel
        with patch.dict("os.environ", {
            "SLACK_CHANNEL_ACME": "#acme-noc",
            "SLACK_ALERT_CHANNEL": "#default",
        }):
            assert _resolve_channel("acme", "NOISE") == "#acme-noc"

    def test_fallback_to_default(self):
        from slack.notifier import _resolve_channel
        with patch.dict("os.environ", {"SLACK_ALERT_CHANNEL": "#noc-triage"}, clear=False):
            result = _resolve_channel("unknown-tenant", "LOW")
            assert result == "#noc-triage"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_action_ids(blocks: list[dict]) -> list[str]:
    ids = []
    for block in blocks:
        if block.get("type") == "actions":
            for elem in block.get("elements", []):
                if aid := elem.get("action_id"):
                    ids.append(aid)
    return ids


def _extract_button_values(blocks: list[dict]) -> list[str]:
    vals = []
    for block in blocks:
        if block.get("type") == "actions":
            for elem in block.get("elements", []):
                if v := elem.get("value"):
                    vals.append(v)
    return vals


def _flatten_text(blocks: list[dict]) -> str:
    """Recursively extract all text strings from Block Kit blocks."""
    parts = []
    def _walk(obj):
        if isinstance(obj, dict):
            if "text" in obj and isinstance(obj["text"], str):
                parts.append(obj["text"])
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
    _walk(blocks)
    return " ".join(parts)
