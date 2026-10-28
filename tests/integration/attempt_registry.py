"""
Registers Stage 5-8 payment attempts in integration tests, the way the live pipeline does: from the
attempt's trigger event, which the test must already have added to the session.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.payment_attempts import (
    PAYPAL_TRIGGER_EVENT_TYPE,
    PIPELINE_TRIGGER_EVENT_TYPE,
    AttemptRef,
    paypal_order_id,
)
from src.core.domain.source_systems import PAYPAL_SOURCE_SYSTEM
from src.infrastructure.models import PaymentEventModel
from src.infrastructure.payment_attempt_repository import PaymentAttemptRepository


async def register_attempt(session: AsyncSession, payment_attempt_id: str) -> AttemptRef:
    """Register the attempt from its earliest stored trigger event; fails if none was stored."""
    await session.flush()
    order_id = paypal_order_id(payment_attempt_id)
    if order_id is not None:
        conditions = (
            PaymentEventModel.payment_id == order_id,
            PaymentEventModel.source_system == PAYPAL_SOURCE_SYSTEM,
            PaymentEventModel.event_type == PAYPAL_TRIGGER_EVENT_TYPE,
        )
    else:
        conditions = (
            PaymentEventModel.payment_id == payment_attempt_id,
            PaymentEventModel.source_system != PAYPAL_SOURCE_SYSTEM,
            PaymentEventModel.event_type == PIPELINE_TRIGGER_EVENT_TYPE,
        )
    stmt = (
        select(PaymentEventModel)
        .where(*conditions)
        .order_by(PaymentEventModel.ingested_at, PaymentEventModel.event_id)
        .limit(1)
    )
    trigger = (await session.execute(stmt)).scalar_one()
    return (await PaymentAttemptRepository(session).register(trigger)).ref
