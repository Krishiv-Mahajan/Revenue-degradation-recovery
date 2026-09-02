"""
Integration tests for Stage 8 Temporal Correctness, Late-Event Replay, and Information Barriers.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.attribution.attribution_service import CounterfactualAttributionService
from src.core.domain.attribution_models import AttributionStatus, TreatmentStatus
from src.core.domain.intervention_models import (
    DecisionType,
    GateVerdict,
    InterventionDecision,
    InterventionRouteKey,
)
from src.core.execution.execution_service import InterventionExecutionService
from src.core.execution.observation_service import PaymentOutcomeObservationService
from src.infrastructure.attribution_repository import AttributionRepository
from src.infrastructure.execution_repository import ExecutionRepository
from src.infrastructure.intervention_repository import InterventionRepository
from src.infrastructure.models import (
    Base,
    FailurePredictionModel,
    PaymentEventModel,
)

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
    amount_minor_units: int = 50000,
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
        payment_status="processed" if event_type != "payment.failed" else "failed",
        payment_method="card",
        bank="HDFC",
        ingested_at=ingested_at,
    )
    session.add(model)


def add_stage5_prediction(
    session: AsyncSession,
    payment_id: str,
    predicted_at: datetime,
    failure_prob: float = 0.85,
) -> uuid.UUID:
    pred_id = uuid.uuid4()
    model = FailurePredictionModel(
        prediction_id=pred_id,
        payment_attempt_id=payment_id,
        prediction_version=1,
        predicted_at=predicted_at,
        prediction_horizon="IMMEDIATE_ATTEMPT",
        failure_probability=failure_prob,
        risk_band="HIGH",
        prediction_status="PREDICTED",
        model_name="synthetic-development-v1",
        model_version="1.0.0",
        feature_schema_version="1.0.0",
        feature_snapshot={},
        input_fingerprint=f"fp_pred_{payment_id}",
        created_at=predicted_at,
    )
    session.add(model)
    return pred_id


async def create_act_decision(
    inter_repo: InterventionRepository,
    payment_id: str,
    stage5_pred_id: uuid.UUID,
    decided_at: datetime,
) -> InterventionDecision:
    decision = InterventionDecision(
        decision_id=uuid.uuid4(),
        payment_attempt_id=payment_id,
        decision_version=1,
        decided_at=decided_at,
        decision_type=DecisionType.ACT,
        selected_route_id=InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
        failure_probability=0.85,
        stage5_prediction_id=stage5_pred_id,
        diagnosis_confidence="STRONG",
        decision_confidence=0.9,
        gate_verdict=GateVerdict.PASSED,
        policy_id="test_policy",
        policy_version="1.0.0",
        input_fingerprint=f"fp_dec_{payment_id}",
        evaluation_audit_payload={},
        created_at=decided_at,
    )
    await inter_repo.append_decision(decision)
    return decision


@pytest.mark.asyncio
async def test_late_arriving_event_appends_new_version_immutably(db_session: AsyncSession):
    """
    1. At T1: Payment in flight -> Attribution Version 1: UNRESOLVED_IN_FLIGHT, GMV = 0.
    2. At T2: Late payment.captured event ingested.
    3. At T3: Outcome observation evaluated -> Version 2.
    4. At T4: Attribution re-run -> Version 2: ATTRIBUTED, GMV = 25500.
    5. Version 1 remains unmutated in DB.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    attr_repo = AttributionRepository(db_session)

    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)
    attr_service = CounterfactualAttributionService(attr_repo)

    payment_id = "pay_late_event"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    T1 = T0 + timedelta(seconds=10)
    T2_event = T0 + timedelta(seconds=20)
    T3_obs = T0 + timedelta(seconds=25)
    T4_attr = T0 + timedelta(seconds=30)

    add_payment_event(db_session, payment_id, "payment.authorized", T0, amount_minor_units=50000)
    pred_id = add_stage5_prediction(db_session, payment_id, T0)
    await db_session.flush()

    decision = await create_act_decision(inter_repo, payment_id, pred_id, T0)
    cmd = await exec_service.create_command(decision, t_create=T0)
    await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    # Initial observation & attribution at T1 (in flight)
    await obs_service.observe(cmd.command_id, as_of_timestamp=T1)
    attr_v1 = await attr_service.attribute_payment_intervention(cmd.command_id, as_of_timestamp=T1)

    assert attr_v1.attribution_version == 1
    assert attr_v1.attribution_status == AttributionStatus.UNRESOLVED_IN_FLIGHT
    assert attr_v1.attributed_protected_gmv_minor_units == 0

    # Late capture arrives at T2
    add_payment_event(db_session, payment_id, "payment.captured", T2_event, amount_minor_units=50000)
    await db_session.flush()

    # Observation at T3 (captured)
    await obs_service.observe(cmd.command_id, as_of_timestamp=T3_obs)

    # Attribution at T4
    attr_v2 = await attr_service.attribute_payment_intervention(cmd.command_id, as_of_timestamp=T4_attr)

    assert attr_v2.attribution_version == 2
    assert attr_v2.attribution_status == AttributionStatus.ATTRIBUTED
    assert attr_v2.attributed_protected_gmv_minor_units == 25500

    # Verify both versions exist immutably in DB
    all_attrs = await attr_repo.get_all_attributions(cmd.command_id)
    assert len(all_attrs) == 2
    assert all_attrs[0].attribution_version == 1
    assert all_attrs[0].attribution_status == AttributionStatus.UNRESOLVED_IN_FLIGHT
    assert all_attrs[0].attributed_protected_gmv_minor_units == 0
    assert all_attrs[1].attribution_version == 2
    assert all_attrs[1].attribution_status == AttributionStatus.ATTRIBUTED
    assert all_attrs[1].attributed_protected_gmv_minor_units == 25500


@pytest.mark.asyncio
async def test_temporal_information_barrier_hides_future_events(db_session: AsyncSession):
    """
    Events ingested after as_of_timestamp are strictly invisible.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    attr_repo = AttributionRepository(db_session)

    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)
    attr_service = CounterfactualAttributionService(attr_repo)

    payment_id = "pay_barrier"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    T1_cutoff = T0 + timedelta(seconds=10)
    T2_future_event = T0 + timedelta(seconds=50)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    pred_id = add_stage5_prediction(db_session, payment_id, T0)
    await db_session.flush()

    decision = await create_act_decision(inter_repo, payment_id, pred_id, T0)
    cmd = await exec_service.create_command(decision, t_create=T0)
    await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    # Event ingested at T2
    add_payment_event(db_session, payment_id, "payment.captured", T2_future_event)
    await db_session.flush()

    # Observation as-of T1 sees no event
    await obs_service.observe(cmd.command_id, as_of_timestamp=T1_cutoff)

    # Attribution as-of T1 cannot see the future event
    attr = await attr_service.attribute_payment_intervention(cmd.command_id, as_of_timestamp=T1_cutoff)

    assert attr.attribution_status == AttributionStatus.UNRESOLVED_IN_FLIGHT
    assert attr.attributed_protected_gmv_minor_units == 0


@pytest.mark.asyncio
async def test_execution_completed_after_terminal_event_not_attributed(db_session: AsyncSession):
    """
    CRITICAL INVARIANT: If the payment was captured BEFORE execution completed,
    the intervention did not precede the outcome. Treatment status is UNTREATED,
    and protected GMV is strictly 0.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    attr_repo = AttributionRepository(db_session)

    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)
    attr_service = CounterfactualAttributionService(attr_repo)

    payment_id = "pay_late_exec"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    pred_id = add_stage5_prediction(db_session, payment_id, T0)
    await db_session.flush()

    decision = await create_act_decision(inter_repo, payment_id, pred_id, T0)
    cmd = await exec_service.create_command(decision, t_create=T0)

    # Terminal event happened at T0 + 2s
    T_cap = T0 + timedelta(seconds=2)
    add_payment_event(db_session, payment_id, "payment.captured", T_cap)
    await db_session.flush()

    # Execution attempt completed at T0 + 10s (after terminal event)
    T_exec_claim = T0 + timedelta(seconds=5)
    await exec_service.execute_command(cmd.command_id, t_claim=T_exec_claim)

    T_obs = T0 + timedelta(seconds=12)
    await obs_service.observe(cmd.command_id, as_of_timestamp=T_obs)

    attr = await attr_service.attribute_payment_intervention(cmd.command_id, as_of_timestamp=T_obs + timedelta(seconds=1))

    # Precedence violated: treatment was after outcome
    assert attr.treatment_status == TreatmentStatus.UNTREATED
    assert attr.attribution_status == AttributionStatus.UNATTRIBUTED_UNTREATED
    assert attr.attributed_protected_gmv_minor_units == 0
