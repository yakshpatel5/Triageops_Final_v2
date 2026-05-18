"""
metrics/instrumentation.py — Prometheus metrics for TriageOps.
"""

import os
from prometheus_client import Counter, Histogram, Gauge, make_asgi_app, REGISTRY

# ---------------------------------------------------------------------------
# Metrics Definitions (Lazy Initialization)
# ---------------------------------------------------------------------------

def _get_metric(cls, name, documentation, labelnames=None, **kwargs):
    """Get or create a metric from the registry to avoid duplication errors."""
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    if labelnames:
        return cls(name, documentation, labelnames, **kwargs)
    return cls(name, documentation, **kwargs)

# Alerts ingested by tenant, source, and triage decision
def get_alerts_total():
    return _get_metric(Counter, "triageops_alerts_total", "Total number of alerts ingested", ["tenant_id", "source", "decision"])

# LLM enrichment duration in seconds
def get_enrichment_duration():
    return _get_metric(Histogram, "triageops_enrichment_seconds", "Time spent on LLM enrichment", ["model"], buckets=(1, 2, 5, 10, 20, 30, 60))

# LLM enrichment errors
def get_enrichment_errors_total():
    return _get_metric(Counter, "triageops_enrichment_errors_total", "Total number of LLM enrichment errors", ["error_type"])

# Celery queue depth (updated periodically by Beat or Worker)
def get_queue_depth():
    return _get_metric(Gauge, "triageops_queue_depth", "Current number of tasks in the enrichment queue")

# Circuit breaker state (0=CLOSED, 1=OPEN)
def get_circuit_breaker_state():
    return _get_metric(Gauge, "triageops_circuit_breaker_state", "Current state of the OpenAI circuit breaker (0=CLOSED, 1=OPEN)", ["service"])

# ---------------------------------------------------------------------------
# ASGI App for /metrics
# ---------------------------------------------------------------------------

def get_metrics_app():
    return make_asgi_app()
