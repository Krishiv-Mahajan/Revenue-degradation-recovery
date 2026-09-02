"""
Integration tests for Stage 7 Temporal Correctness, Information Barriers, and Lifecycle Invariants.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from src.infrastructure.models import (
    Base,
    InterventionExecutionAttemptModel,
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
):
    model = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=ingested_at,
        event_type=event_type,
        currency="INR",
        amount_minor_units=50000,
        payment_status="processed" if event_type != "payment.failed" else "failed",
        payment_method="card",
        bank="HDFC",
        ingested_at=ingested_at,
    )
    session.add(model)


async def create_act_decision(
    inter_repo: InterventionRepository,
    payment_id: str,
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
        stage5_prediction_id=uuid.uuid4(),
        diagnosis_confidence="STRONG",
        decision_confidence=0.9,
        gate_verdict=GateVerdict.PASSED,
        policy_id="test_policy",
        policy_version="1.0.0",
        input_fingerprint=f"fp_{payment_id}",
        evaluation_audit_payload={},
        created_at=decided_at,
    )
    await inter_repo.append_decision(decision)
    return decision


@pytest.mark.asyncio
async def test_pre_execution_expiry_barrier(db_session: AsyncSession):
    """
    If claim_time >= expires_at before dispatch, command transitions to EXPIRED.
    Executor is NEVER invoked. Attempt count = 0.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    exec_service = InterventionExecutionService(db_session, exec_repo)

    payment_id = "pay_pre_expiry"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_act_decision(inter_repo, payment_id, T0)

    # Command created with 60s TTL (expires at T0 + 60s)
    cmd = await exec_service.create_command(decision, expires_in_seconds=60, t_create=T0)
    assert cmd.command_status == CommandStatus.PENDING

    # Worker attempts claim at T0 + 120s (past deadline)
    T_claim_late = T0 + timedelta(seconds=120)
    res_cmd = await exec_service.execute_command(cmd.command_id, t_claim=T_claim_late)

    assert res_cmd.command_status == CommandStatus.EXPIRED
    assert res_cmd.status_reason == "COMMAND_EXPIRED_BEFORE_DISPATCH"

    # Verify zero execution attempts were recorded
    attempts = await exec_repo.get_execution_attempts(cmd.command_id)
    assert len(attempts) == 0


@pytest.mark.asyncio
async def test_pre_execution_terminal_outcome_not_needed(db_session: AsyncSession):
    """
    If a terminal event arrived before claim/dispatch, command transitions to NOT_NEEDED.
    Executor is NEVER invoked. Attempt count = 0.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    exec_service = InterventionExecutionService(db_session, exec_repo)

    payment_id = "pay_pre_not_needed"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_act_decision(inter_repo, payment_id, T0)

    cmd = await exec_service.create_command(decision, expires_in_seconds=300, t_create=T0)

    # Payment terminal event arrives at T0 + 10s
    T_term = T0 + timedelta(seconds=10)
    add_payment_event(db_session, payment_id, "payment.captured", T_term)
    await db_session.flush()

    # Worker attempts claim at T0 + 20s
    T_claim = T0 + timedelta(seconds=20)
    res_cmd = await exec_service.execute_command(cmd.command_id, t_claim=T_claim)

    assert res_cmd.command_status == CommandStatus.NOT_NEEDED
    assert res_cmd.status_reason == "TERMINAL_OUTCOME_ALREADY_INGESTED"

    # Executor was not invoked
    attempts = await exec_repo.get_execution_attempts(cmd.command_id)
    assert len(attempts) == 0


@pytest.mark.asyncio
async def test_post_dispatch_terminal_event_does_not_convert_to_not_needed(db_session: AsyncSession):
    """
    CRITICAL INVARIANT: No EXECUTING -> NOT_NEEDED.
    Once execution starts, attempt is factual. A terminal event ingested during/after execution
    does NOT alter command status to NOT_NEEDED.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    exec_service = InterventionExecutionService(db_session, exec_repo)

    payment_id = "pay_post_dispatch"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_act_decision(inter_repo, payment_id, T0)

    cmd = await exec_service.create_command(decision, t_create=T0)

    # Execute at T0 + 1s (no terminal event existed at T0 + 1s)
    T_claim = T0 + timedelta(seconds=1)
    res_cmd = await exec_service.execute_command(cmd.command_id, t_claim=T_claim)
    assert res_cmd.command_status == CommandStatus.SUCCEEDED

    # Terminal event ingested at T0 + 5s (after execution)
    T_term = T0 + timedelta(seconds=5)
    add_payment_event(db_session, payment_id, "payment.captured", T_term)
    await db_session.flush()

    # Verify command is STILL SUCCEEDED, NOT NOT_NEEDED
    refetched = await exec_repo.get_command_by_id(cmd.command_id)
    assert refetched.command_status == CommandStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_executor_timeout_produces_failed_and_timeout_attempt(db_session: AsyncSession):
    """
    CRITICAL INVARIANT: In-flight timeout produces FAILED + TIMEOUT.
    It is NEVER converted into EXPIRED.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    timeout_executor = SimulatorInterventionExecutor(
        mode=SimulatorMode.SIMULATE_TIMEOUT, simulated_duration_ms=45
    )
    exec_service = InterventionExecutionService(db_session, exec_repo, executor=timeout_executor)

    payment_id = "pay_timeout_test"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_act_decision(inter_repo, payment_id, T0)

    cmd = await exec_service.create_command(decision, t_create=T0)
    res_cmd = await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    # Command is FAILED, not EXPIRED
    assert res_cmd.command_status == CommandStatus.FAILED
    assert res_cmd.status_reason == "EXECUTOR_TIMEOUT"

    # Execution attempt records TIMEOUT
    attempts = await exec_repo.get_execution_attempts(cmd.command_id)
    assert len(attempts) == 1
    assert attempts[0].execution_status == ExecutionResultStatus.TIMEOUT
    assert attempts[0].duration_ms == 45


@pytest.mark.asyncio
async def test_observation_information_barrier_and_replay(db_session: AsyncSession):
    """
    Information Barrier: Events ingested after as_of_timestamp are strictly invisible.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)

    payment_id = "pay_info_barrier"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    T1 = T0 + timedelta(seconds=10)
    T2_event = T0 + timedelta(seconds=20)
    T3 = T0 + timedelta(seconds=30)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_act_decision(inter_repo, payment_id, T0)

    cmd = await exec_service.create_command(decision, t_create=T0)
    await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    # Terminal event arrives at T2
    add_payment_event(db_session, payment_id, "payment.captured", T2_event)
    await db_session.flush()

    # Observation 1 evaluated as-of T1 (before event arrived)
    obs_t1 = await obs_service.observe(cmd.command_id, as_of_timestamp=T1)
    assert obs_t1.payment_outcome == PaymentOutcome.UNKNOWN_IN_FLIGHT
    assert obs_t1.observation_version == 1

    # Observation 2 evaluated as-of T3 (after event arrived)
    obs_t3 = await obs_service.observe(cmd.command_id, as_of_timestamp=T3)
    assert obs_t3.payment_outcome == PaymentOutcome.CAPTURED
    assert obs_t3.observation_version == 2
    assert obs_t3.terminal_event_type == "payment.captured"
