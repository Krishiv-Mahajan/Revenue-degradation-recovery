"""
Integration tests for Stage 6 Intervention Decisioning lifecycle.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.domain.failure_prediction_models import FailurePrediction
from src.core.domain.intervention_models import DecisionType, GateVerdict, InterventionRouteKey
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


def add_payment_event(
    session: AsyncSession,
    payment_id: str,
    event_type: str,
    ingested_at: datetime,
    amount_minor_units: int = 10000,
    payment_method: str = "card",
    bank: str = "HDFC",
):
    model = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=ingested_at,
        event_type=event_type,
        currency="INR",
        amount_minor_units=amount_minor_units,
        payment_status="processed",
        payment_method=payment_method,
        bank=bank,
        ingested_at=ingested_at,
    )
    session.add(model)


async def add_stage5_prediction(
    pred_repo: FailurePredictionRepository,
    payment_id: str,
    predicted_at: datetime,
    failure_probability: float,
    status: str = "PREDICTED",
) -> FailurePrediction:
    pred = FailurePrediction(
        prediction_id=uuid.uuid4(),
        payment_attempt_id=payment_id,
        prediction_version=1,
        predicted_at=predicted_at,
        prediction_horizon="30m",
        failure_probability=failure_probability,
        risk_band="HIGH" if failure_probability >= 0.70 else "LOW",
        prediction_status=status,
        model_name="test_model",
        model_version="1.0",
        feature_schema_version="1.0",
        feature_snapshot={},
        input_fingerprint="fp_" + payment_id,
        created_at=predicted_at,
    )
    await pred_repo.append_prediction(pred)
    return pred


@pytest.mark.asyncio
async def test_lifecycle_act_verdict(db_session: AsyncSession):
    """
    High risk (0.85) + high utility -> ACT verdict with selected permitted route.
    """
    feat_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    inter_repo = InterventionRepository(db_session)
    service = InterventionDecisionService(db_session, inter_repo, feat_repo)

    payment_id = "pay_act_1"
    T = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    # Ingest authorized event
    add_payment_event(db_session, payment_id, "payment.authorized", T, amount_minor_units=50000)
    await db_session.flush()

    # Stage 5 prediction with high failure risk
    await add_stage5_prediction(pred_repo, payment_id, T, failure_probability=0.85)

    decision = await service.decide(payment_id, T)

    assert decision.decision_type == DecisionType.ACT
    assert decision.selected_route_id is not None
    assert decision.selected_route_id in [
        InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
        InterventionRouteKey.DYNAMIC_RETRY_BACKOFF,
        InterventionRouteKey.FALLBACK_PAYMENT_LINK,
    ]
    assert decision.decision_version == 1
    assert decision.failure_probability == 0.85
    assert decision.gate_verdict == GateVerdict.PASSED
    assert 0.0 <= decision.decision_confidence <= 1.0


@pytest.mark.asyncio
async def test_lifecycle_monitor_verdict(db_session: AsyncSession):
    """
    Moderate risk (0.50) -> MONITOR verdict.
    """
    feat_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    inter_repo = InterventionRepository(db_session)
    service = InterventionDecisionService(db_session, inter_repo, feat_repo)

    payment_id = "pay_mon_1"
    T = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T)
    await db_session.flush()

    # Stage 5 prediction with moderate risk
    await add_stage5_prediction(pred_repo, payment_id, T, failure_probability=0.50)

    decision = await service.decide(payment_id, T)

    assert decision.decision_type == DecisionType.MONITOR
    assert decision.selected_route_id is None
    assert decision.decision_version == 1
    assert decision.gate_verdict == GateVerdict.PASSED
    assert 0.0 <= decision.decision_confidence <= 1.0


@pytest.mark.asyncio
async def test_lifecycle_no_action_verdict(db_session: AsyncSession):
    """
    Low risk (0.15) -> NO_ACTION verdict.
    """
    feat_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    inter_repo = InterventionRepository(db_session)
    service = InterventionDecisionService(db_session, inter_repo, feat_repo)

    payment_id = "pay_no_act_1"
    T = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T)
    await db_session.flush()

    # Stage 5 prediction with low risk
    await add_stage5_prediction(pred_repo, payment_id, T, failure_probability=0.15)

    decision = await service.decide(payment_id, T)

    assert decision.decision_type == DecisionType.NO_ACTION
    assert decision.selected_route_id is None
    assert decision.decision_version == 1
    assert decision.gate_verdict == GateVerdict.PASSED


@pytest.mark.asyncio
async def test_lifecycle_pre_terminal_invariance_gate(db_session: AsyncSession):
    """
    If a terminal event arrived before T_decide, Stage 6 immediately forces NO_ACTION.
    """
    feat_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    inter_repo = InterventionRepository(db_session)
    service = InterventionDecisionService(db_session, inter_repo, feat_repo)

    payment_id = "pay_settled_1"
    T_auth = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    T_failed = datetime(2026, 9, 2, 10, 1, tzinfo=timezone.utc)
    T_decide = datetime(2026, 9, 2, 10, 2, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T_auth)
    add_payment_event(db_session, payment_id, "payment.failed", T_failed)
    await db_session.flush()

    await add_stage5_prediction(pred_repo, payment_id, T_auth, failure_probability=0.90)

    decision = await service.decide(payment_id, T_decide)

    assert decision.decision_type == DecisionType.NO_ACTION
    assert decision.selected_route_id is None
    assert decision.gate_verdict == GateVerdict.FAILED_TERMINAL_OUTCOME_INGESTED
    assert decision.decision_confidence == 1.0
