"""
Integration tests for Stage 8 Counterfactual Attribution lifecycle.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.attribution.attribution_service import CounterfactualAttributionService
from src.core.domain.attribution_models import AttributionStatus, TreatmentStatus
from src.core.domain.execution_models import CommandStatus
from src.core.domain.intervention_models import (
    DecisionType,
    GateVerdict,
    InterventionDecision,
    InterventionRouteKey,
)
from src.core.execution.execution_service import InterventionExecutionService
from src.core.execution.observation_service import PaymentOutcomeObservationService
from src.core.execution.simulator import SimulatorInterventionExecutor, SimulatorMode
from src.infrastructure.attribution_repository import AttributionRepository
from src.infrastructure.execution_repository import ExecutionRepository
from src.infrastructure.intervention_repository import InterventionRepository
from src.infrastructure.models import (
    Base,
    CounterfactualAttributionModel,
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
    model_name: str = "synthetic-development-v1",
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
        model_name=model_name,
        model_version="1.0.0",
        feature_schema_version="1.0.0",
        feature_snapshot={},
        input_fingerprint=f"fp_pred_{payment_id}",
        created_at=predicted_at,
    )
    session.add(model)
    return pred_id


async def create_stage6_decision(
    inter_repo: InterventionRepository,
    payment_id: str,
    stage5_pred_id: uuid.UUID,
    decided_at: datetime,
    decision_type: DecisionType = DecisionType.ACT,
    route: InterventionRouteKey = InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
) -> InterventionDecision:
    decision = InterventionDecision(
        decision_id=uuid.uuid4(),
        payment_attempt_id=payment_id,
        decision_version=1,
        decided_at=decided_at,
        decision_type=decision_type,
        selected_route_id=route if decision_type == DecisionType.ACT else None,
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
async def test_full_stage8_lifecycle_captured_and_attributed(db_session: AsyncSession):
    """
    Complete flow:
    Stage 1 Auth -> Stage 5 Pred -> Stage 6 Decision ACT -> Stage 7 Command SUCCEEDED ->
    Stage 1 Captured -> Stage 7 Observed -> Stage 8 Attributed.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    attr_repo = AttributionRepository(db_session)

    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)
    attr_service = CounterfactualAttributionService(attr_repo)

    payment_id = "pay_attr_1"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    amount = 50000  # 500 INR

    # 1. Ingest payment authorized
    add_payment_event(db_session, payment_id, "payment.authorized", T0, amount_minor_units=amount)
    pred_id = add_stage5_prediction(db_session, payment_id, T0, failure_prob=0.85)
    await db_session.flush()

    # 2. Stage 6 Decision
    decision = await create_stage6_decision(inter_repo, payment_id, pred_id, T0)

    # 3. Stage 7 Execution
    cmd = await exec_service.create_command(decision, t_create=T0)
    T_exec = T0 + timedelta(seconds=1)
    await exec_service.execute_command(cmd.command_id, t_claim=T_exec)

    # 4. Ingest Captured event at T0 + 15s (<= 60s after execution)
    T_cap = T0 + timedelta(seconds=15)
    add_payment_event(db_session, payment_id, "payment.captured", T_cap, amount_minor_units=amount)
    await db_session.flush()

    # 5. Observe outcome
    T_obs = T0 + timedelta(seconds=20)
    await obs_service.observe(cmd.command_id, as_of_timestamp=T_obs)

    # 6. Evaluate Stage 8 Attribution
    T_attr = T0 + timedelta(seconds=25)
    attr = await attr_service.attribute_payment_intervention(cmd.command_id, as_of_timestamp=T_attr)

    # Verify attribution results:
    # p0 = 0.8500
    # c_provenance = 0.6000 (synthetic)
    # c_diagnosis = 1.0000 (STRONG)
    # c_timing = 1.0000 (14s <= 60s)
    # alpha = 0.6000
    # loss_exposure = round(50,000 * 0.8500) = 42,500
    # protected_gmv = round(50,000 * 0.8500 * 0.6000) = 25,500
    assert attr.attribution_status == AttributionStatus.ATTRIBUTED
    assert attr.treatment_status == TreatmentStatus.TREATED
    assert attr.observed_payment_outcome == "CAPTURED"
    assert attr.payment_amount_minor_units == 50000
    assert attr.counterfactual_loss_exposure_minor_units == 42500
    assert attr.counterfactual_natural_success_gmv_minor_units == 7500
    assert attr.attributed_protected_gmv_minor_units == 25500
    assert attr.attribution_confidence == Decimal("0.6000")
    assert attr.is_synthetic_baseline is True
    assert attr.is_simulated_execution is True

    # Audit payload checks
    audit = attr.attribution_audit_payload
    assert audit["methodology_name"] == "COUNTERFACTUAL_RISK_WEIGHTED_V1"
    assert audit["stage5_model_provenance"] == "SYNTHETIC_DEVELOPMENT"
    assert audit["counterfactual_risk_proxy_p0"] == "0.8500"
    assert audit["treatment_evidence"]["execution_status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_lifecycle_captured_but_execution_failed_zero_protected(db_session: AsyncSession):
    """
    CRITICAL INVARIANT: Factual capture occurred, but intervention execution failed.
    Attribution is UNATTRIBUTED_EXECUTION_FAILED and protected GMV is strictly 0.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    attr_repo = AttributionRepository(db_session)

    failing_exec = SimulatorInterventionExecutor(mode=SimulatorMode.ALWAYS_FAIL)
    exec_service = InterventionExecutionService(db_session, exec_repo, executor=failing_exec)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)
    attr_service = CounterfactualAttributionService(attr_repo)

    payment_id = "pay_exec_fail"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    pred_id = add_stage5_prediction(db_session, payment_id, T0)
    await db_session.flush()

    decision = await create_stage6_decision(inter_repo, payment_id, pred_id, T0)
    cmd = await exec_service.create_command(decision, t_create=T0)
    await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    # Payment succeeded
    T_cap = T0 + timedelta(seconds=10)
    add_payment_event(db_session, payment_id, "payment.captured", T_cap)
    await db_session.flush()

    await obs_service.observe(cmd.command_id, as_of_timestamp=T_cap + timedelta(seconds=1))
    attr = await attr_service.attribute_payment_intervention(cmd.command_id, as_of_timestamp=T_cap + timedelta(seconds=2))

    assert attr.attribution_status == AttributionStatus.UNATTRIBUTED_EXECUTION_FAILED
    assert attr.treatment_status == TreatmentStatus.EXECUTION_FAILED
    assert attr.attributed_protected_gmv_minor_units == 0


@pytest.mark.asyncio
async def test_lifecycle_execution_success_but_payment_failed_zero_protected(db_session: AsyncSession):
    """
    CRITICAL INVARIANT: Execution succeeded, but payment failed.
    Attribution is UNATTRIBUTED_PAYMENT_FAILED and protected GMV is strictly 0.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    attr_repo = AttributionRepository(db_session)

    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)
    attr_service = CounterfactualAttributionService(attr_repo)

    payment_id = "pay_fail_succ_exec"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    pred_id = add_stage5_prediction(db_session, payment_id, T0)
    await db_session.flush()

    decision = await create_stage6_decision(inter_repo, payment_id, pred_id, T0)
    cmd = await exec_service.create_command(decision, t_create=T0)
    await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    # Payment failed
    T_fail = T0 + timedelta(seconds=10)
    add_payment_event(db_session, payment_id, "payment.failed", T_fail)
    await db_session.flush()

    await obs_service.observe(cmd.command_id, as_of_timestamp=T_fail + timedelta(seconds=1))
    attr = await attr_service.attribute_payment_intervention(cmd.command_id, as_of_timestamp=T_fail + timedelta(seconds=2))

    assert attr.attribution_status == AttributionStatus.UNATTRIBUTED_PAYMENT_FAILED
    assert attr.attributed_protected_gmv_minor_units == 0


@pytest.mark.asyncio
async def test_lifecycle_payment_in_flight_unresolved_zero_protected(db_session: AsyncSession):
    """
    Payment still in flight when attribution is evaluated.
    Status is UNRESOLVED_IN_FLIGHT and protected GMV is strictly 0.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    attr_repo = AttributionRepository(db_session)

    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)
    attr_service = CounterfactualAttributionService(attr_repo)

    payment_id = "pay_in_flight"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    pred_id = add_stage5_prediction(db_session, payment_id, T0)
    await db_session.flush()

    decision = await create_stage6_decision(inter_repo, payment_id, pred_id, T0)
    cmd = await exec_service.create_command(decision, t_create=T0)
    await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    # Observe immediately before terminal event arrives
    T_obs = T0 + timedelta(seconds=5)
    await obs_service.observe(cmd.command_id, as_of_timestamp=T_obs)

    attr = await attr_service.attribute_payment_intervention(cmd.command_id, as_of_timestamp=T_obs)

    assert attr.attribution_status == AttributionStatus.UNRESOLVED_IN_FLIGHT
    assert attr.attributed_protected_gmv_minor_units == 0


@pytest.mark.asyncio
async def test_attribution_idempotency_same_as_of(db_session: AsyncSession):
    """
    Calling attribute_payment_intervention twice with the identical as_of_timestamp
    returns the exact same record without inserting duplicate rows.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    attr_repo = AttributionRepository(db_session)

    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)
    attr_service = CounterfactualAttributionService(attr_repo)

    payment_id = "pay_idem"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    pred_id = add_stage5_prediction(db_session, payment_id, T0)
    await db_session.flush()

    decision = await create_stage6_decision(inter_repo, payment_id, pred_id, T0)
    cmd = await exec_service.create_command(decision, t_create=T0)
    await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    T_cap = T0 + timedelta(seconds=10)
    add_payment_event(db_session, payment_id, "payment.captured", T_cap)
    await db_session.flush()

    T_obs = T0 + timedelta(seconds=15)
    await obs_service.observe(cmd.command_id, as_of_timestamp=T_obs)

    T_attr = T0 + timedelta(seconds=20)
    attr1 = await attr_service.attribute_payment_intervention(cmd.command_id, as_of_timestamp=T_attr)
    attr2 = await attr_service.attribute_payment_intervention(cmd.command_id, as_of_timestamp=T_attr)

    assert attr1.attribution_id == attr2.attribution_id
    assert attr1.attribution_version == attr2.attribution_version

    # Verify only 1 row in DB
    stmt = select(func.count()).where(CounterfactualAttributionModel.command_id == cmd.command_id)
    count = (await db_session.execute(stmt)).scalar()
    assert count == 1
