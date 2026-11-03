"""
Pipeline run reconciliation: replays outbox rows that a crash, restart or failure left behind.

Each due event (see PipelineRunRepository.find_due) is replayed through the same tracked entry point as
the background task, which claims the row atomically first, so concurrent reconcilers and background
tasks never run one event twice at the same time. Replays are idempotent: identifiers are deterministic,
Stages 5-8 return existing records for identical inputs, and terminal observations/attributions are
not rewritten at an as-of boundary that was already recorded.

The database session factory is the live pipeline's (live_pipeline.AsyncSessionLocal), so tests and
scripts bind exactly one database.
"""
import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Optional

from src.core.domain.models import PaymentEvent
from src.core.services import live_pipeline
from src.infrastructure.pipeline_run_repository import PipelineRunRepository

logger = logging.getLogger("pipeline_reconciler")


def to_payment_event(model) -> PaymentEvent:
    """The canonical PaymentEvent stored in a payment_events row."""
    return PaymentEvent(**{name: getattr(model, name) for name in PaymentEvent.model_fields})


async def reconcile_pipeline_runs(now: Optional[datetime] = None, limit: int = 100) -> Dict[str, int]:
    """Replay due pipeline runs once; returns counts by outcome (success / failed / skipped)."""
    now = now or datetime.now(timezone.utc)
    async with live_pipeline.AsyncSessionLocal() as session:
        due = [to_payment_event(m) for m in await PipelineRunRepository(session).find_due(now, limit)]

    outcomes: Counter = Counter()
    for event in due:
        result = await live_pipeline.run_live_pipeline(event)
        outcomes[result["status"]] += 1
    summary = {"due": len(due), **outcomes}
    if due:
        logger.info(f"[PipelineReconcile] {summary}")
    return summary


async def run_reconciler_loop(interval_seconds: float, stop: asyncio.Event) -> None:
    """Reconcile every interval_seconds until stop is set; an error in one pass never stops the loop."""
    while not stop.is_set():
        try:
            await reconcile_pipeline_runs()
        except Exception as exc:
            logger.error(f"[PipelineReconcileError] ({type(exc).__name__})")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
