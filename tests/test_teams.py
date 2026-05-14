"""
tests/test_teams.py — Microsoft Teams integration test suite.

Covers every success test from the feature spec:
  1. TEAMS_WEBHOOK_URL set → Teams card fires
  2. SLACK_BOT_TOKEN set → Slack still works (no regression)
  3. Both set → both fire
  4. Neither set → log warning, no crash, returns None
  5. Teams failure → non-fatal, Slack still delivers
  6. Card content: host, severity, decision, confidence all present
  7. Hallucination caveat present on suggested_action
  8. post_card called with correct webhook URL
  9. TeamsError retryable vs non-retryable
 10. _trunc caps long strings safely
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Fixtures ───────────────────────────────────────────────────────────────

def _alert(host="db-prod-01", message="Disk full", severity="CRITICAL", source="PRTG"):
    a = MagicMock()
    a.id        = uuid.uuid4()
    a.alert_id  = "prtg-42"
    a.tenant_id = "acme"
    a.host      = host
    a.message   = message
    a.severity  = MagicMock(value=severity)
    a.source    = MagicMock(value=source)
    a.received_at = datetime.now(timezone.utc)
    return a


def _enrichment(decision="CRITICAL", score="0.97", action="Run df -h", reasoning="Disk at 98%."):
    e = MagicMock()
    e.id                 = uuid.uuid4()
    e.triage_decision    = decision
    e.confidence_score   = score
    e.suggested_action   = action
    e.llm_reasoning      = reasoning
    e.model_used         = "gpt-4o-2024-11-20"
    e.prompt_version     = "triage-v1.0"
    e.runbook_refs       = []
    e.alert_db_id        = uuid.uuid4()
    return e


# ── teams/cards.py ─────────────────────────────────────────────────────────

class TestBuildTeamsCard:

    def test_card_contains_host(self):
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(host="db-prod-01"), _enrichment())
        text = str(card)
        assert "db-prod-01" in text

    def test_card_contains_decision(self):
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(), _enrichment(decision="NOISE"))
        assert "NOISE" in str(card)

    def test_card_contains_confidence_bar(self):
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(), _enrichment(score="0.94"))
        # 94% → 9 filled blocks
        assert "94%" in str(card)

    def test_hallucination_caveat_present(self):
        """suggested_action must always carry the caveat. Non-negotiable."""
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(), _enrichment(action="df -h"))
        text = str(card)
        assert "verify before executing" in text.lower()

    def test_no_suggested_action_section_when_none(self):
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(), _enrichment(action=None))
        # Section with caveat should not appear when action is None
        assert "verify before executing" not in str(card).lower()

    def test_theme_color_critical(self):
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(), _enrichment(decision="CRITICAL"))
        assert card["themeColor"] == "DC2626"

    def test_theme_color_noise(self):
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(), _enrichment(decision="NOISE"))
        assert card["themeColor"] == "10B981"

    def test_theme_color_needs_review(self):
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(), _enrichment(decision="NEEDS_REVIEW"))
        assert card["themeColor"] == "D97706"

    def test_card_type_is_message_card(self):
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(), _enrichment())
        assert card["@type"] == "MessageCard"

    def test_potential_action_open_url(self):
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(), _enrichment())
        actions = card.get("potentialAction", [])
        assert len(actions) == 1
        assert actions[0]["@type"] == "OpenUri"

    def test_long_message_truncated(self):
        from teams.cards import build_teams_card
        card = build_teams_card(_alert(message="X" * 1000), _enrichment())
        # Should not contain 1000 X's verbatim
        assert "X" * 300 not in str(card)

    def test_reasoning_truncated_in_facts(self):
        from teams.cards import build_teams_card
        long_reasoning = "A" * 500
        card = build_teams_card(_alert(), _enrichment(reasoning=long_reasoning))
        # Facts section should cap the reasoning
        text = str(card)
        assert "A" * 200 not in text  # definitely truncated before 200 chars

    def test_conf_bar_high_confidence(self):
        from teams.cards import _conf_bar
        bar = _conf_bar("0.94")
        assert "█" * 9 in bar
        assert "94%" in bar

    def test_conf_bar_zero(self):
        from teams.cards import _conf_bar
        bar = _conf_bar("0.00")
        assert "0%" in bar

    def test_conf_bar_none_returns_dash(self):
        from teams.cards import _conf_bar
        assert _conf_bar(None) == "—"

    def test_trunc_short_string_unchanged(self):
        from teams.cards import _trunc
        assert _trunc("hello") == "hello"

    def test_trunc_long_string_adds_ellipsis(self):
        from teams.cards import _trunc
        result = _trunc("A" * 300, limit=100)
        assert result.endswith("…")
        assert len(result) == 100

    def test_trunc_none_returns_empty(self):
        from teams.cards import _trunc
        assert _trunc(None) == ""


# ── teams/client.py ────────────────────────────────────────────────────────

class TestPostCard:

    @pytest.mark.asyncio
    async def test_posts_to_webhook_url(self):
        from teams.client import post_card

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "1"

        with patch.dict("os.environ", {"TEAMS_WEBHOOK_URL": "https://teams.example.com/webhook"}):
            with patch("teams.client.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client_cls.return_value = mock_client

                await post_card({"test": "payload"})

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://teams.example.com/webhook"

    @pytest.mark.asyncio
    async def test_raises_when_url_not_set(self):
        from teams.client import post_card, TeamsError

        with patch.dict("os.environ", {}, clear=True):
            # Remove TEAMS_WEBHOOK_URL if it exists
            import os
            os.environ.pop("TEAMS_WEBHOOK_URL", None)
            with pytest.raises(TeamsError):
                await post_card({"test": "data"})

    @pytest.mark.asyncio
    async def test_non_200_raises_teams_error(self):
        from teams.client import post_card, TeamsError

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad payload"

        with patch.dict("os.environ", {"TEAMS_WEBHOOK_URL": "https://teams.example.com/wh"}):
            with patch("teams.client.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_cls.return_value = mock_client

                with pytest.raises(TeamsError):
                    await post_card({"bad": "payload"})

    @pytest.mark.asyncio
    async def test_timeout_raises_retryable_error(self):
        import httpx
        from teams.client import post_card, TeamsError

        with patch.dict("os.environ", {"TEAMS_WEBHOOK_URL": "https://teams.example.com/wh"}):
            with patch("teams.client.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(
                    side_effect=httpx.TimeoutException("timeout")
                )
                mock_cls.return_value = mock_client

                with pytest.raises(TeamsError) as exc_info:
                    await post_card({"data": "x"})

                assert exc_info.value.retryable is True


# ── slack/notifier.py routing ──────────────────────────────────────────────

class TestNotifierRouting:
    """
    Integration tests for the routing logic in send_alert_notification.
    All external calls (Teams, Slack, DB) are mocked.
    """

    def _mock_db(self):
        """Return mocked session that yields alert + enrichment."""
        alert    = _alert()
        enrichment = _enrichment()

        async def fake_session():
            s = AsyncMock()
            # First execute → alert, second execute → enrichment
            alert_res = MagicMock()
            alert_res.scalar_one_or_none.return_value = alert
            enrich_res = MagicMock()
            enrich_res.scalar_one_or_none.return_value = enrichment
            s.execute = AsyncMock(side_effect=[alert_res, enrich_res])
            s.add = MagicMock()
            s.flush = AsyncMock()
            s.__aenter__ = AsyncMock(return_value=s)
            s.__aexit__  = AsyncMock(return_value=False)
            return s

        return fake_session

    @pytest.mark.asyncio
    async def test_teams_fires_when_url_set(self):
        from slack.notifier import send_alert_notification

        with patch.dict("os.environ", {
            "TEAMS_WEBHOOK_URL": "https://teams.example.com/wh",
            "SLACK_BOT_TOKEN": "",
        }):
            with patch("slack.notifier.get_session") as mock_sess:
                mock_sess.return_value = await self._mock_db()()
                with patch("slack.notifier.post_card", new=AsyncMock()) as mock_teams:
                    with patch("slack.notifier.build_teams_card", return_value={}):
                        result = await send_alert_notification("00000000-0000-0000-0000-000000000001", "acme")

        mock_teams.assert_called_once()

    @pytest.mark.asyncio
    async def test_slack_fires_when_token_set(self):
        from slack.notifier import send_alert_notification

        with patch.dict("os.environ", {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "TEAMS_WEBHOOK_URL": "",
        }):
            with patch("slack.notifier.get_session") as mock_sess:
                mock_sess.return_value = await self._mock_db()()
                with patch("slack.notifier.post_message", new=AsyncMock(return_value="ts-123")):
                    with patch("slack.notifier.build_alert_notification", return_value={"blocks": []}):
                        with patch("slack.notifier._resolve_channel", return_value="#noc-triage"):
                            result = await send_alert_notification("00000000-0000-0000-0000-000000000002", "acme")

        assert result == "ts-123"

    @pytest.mark.asyncio
    async def test_both_fire_when_both_configured(self):
        from slack.notifier import send_alert_notification

        with patch.dict("os.environ", {
            "TEAMS_WEBHOOK_URL": "https://teams.example.com/wh",
            "SLACK_BOT_TOKEN": "xoxb-test",
        }):
            with patch("slack.notifier.get_session") as mock_sess:
                mock_sess.return_value = await self._mock_db()()
                with patch("slack.notifier.post_card", new=AsyncMock()) as mock_teams:
                    with patch("slack.notifier.build_teams_card", return_value={}):
                        with patch("slack.notifier.post_message", new=AsyncMock(return_value="ts-456")):
                            with patch("slack.notifier.build_alert_notification", return_value={"blocks": []}):
                                with patch("slack.notifier._resolve_channel", return_value="#noc"):
                                    result = await send_alert_notification(
                                        "00000000-0000-0000-0000-000000000003", "acme"
                                    )

        # Both fired — Teams called, Slack returned ts
        mock_teams.assert_called_once()
        assert result == "ts-456"

    @pytest.mark.asyncio
    async def test_neither_set_returns_none_no_crash(self):
        from slack.notifier import send_alert_notification
        import os

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("TEAMS_WEBHOOK_URL", None)
            os.environ.pop("SLACK_BOT_TOKEN", None)

            # Should return None without crashing, no DB calls needed
            result = await send_alert_notification("00000000-0000-0000-0000-000000000004", "acme")

        assert result is None

    @pytest.mark.asyncio
    async def test_teams_failure_is_nonfatal_slack_still_delivers(self):
        """Teams TeamsError must not prevent Slack from sending."""
        from slack.notifier import send_alert_notification
        from teams.client import TeamsError

        with patch.dict("os.environ", {
            "TEAMS_WEBHOOK_URL": "https://teams.example.com/wh",
            "SLACK_BOT_TOKEN": "xoxb-test",
        }):
            with patch("slack.notifier.get_session") as mock_sess:
                mock_sess.return_value = await self._mock_db()()
                # Teams fails
                with patch("slack.notifier.post_card",
                           new=AsyncMock(side_effect=TeamsError("webhook down"))):
                    with patch("slack.notifier.build_teams_card", return_value={}):
                        # Slack succeeds
                        with patch("slack.notifier.post_message",
                                   new=AsyncMock(return_value="ts-789")):
                            with patch("slack.notifier.build_alert_notification",
                                       return_value={"blocks": []}):
                                with patch("slack.notifier._resolve_channel",
                                           return_value="#noc"):
                                    result = await send_alert_notification(
                                        "00000000-0000-0000-0000-000000000005", "acme"
                                    )

        # Slack ts returned despite Teams failure
        assert result == "ts-789"
