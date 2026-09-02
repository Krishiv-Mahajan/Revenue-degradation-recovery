"""
Integration tests for Stage 6 Temporal Correctness and Time-Travel Invariance.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.domain.failure_prediction_models import FailurePrediction
from src.core.domain.intervention_models import DecisionType, GateVerdict
from src.core.intervention.service import InterventionDecisionService
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.infrastructure.failure_prediction_repository import FailurePredictionRepository
from src.infrastructure.intervention_repository import InterventionRepository
from src.infrastructure.models import Base, PaymentEventModel

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_future_terminal_event_does_not_leak_into_earlier_decision(db_session: AsyncSession):
    """
    If a terminal event arrived at T + 5min, a decision evaluated as-of T
    must NOT see the terminal event, remaining eligible for prediction/action.
    """
    feat_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    inter_repo = InterventionRepository(db_session)
    service = InterventionDecisionService(db_session, inter_repo, feat_repo)

    payment_id = "pay_temp_1"
    T_decide = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    T_future_failed = T_decide + timedelta(minutes=5)

    # Ingest auth at T_decide
    auth = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=T_decide,
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=50000,
        payment_status="processed",
        payment_method="card",
        bank="HDFC",
        ingested_at=T_decide,
    )
    # Ingest future failure event that exists in DB (e.g. during replay)
    future_fail = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=T_future_failed,
        event_type="payment.failed",
        currency="INR",
        amount_minor_units=50000,
        payment_status="failed",
        payment_method="card",
        bank="HDFC",
        ingested_at=T_future_failed,
    )
    db_session.add(auth)
    db_session.add(future_fail)
    await db_session.flush()

    # Stage 5 prediction available at T_decide
    pred = FailurePrediction(
        prediction_id=uuid.uuid4(),
        payment_attempt_id=payment_id,
        prediction_version=1,
        predicted_at=T_decide,
        prediction_horizon="30m",
        failure_probability=0.85,
        risk_band="HIGH",
        prediction_status="PREDICTED",
        model_name="test",
        model_version="1.0",
        feature_schema_version="1.0",
        feature_snapshot={},
        input_fingerprint="fp1",
        created_at=T_decide,
    )
    await pred_repo.append_prediction(pred)

    # Evaluating as-of T_decide must NOT trigger FAILED_TERMINAL_OUTCOME_INGESTED
    decision = await service.decide(payment_id, T_decide)
    assert decision.decision_type == DecisionType.ACT
    assert decision.gate_verdict == GateVerdict.PASSED

    # Evaluating as-of T_future_failed + 1min MUST see the terminal event and force NO_ACTION
    T_after = T_future_failed + timedelta(minutes=1)
    decision_after = await service.decide(payment_id, T_after)
    assert decision_after.decision_type == DecisionType.NO_ACTION
    assert decision_after.gate_verdict == GateVerdict.FAILED_TERMINAL_OUTCOME_INGESTED


@pytest.mark.asyncio
async def test_future_stage5_prediction_does_not_leak_into_earlier_decision(db_session: AsyncSession):
    """
    If a Stage 5 prediction was evaluated at T + 5min, a decision evaluated as-of T
    must NOT see it, triggering FAILED_STAGE5_UNAVAILABLE.
    """
    feat_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    inter_repo = InterventionRepository(db_session)
    service = InterventionDecisionService(db_session, inter_repo, feat_repo)

    payment_id = "pay_temp_2"
    T_decide = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    T_future_pred = T_decide + timedelta(minutes=5)

    auth = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=T_decide,
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=50000,
        payment_status="processed",
        payment_method="card",
        bank="HDFC",
        ingested_at=T_decide,
    )
    db_session.add(auth)
    await db_session.flush()

    # Prediction evaluated in the future
    pred = FailurePrediction(
        prediction_id=uuid.uuid4(),
        payment_attempt_id=payment_id,
        prediction_version=1,
        predicted_at=T_future_pred,
        prediction_horizon="30m",
        failure_probability=0.85,
        risk_band="HIGH",
        prediction_status="PREDICTED",
        model_name="test",
        model_version="1.0",
        feature_schema_version="1.0",
        feature_snapshot={},
        input_fingerprint="fp2",
        created_at=T_future_pred,
    )
    await pred_repo.append_prediction(pred)

    # Decision at T_decide must not see future prediction
    decision = await service.decide(payment_id, T_decide)
    assert decision.decision_type == DecisionType.NO_ACTION
    assert decision.gate_verdict == GateVerdict.FAILED_STAGE5_UNAVAILABLE
