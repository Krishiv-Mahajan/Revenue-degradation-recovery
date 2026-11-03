"""
Registers Stage 5-8 payment attempts in integration tests, the way the live pipeline does: from the
attempt's trigger event, which the test must already have added to the session. Also records the
Stage 5 prediction a test decision references, because decisions may only reference a prediction of
their own attempt and provider (fk_decision_prediction, fk_decision_prediction_attempt).
"""
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.payment_attempts import (
    PAYPAL_TRIGGER_EVENT_TYPE,
    PIPELINE_TRIGGER_EVENT_TYPE,
    AttemptRef,
    paypal_order_id,
)
from src.core.domain.source_systems import PAYPAL_SOURCE_SYSTEM
from src.infrastructure.models import FailurePredictionModel, PaymentEventModel
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


async def record_prediction(
    session: AsyncSession,
    attempt: AttemptRef,
    predicted_at: datetime,
    failure_probability: float = 0.85,
) -> uuid.UUID:
    """Store the next PREDICTED version for a registered attempt and return its prediction_id."""
    latest = await session.scalar(
        select(func.max(FailurePredictionModel.prediction_version)).where(
            FailurePredictionModel.payment_attempt_id == attempt.payment_attempt_id,
            FailurePredictionModel.source_system == attempt.source_system,
        )
    )
    prediction_id = uuid.uuid4()
    session.add(FailurePredictionModel(
        prediction_id=prediction_id,
        payment_attempt_id=attempt.payment_attempt_id,
        source_system=attempt.source_system,
        prediction_version=(latest or 0) + 1,
        predicted_at=predicted_at,
        prediction_horizon="30m",
        failure_probability=failure_probability,
        risk_band="HIGH",
        prediction_status="PREDICTED",
        model_name="test_model",
        model_version="1.0",
        feature_schema_version="v1.0.0",
        feature_snapshot={},
        input_fingerprint=f"sha256:{prediction_id.hex}",
        created_at=predicted_at,
    ))
    await session.flush()
    return prediction_id
