"""
Durable pipeline runs (transactional outbox).

Every stored event that needs the Stages 2-8 pipeline gets a pipeline_runs row in the same transaction
as the event itself, so a crash after ingestion can never lose the run: the reconciler finds and replays
it. The background task and the reconciler both claim the row atomically before running.

Lifecycle: PENDING -> RUNNING -> SUCCEEDED, or RUNNING -> FAILED -> RUNNING (retry) up to
MAX_PIPELINE_ATTEMPTS. A RUNNING row whose claim is older than RUNNING_LEASE is treated as abandoned
(the worker died) and may be claimed again.
"""
from datetime import timedelta
from enum import Enum

from src.core.domain.models import PaymentEvent
from src.core.domain.payment_attempts import resolve_pipeline_attempt
from src.core.domain.source_systems import PAYPAL_SOURCE_SYSTEM

MAX_PIPELINE_ATTEMPTS = 5
RUNNING_LEASE = timedelta(minutes=10)
PENDING_GRACE = timedelta(seconds=30)


class PipelineRunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def pipeline_required(event: PaymentEvent) -> bool:
    """
    Whether a newly stored event must run the pipeline: every Razorpay/synthetic event (Stages 2-4 use
    them all), and PayPal events that start or resolve a PayPal attempt.
    """
    if event.source_system == PAYPAL_SOURCE_SYSTEM:
        return resolve_pipeline_attempt(event) is not None
    return True
