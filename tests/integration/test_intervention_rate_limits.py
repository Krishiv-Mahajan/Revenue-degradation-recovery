"""
Integration tests for Stage 6 Global Rate Limits, Atomic Admission, and Route Cooldowns.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.domain.failure_prediction_models import FailurePrediction
from src.core.domain.intervention_models import DecisionType, GateVerdict, InterventionRouteKey
from src.core.intervention.policy import InterventionPolicy, RoutePolicyDefinition, get_default_policy
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


def add_payment_event(session: AsyncSession, payment_id: str, T: datetime):
    model = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=T,
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=50000,
        payment_status="processed",
        payment_method="card",
        bank="HDFC",
        ingested_at=T,
    )
    session.add(model)


async def add_prediction(pred_repo: FailurePredictionRepository, payment_id: str, T: datetime):
    pred = FailurePrediction(
        prediction_id=uuid.uuid4(),
        payment_attempt_id=payment_id,
        prediction_version=1,
        predicted_at=T,
        prediction_horizon="30m",
        failure_probability=0.85,
        risk_band="HIGH",
        prediction_status="PREDICTED",
        model_name="test",
        model_version="1.0",
        feature_schema_version="1.0",
        feature_snapshot={},
        input_fingerprint="fp_" + payment_id,
        created_at=T,
    )
    await pred_repo.append_prediction(pred)


@pytest.mark.asyncio
async def test_global_rate_limit_circuit_breaker(db_session: AsyncSession):
    """
    When global rate limit of ACT decisions is exceeded, subsequent attempts are forced to NO_ACTION.
    """
    feat_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    inter_repo = InterventionRepository(db_session)

    # Set strict limit of 2 ACT decisions per minute
    base_policy = get_default_policy()
    policy = InterventionPolicy(
        policy_id="test-limit-policy",
        policy_version="1.0.0",
        kill_switch_enabled=False,
        act_risk_threshold=base_policy.act_risk_threshold,
        monitor_risk_threshold=base_policy.monitor_risk_threshold,
        min_policy_utility_threshold=base_policy.min_policy_utility_threshold,
        global_rate_limit_per_minute=2,
        routes=base_policy.routes,
    )
    service = InterventionDecisionService(db_session, inter_repo, feat_repo, policy=policy)

    T = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    # Attempt 1 -> Passes
    add_payment_event(db_session, "pay_1", T)
    await db_session.flush()
    await add_prediction(pred_repo, "pay_1", T)
    d1 = await service.decide("pay_1", T)
    assert d1.decision_type == DecisionType.ACT

    # Attempt 2 -> Passes (total = 2)
    add_payment_event(db_session, "pay_2", T)
    await db_session.flush()
    await add_prediction(pred_repo, "pay_2", T)
    d2 = await service.decide("pay_2", T)
    assert d2.decision_type == DecisionType.ACT

    # Attempt 3 -> Exceeds limit (2 >= 2) -> Forced to NO_ACTION
    add_payment_event(db_session, "pay_3", T)
    await db_session.flush()
    await add_prediction(pred_repo, "pay_3", T)
    d3 = await service.decide("pay_3", T)
    assert d3.decision_type == DecisionType.NO_ACTION
    assert d3.selected_route_id is None
    assert d3.gate_verdict == GateVerdict.FAILED_GLOBAL_RATE_LIMIT
    assert d3.decision_confidence == 1.0


@pytest.mark.asyncio
async def test_route_cooldown_enforcement(db_session: AsyncSession):
    """
    If a route was chosen for a payment attempt, it cannot be chosen again within cooldown period.
    """
    feat_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    inter_repo = InterventionRepository(db_session)

    # Configure only 1 route with a 300s cooldown
    routes = {
        InterventionRouteKey.RETRY_SECONDARY_GATEWAY: RoutePolicyDefinition(
            route_key=InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
            target_dimensions=("BANK", "GLOBAL"),
            policy_recovery_rate=Decimal("0.55"),
            policy_cost_minor_units=100,
            cooldown_seconds=300,
            requires_diagnosis=False,
            requires_strong_rca=False,
            enabled=True,
        )
    }
    policy = InterventionPolicy(
        policy_id="test-cooldown-policy",
        policy_version="1.0.0",
        kill_switch_enabled=False,
        act_risk_threshold=0.70,
        monitor_risk_threshold=0.35,
        min_policy_utility_threshold=Decimal("50"),
        global_rate_limit_per_minute=100,
        routes=routes,
    )
    service = InterventionDecisionService(db_session, inter_repo, feat_repo, policy=policy)

    payment_id = "pay_cd_1"
    T1 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    T2 = T1 + timedelta(seconds=60)  # within 300s cooldown

    add_payment_event(db_session, payment_id, T1)
    await db_session.flush()
    await add_prediction(pred_repo, payment_id, T1)

    # First decision at T1 -> ACT with the route
    d1 = await service.decide(payment_id, T1)
    assert d1.decision_type == DecisionType.ACT
    assert d1.selected_route_id == InterventionRouteKey.RETRY_SECONDARY_GATEWAY

    # Second decision at T2 (1 min later) -> Route is on cooldown. Since no other routes exist, NO_ACTION.
    d2 = await service.decide(payment_id, T2)
    assert d2.decision_type == DecisionType.NO_ACTION
    assert d2.selected_route_id is None
    assert d2.gate_verdict == GateVerdict.FAILED_NO_ELIGIBLE_ROUTES
