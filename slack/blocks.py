"""
slack/blocks.py — Block Kit message builder for TriageOps alert notifications.

Design rules:
  - CRITICAL alerts: red header, full enrichment context, 4 action buttons
  - NOISE alerts:    grey header, compact layout, Dismiss / Escalate only
  - NEEDS_REVIEW:    yellow header, prominent "Low confidence" callout

Security rules:
  - All user-supplied strings are truncated + stripped before injection into blocks
  - suggested_action is clearly labelled "AI suggestion — verify before executing"
  - runbook_refs URLs from LLM are labelled "⚠️ AI-generated link — verify before clicking"
  - slack_ts is the correlation key for interactive callbacks — never expose db UUIDs in action_id values

Block Kit reference: https://api.slack.com/reference/block-kit/blocks
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from models import AlertEnrichment
from models import Alert


# ---------------------------------------------------------------------------
# Colour sidebar (Block Kit "color" field on attachments)
# ---------------------------------------------------------------------------
_DECISION_COLOUR = {
    "CRITICAL":     "#E01E5A",   # red
    "NOISE":        "#616061",   # dark grey
    "NEEDS_REVIEW": "#ECB22E",   # amber
}

_DECISION_EMOJI = {
    "CRITICAL":     "🚨",
    "NOISE":        "🔇",
    "NEEDS_REVIEW": "🤔",
}

_SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH":     "🟠",
    "MEDIUM":   "🟡",
    "LOW":      "🟢",
    "UNKNOWN":  "⚪",
}

# Truncation limits — Slack Block Kit hard limits, leave headroom
_MAX_TEXT      = 2900   # section text
_MAX_FIELD     = 1900   # field text
_MAX_BUTTON    = 75     # button text


def _trunc(s: str | None, limit: int = _MAX_TEXT) -> str:
    """Truncate and strip a string; return empty string for None."""
    if not s:
        return ""
    s = str(s).strip()
    if len(s) > limit:
        return s[: limit - 3] + "..."
    return s


def _ts_human(dt: datetime | None) -> str:
    """ISO-ish timestamp for display."""
    if dt is None:
        return "unknown"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def build_alert_notification(
    alert: Alert,
    enrichment: AlertEnrichment,
    approval_id: str,           # SlackApproval.id — used as callback correlation key
    channel: str,
) -> dict[str, Any]:
    """
    Build the full Slack API payload (chat.postMessage body) for one alert.

    Returns a dict ready to POST to Slack's chat.postMessage endpoint.
    The 'blocks' key carries the Block Kit structure.
    The 'attachments' key carries the colour sidebar (deprecated API but
    still the only way to get a left-border colour in Slack).
    """
    decision: str = enrichment.triage_decision
    colour   = _DECISION_COLOUR.get(decision, "#616061")
    d_emoji  = _DECISION_EMOJI.get(decision, "❓")
    s_emoji  = _SEVERITY_EMOJI.get(alert.severity.value if hasattr(alert.severity, "value") else str(alert.severity), "⚪")

    blocks: list[dict] = []

    # ── Header ──────────────────────────────────────────────────────────────
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": _trunc(
                f"{d_emoji} {decision} — {alert.host}",
                150,
            ),
            "emoji": True,
        },
    })

    # ── Core alert fields (2-column) ─────────────────────────────────────
    blocks.append({
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*Source*\n{_trunc(alert.source.value if hasattr(alert.source, 'value') else str(alert.source), 100)}"},
            {"type": "mrkdwn", "text": f"*Severity*\n{s_emoji} {_trunc(alert.severity.value if hasattr(alert.severity, 'value') else str(alert.severity), 100)}"},
            {"type": "mrkdwn", "text": f"*Host*\n`{_trunc(alert.host, 200)}`"},
            {"type": "mrkdwn", "text": f"*Received*\n{_ts_human(alert.received_at)}"},
        ],
    })

    # ── Alert message ────────────────────────────────────────────────────
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Alert message*\n```{_trunc(alert.message, 600)}```",
        },
    })

    blocks.append({"type": "divider"})

    # ── LLM triage result ────────────────────────────────────────────────
    confidence_pct = int(float(enrichment.confidence_score) * 100)
    confidence_bar = _confidence_bar(confidence_pct)

    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*🤖 AI Triage — {decision}*\n"
                f"Confidence: {confidence_bar} {confidence_pct}%"
            ),
        },
    })

    if enrichment.llm_reasoning:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reasoning*\n{_trunc(enrichment.llm_reasoning, 800)}",
            },
        })

    # ── Suggested action (with hallucination caveat) ──────────────────
    if enrichment.suggested_action:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*⚠️ AI suggestion — verify before executing*\n"
                    f"```{_trunc(enrichment.suggested_action, 600)}```"
                ),
            },
        })

    # ── Runbook refs (LLM-generated — URLs flagged as unverified) ─────
    if enrichment.runbook_refs:
        refs: list[dict] = enrichment.runbook_refs if isinstance(enrichment.runbook_refs, list) else []
        if refs:
            ref_lines = []
            for ref in refs[:3]:
                title = _trunc(ref.get("title", "Untitled"), 100)
                url   = ref.get("url")
                relevance = _trunc(ref.get("relevance", ""), 120)
                if url:
                    ref_lines.append(f"• <{url}|{title}> _(⚠️ AI-generated link — verify)_\n  _{relevance}_")
                else:
                    ref_lines.append(f"• *{title}*\n  _{relevance}_")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Runbook references*\n" + "\n".join(ref_lines),
                },
            })

    blocks.append({"type": "divider"})

    # ── Low-confidence callout for NEEDS_REVIEW ───────────────────────
    if decision == "NEEDS_REVIEW":
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"⚠️ *Low AI confidence ({confidence_pct}%)* — "
                    "manual review required before taking any action."
                ),
            },
        })

    # ── Action buttons ────────────────────────────────────────────────
    # action_value encodes approval_id so the callback handler can look up
    # the right SlackApproval row without exposing internal UUIDs in the UI.
    buttons = _build_action_buttons(decision, approval_id)
    if buttons:
        blocks.append({
            "type": "actions",
            "block_id": f"approval_{approval_id}",
            "elements": buttons,
        })

    # ── Footer ────────────────────────────────────────────────────────
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"TriageOps · alert_id: `{_trunc(alert.alert_id, 80)}` · "
                    f"model: {_trunc(enrichment.model_used, 40)} · "
                    f"prompt: {_trunc(enrichment.prompt_version, 30)} · "
                    f"V1 read-only — no auto-close"
                ),
            }
        ],
    })

    return {
        "channel": channel,
        "text": f"{d_emoji} TriageOps: {decision} on {alert.host}",   # fallback for notifications
        "blocks": blocks,
        "attachments": [
            {
                "color": colour,
                "fallback": f"TriageOps alert: {decision} on {alert.host}",
            }
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def build_approval_update(
    original_blocks: list[dict],
    action: str,
    actioned_by_name: str,
    actioned_at: datetime,
    note: str | None = None,
) -> list[dict]:
    """
    Return updated blocks for chat.update after a human takes action.
    Replaces the action buttons with a status banner — prevents double-clicking.
    """
    action_emoji = {
        "ACKNOWLEDGE": "✅",
        "SUPPRESS":    "🔇",
        "ESCALATE":    "📣",
        "DISMISS":     "🚫",
    }.get(action, "✔️")

    # Strip existing action buttons block (last actions block)
    new_blocks = [b for b in original_blocks if b.get("type") != "actions"]

    new_blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"{action_emoji} *{action}* by {actioned_by_name} "
                f"at {_ts_human(actioned_at)}"
                + (f"\n_{_trunc(note, 300)}_" if note else "")
            ),
        },
    })
    return new_blocks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _confidence_bar(pct: int) -> str:
    """ASCII progress bar for confidence score."""
    filled = round(pct / 10)
    return "█" * filled + "░" * (10 - filled)


def _build_action_buttons(decision: str, approval_id: str) -> list[dict]:
    """Return Block Kit button elements appropriate for the triage decision."""
    # All buttons encode approval_id in value so callback can resolve the row
    ack_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "✅ Acknowledge", "emoji": True},
        "style": "primary",
        "action_id": "approval_acknowledge",
        "value": approval_id,
        "confirm": {
            "title": {"type": "plain_text", "text": "Acknowledge alert?"},
            "text": {"type": "mrkdwn", "text": "Confirm you are taking ownership of this alert."},
            "confirm": {"type": "plain_text", "text": "Yes, acknowledge"},
            "deny": {"type": "plain_text", "text": "Cancel"},
        },
    }
    suppress_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "🔇 Suppress", "emoji": True},
        "action_id": "approval_suppress",
        "value": approval_id,
        "confirm": {
            "title": {"type": "plain_text", "text": "Suppress this alert?"},
            "text": {"type": "mrkdwn", "text": "Mark as noise and suppress future similar alerts."},
            "confirm": {"type": "plain_text", "text": "Suppress"},
            "deny": {"type": "plain_text", "text": "Cancel"},
        },
    }
    escalate_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "📣 Escalate", "emoji": True},
        "style": "danger",
        "action_id": "approval_escalate",
        "value": approval_id,
        "confirm": {
            "title": {"type": "plain_text", "text": "Escalate to Tier 2?"},
            "text": {"type": "mrkdwn", "text": "This will page the on-call engineer."},
            "confirm": {"type": "plain_text", "text": "Escalate"},
            "deny": {"type": "plain_text", "text": "Cancel"},
        },
    }
    dismiss_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "🚫 Dismiss", "emoji": True},
        "action_id": "approval_dismiss",
        "value": approval_id,
    }

    if decision == "CRITICAL":
        return [ack_btn, escalate_btn, suppress_btn, dismiss_btn]
    elif decision == "NOISE":
        return [suppress_btn, ack_btn, dismiss_btn]
    else:  # NEEDS_REVIEW
        return [ack_btn, escalate_btn, suppress_btn, dismiss_btn]
