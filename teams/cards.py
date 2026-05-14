"""
teams/cards.py — Build Microsoft Teams Adaptive Card from alert + enrichment.

Adaptive Cards spec: https://adaptivecards.io/explorer/
Teams Adaptive Card support matrix: https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-reference

V1 limitations (incoming webhook tier):
  - Action.Submit NOT supported — no approval buttons in Teams v1.
  - Action.OpenUrl only — links to the TriageOps dashboard.
  - messageCard format used for max Teams version compatibility.
    (Adaptive Card via O365ConnectorCard — widest webhook support.)

HALLUCINATION RISK: Teams card schema versions change across clients.
  Tested against Teams desktop 2024. Validate against a real webhook before
  deploying to avoid silent render failures.
"""

from __future__ import annotations

import os
from typing import Any

from models import Alert, AlertEnrichment

# Colour map for the card's themeColor stripe (hex, no #)
_DECISION_COLOR = {
    "CRITICAL":     "DC2626",   # ruby red
    "NOISE":        "10B981",   # emerald
    "NEEDS_REVIEW": "D97706",   # amber
}

_SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH":     "🟠",
    "MEDIUM":   "🟡",
    "LOW":      "🟢",
    "UNKNOWN":  "⚪",
}

_DECISION_EMOJI = {
    "CRITICAL":     "🚨",
    "NOISE":        "🔇",
    "NEEDS_REVIEW": "🤔",
}


def _trunc(s: str | None, limit: int = 200) -> str:
    if not s:
        return ""
    s = str(s).strip()
    return s[:limit - 3] + "…" if len(s) > limit else s


def _conf_bar(score: str | None) -> str:
    """ASCII confidence bar for Teams (no custom components)."""
    if not score:
        return "—"
    pct = round(float(score) * 100)
    filled = round(pct / 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"{bar}  {pct}%"


def build_teams_card(
    alert: Alert,
    enrichment: AlertEnrichment,
) -> dict[str, Any]:
    """
    Build an O365ConnectorCard payload (MessageCard format) for Teams.

    Returns dict ready to POST to TEAMS_WEBHOOK_URL.

    Structure:
      - Header section: host, decision badge, severity
      - Alert section: message, source, received_at
      - AI Triage section: decision, confidence bar, reasoning
      - Suggested Action section (if present) — with hallucination caveat
      - Footer: model, prompt version, "read-only" reminder
      - Potential Action: "View in Dashboard" OpenUrl button
    """
    decision = enrichment.triage_decision
    color = _DECISION_COLOR.get(decision, "64748B")
    d_emoji = _DECISION_EMOJI.get(decision, "❓")
    s_emoji = _SEVERITY_EMOJI.get(
        alert.severity.value if hasattr(alert.severity, "value") else str(alert.severity),
        "⚪",
    )
    source = alert.source.value if hasattr(alert.source, "value") else str(alert.source)
    severity = alert.severity.value if hasattr(alert.severity, "value") else str(alert.severity)

    dashboard_url = os.getenv("TRIAGEOPS_DASHBOARD_URL", "http://localhost:8000/dashboard")

    sections: list[dict] = []

    # ── Section 1: Alert identity ────────────────────────────────────────
    sections.append({
        "activityTitle": f"{d_emoji} **{decision}** — `{_trunc(alert.host, 80)}`",
        "activitySubtitle": (
            f"{s_emoji} {severity} &nbsp;·&nbsp; "
            f"{source} &nbsp;·&nbsp; "
            f"Alert ID: `{_trunc(alert.alert_id, 60)}`"
        ),
        "activityText": f"_{_trunc(alert.message, 300)}_",
        "markdown": True,
    })

    # ── Section 2: AI triage ─────────────────────────────────────────────
    triage_facts = [
        {"name": "Decision",    "value": decision},
        {"name": "Confidence",  "value": _conf_bar(enrichment.confidence_score)},
        {"name": "Category",    "value": enrichment.llm_reasoning[:120] + "…"
                                          if enrichment.llm_reasoning and len(enrichment.llm_reasoning) > 120
                                          else (enrichment.llm_reasoning or "—")},
    ]
    sections.append({
        "title":   "🤖 AI Triage Analysis",
        "facts":   triage_facts,
        "markdown": True,
    })

    # ── Section 3: Suggested action (hallucination caveat required) ──────
    if enrichment.suggested_action:
        sections.append({
            "title": "⚠️ AI Suggestion — verify before executing",
            "text":  f"```\n{_trunc(enrichment.suggested_action, 400)}\n```",
            "markdown": True,
        })

    # ── Section 4: Footer ────────────────────────────────────────────────
    sections.append({
        "facts": [
            {"name": "Model",   "value": enrichment.model_used},
            {"name": "Prompt",  "value": enrichment.prompt_version},
            {"name": "Note",    "value": "✋ TriageOps v1 — read-only. No auto-close without human approval."},
        ],
        "markdown": True,
    })

    # ── Potential Actions ────────────────────────────────────────────────
    potential_actions = [
        {
            "@type": "OpenUri",
            "name":  "View in TriageOps Dashboard",
            "targets": [{"os": "default", "uri": dashboard_url}],
        }
    ]

    card: dict[str, Any] = {
        "@type":       "MessageCard",
        "@context":    "https://schema.org/extensions",
        "themeColor":  color,
        "summary":     f"TriageOps {decision} — {alert.host}",
        "title":       f"{d_emoji} TriageOps Alert: {decision} on {_trunc(alert.host, 60)}",
        "sections":    sections,
        "potentialAction": potential_actions,
    }

    return card
