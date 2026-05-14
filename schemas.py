"""
Pydantic v2 schemas.

PRTG / Datadog payload structures are large and poorly documented — normalisation
logic here is best-effort. Raw payloads are always stored so nothing is lost.

HALLUCINATION RISK: PRTG field names (sensorid, device, status) and Datadog
field names (monitor_id, host.name, alert_type) were sourced from vendor docs at
time of writing. Validate against live webhook samples before deploying.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums — imported by models.py too, so define here (single source of truth)
# ---------------------------------------------------------------------------

class AlertSource(str, Enum):
    PRTG = "PRTG"
    DATADOG = "DATADOG"


class AlertSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Normalised internal model — what we persist and hand to the LLM pipeline
# ---------------------------------------------------------------------------

class AlertIngestionEvent(BaseModel):
    """Canonical alert representation, source-agnostic."""

    alert_id: str = Field(..., min_length=1, max_length=512)
    tenant_id: str = Field(..., min_length=1, max_length=256)
    source: AlertSource
    severity: AlertSeverity
    host: str = Field(..., min_length=1, max_length=512)
    message: str = Field(..., min_length=1)
    raw_payload: dict[str, Any]
    received_at: datetime

    @field_validator("received_at", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        if isinstance(v, datetime):
            return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
        return v

    @field_validator("alert_id", "host", mode="before")
    @classmethod
    def strip_and_require(cls, v: Any) -> str:
        if isinstance(v, str):
            v = v.strip()
        if not v:
            raise ValueError("Field must be a non-empty string")
        return v


# ---------------------------------------------------------------------------
# PRTG raw webhook schema
# ---------------------------------------------------------------------------
# PRTG sends x-www-form-urlencoded OR JSON depending on notification template.
# We support JSON (configure notification template to POST JSON).
# HALLUCINATION RISK: verify field names against your PRTG version.

_PRTG_SEVERITY_MAP: dict[str, AlertSeverity] = {
    "down": AlertSeverity.CRITICAL,
    "down (acknowledged)": AlertSeverity.HIGH,
    "warning": AlertSeverity.MEDIUM,
    "unusual": AlertSeverity.LOW,
    "up": AlertSeverity.LOW,
    "paused": AlertSeverity.LOW,
    "unknown": AlertSeverity.UNKNOWN,
}


class PRTGPayload(BaseModel):
    """
    Loosely typed — PRTG fields vary by notification template.
    All fields optional at parse time; validation in normalise().
    """

    model_config = {"extra": "allow"}   # store unknown fields in raw_payload

    sensorid: str | int | None = None
    sensor: str | None = None           # sensor name
    device: str | None = None           # device/host name
    host: str | None = None             # may also appear as 'host'
    status: str | None = None           # "Down", "Warning", etc.
    message: str | None = None
    laststatus: str | None = None
    datetime_: str | None = Field(None, alias="datetime")

    def normalise(self, tenant_id: str, raw: dict[str, Any]) -> AlertIngestionEvent:
        alert_id = str(self.sensorid or raw.get("sensorid") or "")
        if not alert_id:
            raise ValueError("PRTG payload missing sensorid — cannot deduplicate")

        status_raw = (self.status or "unknown").lower().strip()
        severity = _PRTG_SEVERITY_MAP.get(status_raw, AlertSeverity.UNKNOWN)

        host = (
            self.device
            or self.host
            or raw.get("device")
            or raw.get("host")
            or "unknown-host"
        )
        msg = self.message or f"PRTG alert: sensor {self.sensor!r} is {self.status!r}"

        return AlertIngestionEvent(
            alert_id=f"prtg-{alert_id}",   # namespace to avoid collision with datadog IDs
            tenant_id=tenant_id,
            source=AlertSource.PRTG,
            severity=severity,
            host=str(host),
            message=str(msg),
            raw_payload=raw,
            received_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Datadog raw webhook schema
# ---------------------------------------------------------------------------
# HALLUCINATION RISK: Datadog monitor webhook body — verify against:
# https://docs.datadoghq.com/integrations/webhooks/#variables

_DD_SEVERITY_MAP: dict[str, AlertSeverity] = {
    "alert": AlertSeverity.CRITICAL,
    "error": AlertSeverity.HIGH,
    "warning": AlertSeverity.MEDIUM,
    "info": AlertSeverity.LOW,
    "success": AlertSeverity.LOW,
    "recovered": AlertSeverity.LOW,
    "no data": AlertSeverity.UNKNOWN,
}

# Datadog monitor_id is an integer; alert_id in webhook is "$ALERT_ID" (string)
_DD_TEMPLATE_VAR_RE = re.compile(r"^\$[A-Z_]+$")   # un-expanded template variable sentinel


class DatadogPayload(BaseModel):
    """
    Datadog webhook body — uses $VAR template variables; unresolved ones arrive as
    literal strings like "$HOSTNAME". Detect and treat as unknown.
    """

    model_config = {"extra": "allow"}

    id: str | int | None = None                 # monitor ID ($ALERT_ID)
    alert_id: str | int | None = None           # same field, different DD versions
    alert_type: str | None = None               # "alert" | "warning" | "recovered"
    alert_metric: str | None = None
    hostname: str | None = None
    host: str | None = None
    title: str | None = None
    text: str | None = None
    body: str | None = None
    date_happened: int | None = None            # unix timestamp
    tags: str | list[str] | None = None

    @field_validator("hostname", "host", "title", "text", mode="before")
    @classmethod
    def reject_unexpanded_template_vars(cls, v: Any) -> Any | None:
        """Datadog sends literal '$HOSTNAME' if template var is unavailable."""
        if isinstance(v, str) and _DD_TEMPLATE_VAR_RE.match(v):
            return None
        return v

    def normalise(self, tenant_id: str, raw: dict[str, Any]) -> AlertIngestionEvent:
        raw_id = self.id or self.alert_id or raw.get("id") or raw.get("alert_id")
        if not raw_id:
            raise ValueError("Datadog payload missing id/alert_id — cannot deduplicate")

        alert_type = (self.alert_type or "alert").lower().strip()
        severity = _DD_SEVERITY_MAP.get(alert_type, AlertSeverity.UNKNOWN)

        host = (
            self.hostname
            or self.host
            or raw.get("hostname")
            or raw.get("host")
            or "unknown-host"
        )
        msg = (
            self.text
            or self.body
            or self.title
            or f"Datadog monitor alert: {alert_type}"
        )

        return AlertIngestionEvent(
            alert_id=f"dd-{raw_id}",
            tenant_id=tenant_id,
            source=AlertSource.DATADOG,
            severity=severity,
            host=str(host),
            message=str(msg),
            raw_payload=raw,
            received_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# API responses
# ---------------------------------------------------------------------------

class IngestResponse(BaseModel):
    status: str                          # "accepted" | "duplicate" | "suppressed"
    alert_id: str | None = None
    detail: str | None = None
