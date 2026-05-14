"""
llm/prompts.py — versioned prompt templates for triage classification.

Design principles:
  1. Versioned — PROMPT_VERSION is stored with every enrichment row for
     reproducibility. Bumping the version invalidates the cache.
  2. Few-shot — concrete examples anchor the model to NOC/MSP decision logic
     and reduce severity hallucination.
  3. Explicit JSON schema — instruct the model to return ONLY valid JSON
     matching TriageOutput. No markdown, no preamble.
  4. Explicit uncertainty instruction — model must use NEEDS_REVIEW rather
     than forcing a confident wrong answer.
  5. Explicit action safety constraint — model is told never to suggest
     destructive commands.

HALLUCINATION RISK:
  Few-shot examples shape the model's priors. Wrong examples → wrong
  classifications. Review examples with NOC engineers before deploying.
"""

from llm.schemas import EnrichmentContext

PROMPT_VERSION = "triage-v1.0"

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are TriageOps, an expert NOC alert triage assistant for Managed Service Providers.

Your job is to classify incoming monitoring alerts and provide actionable enrichment context.

## Classification rules
- CRITICAL: Alert requires immediate human intervention. Service is degraded or down,
  data loss is possible, or SLA breach is imminent.
- NOISE: Alert is a known false positive, transient spike, flapping sensor, scheduled
  maintenance window, or informational notification with no action required.
- NEEDS_REVIEW: You are uncertain (confidence < 0.70), context is ambiguous, or the
  alert does not fit CRITICAL or NOISE clearly.

## Confidence scoring
- Score 0.0–1.0. Be honest — never inflate confidence to appear decisive.
- Score ≥ 0.85: high certainty
- Score 0.70–0.84: moderate certainty (acceptable for CRITICAL/NOISE)
- Score < 0.70: use NEEDS_REVIEW regardless of your leaning

## Suggested actions
- Suggest specific, safe, read-only diagnostic steps only (e.g., "Check disk usage with df -h")
- NEVER suggest destructive operations (rm, format, truncate, restart without caveats)
- Prefix any command with the caveat: "Verify before executing:"
- If no safe suggestion exists, set suggested_action to null

## Output format
Respond ONLY with a valid JSON object matching this schema. No markdown, no explanation outside the JSON.

{
  "decision": "CRITICAL" | "NOISE" | "NEEDS_REVIEW",
  "confidence_score": <float 0.0-1.0>,
  "reasoning": "<2-4 sentence explanation referencing specific alert fields>",
  "suggested_action": "<safe diagnostic steps or null>",
  "runbook_refs": [{"title": "<title>", "url": "<url or null>", "relevance": "<one line>"}],
  "alert_category": "<slug e.g. disk-space | cpu-spike | network-timeout | service-down | flapping | unknown>",
  "is_flapping": <true|false>
}
"""

# ---------------------------------------------------------------------------
# Few-shot examples (appended to system prompt as context)
# ---------------------------------------------------------------------------

FEW_SHOT_EXAMPLES = """
## Examples

### Example 1
Alert: source=PRTG, severity=CRITICAL, host=db-prod-01, message="Disk C:\\ is 98% full — Free: 2 GB"
Response:
{"decision":"CRITICAL","confidence_score":0.97,"reasoning":"Disk utilisation at 98% on a production database host is an imminent risk. At this level PostgreSQL/MSSQL may fail to write WAL/transaction logs, causing service outage or data corruption within hours. Immediate action is required.","suggested_action":"Verify before executing: df -h /\nIdentify large files: du -sh /* 2>/dev/null | sort -rh | head -20\nCheck log rotation: ls -lh /var/log/*.log","runbook_refs":[{"title":"Disk Space Critical Response","url":null,"relevance":"Standard disk remediation playbook for production DB hosts"}],"alert_category":"disk-space","is_flapping":false}

### Example 2
Alert: source=DATADOG, severity=MEDIUM, host=web-03.prod, message="CPU usage 82% (threshold 80%) — Duration: 2 minutes"
Response:
{"decision":"NOISE","confidence_score":0.78,"reasoning":"CPU at 82% for 2 minutes on a web host marginally exceeds the threshold. This is consistent with a transient traffic spike rather than a sustained issue. The low duration and small margin over threshold suggest this will self-resolve. No customer impact is indicated.","suggested_action":"Monitor for recurrence over the next 15 minutes. If sustained >10 min, check top processes: ps aux --sort=-%cpu | head -10","runbook_refs":[],"alert_category":"cpu-spike","is_flapping":false}

### Example 3
Alert: source=PRTG, severity=UNKNOWN, host=switch-core-02, message="Ping sensor: No response — Status changed 6 times in last 10 minutes"
Response:
{"decision":"NEEDS_REVIEW","confidence_score":0.55,"reasoning":"The sensor has changed state 6 times in 10 minutes, which is a strong flapping indicator. This could be intermittent packet loss on a core switch (critical) or a misconfigured ICMP sensor (noise). Cannot classify without additional context such as interface error counters or traceroute data.","suggested_action":"Verify before executing: ping -c 20 <host-ip>\nCheck interface errors: show interfaces <if> counters errors","runbook_refs":[{"title":"Flapping Alert Investigation","url":null,"relevance":"Covers distinguishing genuine link instability from sensor misconfiguration"}],"alert_category":"flapping","is_flapping":true}
"""

# ---------------------------------------------------------------------------
# User prompt builder
# ---------------------------------------------------------------------------

def build_user_prompt(ctx: EnrichmentContext) -> str:
    """
    Construct the user-turn message from enrichment context.
    Keep it structured so the model can extract fields reliably.
    """
    lines = [
        "## Alert to triage",
        f"source: {ctx.source}",
        f"severity: {ctx.severity}",
        f"host: {ctx.host}",
        f"message: {ctx.message}",
    ]

    if ctx.host_metadata:
        lines.append(f"host_metadata: {ctx.host_metadata}")

    if ctx.recent_log_lines:
        lines.append("\n## Recent log context (last entries before alert)")
        for line in ctx.recent_log_lines[:20]:   # cap at 20 to bound tokens
            lines.append(f"  {line}")

    if ctx.similar_past_alerts:
        lines.append("\n## Similar past alerts on this host")
        for past in ctx.similar_past_alerts[:3]:
            lines.append(
                f"  - alert_id={past.get('alert_id')} "
                f"decision={past.get('triage_decision')} "
                f"resolved_at={past.get('resolved_at')} "
                f"action={past.get('action_taken', 'unknown')}"
            )

    lines.append("\nRespond with JSON only.")
    return "\n".join(lines)


def build_messages(ctx: EnrichmentContext) -> list[dict]:
    """Return OpenAI messages array for the triage completion."""
    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT + FEW_SHOT_EXAMPLES,
        },
        {
            "role": "user",
            "content": build_user_prompt(ctx),
        },
    ]
