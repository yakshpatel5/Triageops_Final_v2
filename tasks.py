"""
tasks.py — Celery application, task definitions, and Beat schedule.

All async pipeline work (LLM enrichment, Slack notifications, escalation)
runs here via asyncio.run() — safe for Celery's fork process model because
each task creates its own event loop.

Queue topology:
  enrichment  — LLM-bound tasks, rate-limited to 10/min (OpenAI quota)
  default     — cron tasks, escalation dispatch

Beat schedule (singleton — NEVER scale beat service above 1 replica):
  expire_stale_approvals         every 15 min
  escalate_unactioned_criticals  every  5 min
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager

import sentry_sdk
from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init
from celery.utils.log import get_task_logger
from loguru import logger

from llm.client import LLMError
from llm.pipeline import AlertNotFound, run_enrichment_pipeline
from metrics.instrumentation import get_queue_depth
from slack.notifier import send_alert_notification

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "triageops",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    result_expires=3600,  # Expire results after 1 hour to save Redis memory
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "tasks.enrich_alert":                  {"queue": "enrichment"},
        "tasks.expire_stale_approvals":         {"queue": "default"},
        "tasks.escalate_unactioned_criticals":  {"queue": "default"},
        "tasks.dispatch_escalation_for_approval": {"queue": "default"},
    },
    task_annotations={
        "tasks.enrich_alert": {"rate_limit": "10/m"},
    },
    beat_schedule={
        "expire-stale-approvals": {
            "task": "tasks.expire_stale_approvals",
            "schedule": crontab(minute="*/15"),
        },
        "escalate-unactioned-criticals": {
            "task": "tasks.escalate_unactioned_criticals",
            "schedule": crontab(minute="*/5"),
        },
    },
)

_task_logger = get_task_logger(__name__)

# ---------------------------------------------------------------------------
# Asyncio Worker Setup
# ---------------------------------------------------------------------------

_worker_loop: asyncio.AbstractEventLoop | None = None

@worker_process_init.connect
def init_worker_process(**kwargs):
    """
    Initialize a single event loop per worker process.
    This allows sharing connection pools across tasks in the same process.
    """
    global _worker_loop
    _worker_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_worker_loop)
    logger.info("Celery worker process initialized with shared event loop")


def run_async(coro):
    """Helper to run coroutines in the worker's shared event loop."""
    if _worker_loop is None:
        # Fallback for local testing or if signal didn't fire
        return asyncio.run(coro)
    return _worker_loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# Task: LLM enrichment + Slack notification
# ---------------------------------------------------------------------------

@celery_app.task(
    name="tasks.enrich_alert",
    bind=True,
    max_retries=3,
    acks_late=True,
)
def enrich_alert(self, db_alert_id: str, tenant_id: str) -> dict:
    """
    Full pipeline: LLM enrichment → Slack notification.
    Celery retries on transient LLM errors with exponential backoff.
    """
    # Update queue depth metric
    try:
        with celery_app.connection_or_acquire() as conn:
            # This is a rough estimate of the queue depth for Redis
            count = conn.default_channel.client.llen("enrichment")
            get_queue_depth().set(count)
    except Exception:
        pass

    logger.info(
        "Task enrich_alert start | db_id={} tenant={} attempt={}/{}",
        db_alert_id, tenant_id,
        self.request.retries + 1, self.max_retries + 1,
    )

    # Stage 1: LLM enrichment
    try:
        result = run_async(
            run_enrichment_pipeline(db_alert_id=db_alert_id, tenant_id=tenant_id)
        )
    except AlertNotFound as exc:
        logger.error("Alert not found | db_id={} error={}", db_alert_id, exc)
        sentry_sdk.capture_exception(exc)
        return {"status": "skipped", "reason": "alert_not_found", "db_alert_id": db_alert_id}
    except LLMError as exc:
        if exc.retryable:
            delay = 30 * (3 ** self.request.retries)   # 30s → 90s → 270s
            logger.warning(
                "LLM retryable error — retry in {}s | db_id={} attempt={} error={}",
                delay, db_alert_id, self.request.retries + 1, exc,
            )
            raise self.retry(exc=exc, countdown=delay)
        logger.error("LLM non-retryable | db_id={} error={}", db_alert_id, exc)
        sentry_sdk.capture_exception(exc)
        # Pipeline persisted NEEDS_REVIEW — fall through to Slack
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        logger.exception("Unexpected enrichment error | db_id={} error={}", db_alert_id, exc)
        if self.request.retries < 2:
            raise self.retry(exc=exc, countdown=60)
        return {"status": "failed", "reason": str(exc), "db_alert_id": db_alert_id}

    decision   = result.triage_output.decision
    confidence = result.triage_output.confidence_score

    # Stage 2: Slack notification (non-fatal)
    try:
        slack_ts = run_async(
            send_alert_notification(db_alert_id=db_alert_id, tenant_id=tenant_id)
        )
        if not slack_ts:
            logger.warning("Slack delivery failed (non-fatal) | db_id={}", db_alert_id)
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        logger.error("Unexpected Slack error (non-fatal) | db_id={} error={}", db_alert_id, exc)

    logger.info(
        "Task enrich_alert complete | db_id={} tenant={} decision={} confidence={}",
        db_alert_id, tenant_id, decision, confidence,
    )
    return {
        "status": "complete",
        "db_alert_id": db_alert_id,
        "decision": decision,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Distributed Lock for Beat Singleton Tasks
# ---------------------------------------------------------------------------

@contextmanager
def distributed_lock(lock_name: str, expire_secs: int = 60):
    """
    Redis-backed distributed lock to ensure only one Beat task runs at a time.
    Prevents double-execution during rolling deployments.
    """
    import redis
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    r = redis.from_url(url, decode_responses=True)
    
    lock_key = f"lock:beat:{lock_name}"
    
    # SET NX EX: Set if Not eXists with EXpiry
    acquired = r.set(lock_key, "locked", ex=expire_secs, nx=True)
    
    if not acquired:
        logger.warning("Could not acquire distributed lock | key={}", lock_key)
        yield False
        return

    try:
        yield True
    finally:
        # Only delete if we still own it (basic safety)
        r.delete(lock_key)


# ---------------------------------------------------------------------------
# Cron task: expire stale approvals
# ---------------------------------------------------------------------------

@celery_app.task(
    name="tasks.expire_stale_approvals",
    acks_late=True,
    max_retries=2,
)
def expire_stale_approvals() -> dict:
    """Mark PENDING approvals past TTL as EXPIRED. Runs every 15 minutes."""
    with distributed_lock("expire_stale_approvals", expire_secs=300) as acquired:
        if not acquired:
            return {"status": "skipped", "reason": "lock_not_acquired"}

        from cron import _run_expire_stale_approvals
        logger.info("Cron: expire_stale_approvals start")
        try:
            result = run_async(_run_expire_stale_approvals())
            logger.info("Cron: expire_stale_approvals complete | {}", result)
            return result
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            logger.exception("Cron: expire_stale_approvals failed | error={}", exc)
            return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Cron task: auto-escalate expired CRITICAL approvals
# ---------------------------------------------------------------------------

@celery_app.task(
    name="tasks.escalate_unactioned_criticals",
    acks_late=True,
    max_retries=2,
)
def escalate_unactioned_criticals() -> dict:
    """Auto-escalate CRITICAL alerts that expired without human action. Runs every 5 minutes."""
    with distributed_lock("escalate_unactioned_criticals", expire_secs=120) as acquired:
        if not acquired:
            return {"status": "skipped", "reason": "lock_not_acquired"}

        from cron import _run_escalate_unactioned_criticals
        logger.info("Cron: escalate_unactioned_criticals start")
        try:
            result = run_async(_run_escalate_unactioned_criticals())
            logger.info("Cron: escalate_unactioned_criticals complete | {}", result)
            return result
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            logger.exception("Cron: escalate_unactioned_criticals failed | error={}", exc)
            return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# On-demand task: escalation dispatch (triggered by Slack ESCALATE button)
# ---------------------------------------------------------------------------

@celery_app.task(
    name="tasks.dispatch_escalation_for_approval",
    bind=True,          # FIX: bind=True required so self.retry() is available
    acks_late=True,
    max_retries=3,
)
def dispatch_escalation_for_approval(self, approval_id: str) -> dict:
    """
    Triggered immediately when a human clicks ESCALATE in Slack.
    Runs async in the background — Slack callback returns within 3s.
    """
    from escalation.dispatcher import dispatch_escalation
    from db.session import AsyncSessionLocal
    from models import SlackApproval, Alert, AlertEnrichment
    from sqlalchemy import select
    import uuid as _uuid

    logger.info("Task dispatch_escalation_for_approval | approval_id={}", approval_id)

    async def _run():
        approval_uuid = _uuid.UUID(approval_id)
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                select(SlackApproval).where(SlackApproval.id == approval_uuid)
            )
            approval = r.scalar_one_or_none()
            if not approval:
                logger.error("Approval not found | id={}", approval_id)
                return {"status": "skipped", "reason": "approval_not_found"}

            r = await session.execute(
                select(Alert).where(Alert.id == approval.alert_db_id)
            )
            alert = r.scalar_one_or_none()
            if not alert:
                logger.error("Alert not found for approval | id={}", approval_id)
                return {"status": "skipped", "reason": "alert_not_found"}

            r = await session.execute(
                select(AlertEnrichment).where(
                    AlertEnrichment.alert_db_id == approval.alert_db_id
                )
            )
            enrichment = r.scalar_one_or_none()
            if not enrichment:
                logger.error("Enrichment not found for approval | id={}", approval_id)
                return {"status": "skipped", "reason": "enrichment_not_found"}

        event = await dispatch_escalation(
            approval=approval,
            alert=alert,
            enrichment=enrichment,
        )
        return {"status": event.status, "provider": event.provider}

    try:
        return run_async(_run())
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        logger.exception(
            "dispatch_escalation_for_approval failed | id={} attempt={} error={}",
            approval_id, self.request.retries + 1, exc,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=15)
        return {"status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Enqueue helpers
# ---------------------------------------------------------------------------

def enqueue_enrichment(db_alert_id: str, tenant_id: str) -> None:
    """Queue LLM enrichment task after alert is persisted."""
    enrich_alert.apply_async(args=[db_alert_id, tenant_id], queue="enrichment")
    logger.debug("Enrichment task queued | db_id={} tenant={}", db_alert_id, tenant_id)


def enqueue_escalation(approval_id: str) -> None:
    """Queue escalation dispatch after engineer clicks ESCALATE in Slack."""
    dispatch_escalation_for_approval.apply_async(
        args=[approval_id], queue="default",
    )
    logger.debug("Escalation task queued | approval_id={}", approval_id)
