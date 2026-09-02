"""
Integration tests for Stage 7 Intervention Execution lifecycle.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.domain.execution_exceptions import InvalidDecisionForExecutionError
from src.core.domain.execution_models import (
    CommandStatus,
    ExecutionResultStatus,
    PaymentOutcome,
)
from src.core.domain.intervention_models import (
    DecisionType,
    GateVerdict,
    InterventionDecision,
    InterventionRouteKey,
)
from src.core.execution.execution_service import InterventionExecutionService
from src.core.execution.observation_service import PaymentOutcomeObservationService
from src.core.execution.simulator import SimulatorInterventionExecutor, SimulatorMode
from src.infrastructure.execution_repository import ExecutionRepository
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


async def create_stage6_decision(
    inter_repo: InterventionRepository,
    payment_id: str,
    decision_type: DecisionType = DecisionType.ACT,
    route: InterventionRouteKey = InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
    gate_verdict: GateVerdict = GateVerdict.PASSED,
    decided_at: datetime = None,
) -> InterventionDecision:
    now = decided_at or datetime.now(timezone.utc)
    decision = InterventionDecision(
        decision_id=uuid.uuid4(),
        payment_attempt_id=payment_id,
        decision_version=1,
        decided_at=now,
        decision_type=decision_type,
        selected_route_id=route if decision_type == DecisionType.ACT else None,
        failure_probability=0.85,
        stage5_prediction_id=uuid.uuid4(),
        diagnosis_confidence="STRONG",
        decision_confidence=0.9,
        gate_verdict=gate_verdict,
        policy_id="test_policy",
        policy_version="1.0.0",
        input_fingerprint=f"fp_{payment_id}",
        evaluation_audit_payload={},
        created_at=now,
    )
    await inter_repo.append_decision(decision)
    return decision


@pytest.mark.asyncio
async def test_full_lifecycle_act_execution_and_captured_observation(db_session: AsyncSession):
    """
    Stage 6 ACT -> Stage 7 Command PENDING -> Executed SUCCEEDED -> payment.captured ingested -> Outcome CAPTURED.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)

    payment_id = "pay_lc_1"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    # 1. Ingest authorized event and create Stage 6 ACT decision
    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_stage6_decision(inter_repo, payment_id, decided_at=T0)

    # 2. Create command
    cmd = await exec_service.create_command(decision, expires_in_seconds=300, t_create=T0)
    assert cmd.command_status == CommandStatus.PENDING
    assert cmd.payment_attempt_id == payment_id
    assert cmd.decision_id == decision.decision_id
    assert cmd.route_key == InterventionRouteKey.RETRY_SECONDARY_GATEWAY

    # 3. Execute command
    T_exec = T0 + timedelta(seconds=1)
    executed_cmd = await exec_service.execute_command(cmd.command_id, t_claim=T_exec)
    assert executed_cmd.command_status == CommandStatus.SUCCEEDED

    # Verify execution attempt was recorded
    attempts = await exec_repo.get_execution_attempts(cmd.command_id)
    assert len(attempts) == 1
    assert attempts[0].execution_status == ExecutionResultStatus.SUCCESS
    assert attempts[0].is_simulation is True
    assert attempts[0].executor_name == "SIMULATOR_V1"

    # 4. Ingest terminal captured event at T + 2s
    T_cap = T0 + timedelta(seconds=2)
    add_payment_event(db_session, payment_id, "payment.captured", T_cap)
    await db_session.flush()

    # 5. Observe outcome
    T_obs = T0 + timedelta(seconds=5)
    obs = await obs_service.observe(cmd.command_id, as_of_timestamp=T_obs)
    assert obs.payment_outcome == PaymentOutcome.CAPTURED
    assert obs.terminal_event_type == "payment.captured"
    assert obs.observation_version == 1
    assert obs.observation_audit_payload["conflict_detected"] is False


@pytest.mark.asyncio
async def test_lifecycle_execution_succeeded_but_payment_failed(db_session: AsyncSession):
    """
    CRITICAL INVARIANT: Execution Result != Payment Outcome.
    Intervention execution succeeded, but canonical outcome is payment.failed.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)

    payment_id = "pay_lc_fail"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_stage6_decision(inter_repo, payment_id, decided_at=T0)

    cmd = await exec_service.create_command(decision, t_create=T0)
    executed_cmd = await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))
    assert executed_cmd.command_status == CommandStatus.SUCCEEDED

    # Payment failed
    T_fail = T0 + timedelta(seconds=3)
    add_payment_event(db_session, payment_id, "payment.failed", T_fail)
    await db_session.flush()

    obs = await obs_service.observe(cmd.command_id, as_of_timestamp=T_fail + timedelta(seconds=1))
    assert obs.payment_outcome == PaymentOutcome.FAILED
    assert obs.terminal_event_type == "payment.failed"


@pytest.mark.asyncio
async def test_lifecycle_execution_failed_but_payment_captured(db_session: AsyncSession):
    """
    CRITICAL INVARIANT: Execution Result != Payment Outcome.
    Intervention execution failed (e.g. gateway switch rejected), but user still succeeded in capturing payment.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    failing_executor = SimulatorInterventionExecutor(mode=SimulatorMode.ALWAYS_FAIL)
    exec_service = InterventionExecutionService(db_session, exec_repo, executor=failing_executor)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)

    payment_id = "pay_lc_exec_fail_pay_succ"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_stage6_decision(inter_repo, payment_id, decided_at=T0)

    cmd = await exec_service.create_command(decision, t_create=T0)
    executed_cmd = await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))
    assert executed_cmd.command_status == CommandStatus.FAILED
    assert executed_cmd.status_reason is not None

    # Payment captured later
    T_cap = T0 + timedelta(seconds=5)
    add_payment_event(db_session, payment_id, "payment.captured", T_cap)
    await db_session.flush()

    obs = await obs_service.observe(cmd.command_id, as_of_timestamp=T_cap + timedelta(seconds=1))
    assert obs.payment_outcome == PaymentOutcome.CAPTURED


@pytest.mark.asyncio
async def test_lifecycle_execution_succeeded_payment_in_flight(db_session: AsyncSession):
    """
    No terminal event yet ingested: Outcome is strictly UNKNOWN_IN_FLIGHT.
    Never guesses or assumes success.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)

    payment_id = "pay_lc_in_flight"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_stage6_decision(inter_repo, payment_id, decided_at=T0)

    cmd = await exec_service.create_command(decision, t_create=T0)
    await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    # Observe immediately when no terminal event exists
    obs = await obs_service.observe(cmd.command_id, as_of_timestamp=T0 + timedelta(seconds=2))
    assert obs.payment_outcome == PaymentOutcome.UNKNOWN_IN_FLIGHT
    assert obs.terminal_event_id is None
    assert obs.time_to_outcome_ms is None


@pytest.mark.asyncio
async def test_rejection_of_non_act_decisions(db_session: AsyncSession):
    """
    Stage 7 strictly rejects NO_ACTION and MONITOR decisions.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    exec_service = InterventionExecutionService(db_session, exec_repo)

    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    # NO_ACTION
    dec_no_action = await create_stage6_decision(
        inter_repo, "pay_rej_1", decision_type=DecisionType.NO_ACTION, decided_at=T0
    )
    with pytest.raises(InvalidDecisionForExecutionError):
        await exec_service.create_command(dec_no_action)

    # MONITOR
    dec_monitor = await create_stage6_decision(
        inter_repo, "pay_rej_2", decision_type=DecisionType.MONITOR, decided_at=T0
    )
    with pytest.raises(InvalidDecisionForExecutionError):
        await exec_service.create_command(dec_monitor)
