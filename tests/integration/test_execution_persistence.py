"""
Integration tests for Stage 7 Persistence, Append-Only Semantics, and Conflict Preservation.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.domain.execution_models import PaymentOutcome
from src.core.domain.intervention_models import (
    DecisionType,
    GateVerdict,
    InterventionDecision,
    InterventionRouteKey,
)
from src.core.execution.execution_service import InterventionExecutionService
from src.core.execution.observation_service import PaymentOutcomeObservationService
from src.infrastructure.execution_repository import ExecutionRepository
from src.infrastructure.intervention_repository import InterventionRepository
from src.infrastructure.models import (
    Base,
    PaymentEventModel,
    PaymentOutcomeObservationModel,
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
    source_event_id: str = None,
):
    model = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=source_event_id or str(uuid.uuid4()),
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
async def test_conflicting_terminal_events_audit_preservation(db_session: AsyncSession):
    """
    When conflicting terminal events exist (captured + failed for same payment attempt):
    1. Derives primary outcome using Stage 1 canonical ingestion order (ingested_at.asc()).
    2. Marks conflict_detected = True in observation_audit_payload.
    3. Preserves all conflicting event records in observation_audit_payload.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)

    payment_id = "pay_conflict_test"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    T1_fail = T0 + timedelta(seconds=2)
    T2_captured = T0 + timedelta(seconds=5)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_act_decision(inter_repo, payment_id, T0)

    cmd = await exec_service.create_command(decision, t_create=T0)
    await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    # Ingest two conflicting terminal events: failed arrived first, captured arrived second
    add_payment_event(db_session, payment_id, "payment.failed", T1_fail)
    add_payment_event(db_session, payment_id, "payment.captured", T2_captured)
    await db_session.flush()

    obs = await obs_service.observe(cmd.command_id, as_of_timestamp=T2_captured + timedelta(seconds=1))

    # First event ingested was payment.failed
    assert obs.payment_outcome == PaymentOutcome.FAILED
    assert obs.terminal_event_type == "payment.failed"

    # Verify conflict audit payload
    audit = obs.observation_audit_payload
    assert audit["conflict_detected"] is True
    assert audit["terminal_event_count"] == 2
    assert len(audit["conflicting_events"]) == 2

    # Check both event types are present in conflicting_events
    types_preserved = {ev["event_type"] for ev in audit["conflicting_events"]}
    assert types_preserved == {"payment.failed", "payment.captured"}


@pytest.mark.asyncio
async def test_append_only_observation_versioning(db_session: AsyncSession):
    """
    Multiple observation calls append new version rows; earlier rows are never updated or deleted.
    """
    inter_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)
    exec_service = InterventionExecutionService(db_session, exec_repo)
    obs_service = PaymentOutcomeObservationService(db_session, exec_repo)

    payment_id = "pay_obs_vers"
    T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    T1 = T0 + timedelta(seconds=5)
    T2_event = T0 + timedelta(seconds=10)
    T3 = T0 + timedelta(seconds=15)

    add_payment_event(db_session, payment_id, "payment.authorized", T0)
    await db_session.flush()
    decision = await create_act_decision(inter_repo, payment_id, T0)

    cmd = await exec_service.create_command(decision, t_create=T0)
    await exec_service.execute_command(cmd.command_id, t_claim=T0 + timedelta(seconds=1))

    # Observation 1 at T1
    obs1 = await obs_service.observe(cmd.command_id, as_of_timestamp=T1)
    assert obs1.observation_version == 1
    assert obs1.payment_outcome == PaymentOutcome.UNKNOWN_IN_FLIGHT

    # Event arrives at T2
    add_payment_event(db_session, payment_id, "payment.captured", T2_event)
    await db_session.flush()

    # Observation 2 at T3
    obs2 = await obs_service.observe(cmd.command_id, as_of_timestamp=T3)
    assert obs2.observation_version == 2
    assert obs2.payment_outcome == PaymentOutcome.CAPTURED

    # Verify DB has 2 distinct rows
    all_obs = await exec_repo.get_all_observations(cmd.command_id)
    assert len(all_obs) == 2
    assert all_obs[0].observation_version == 1
    assert all_obs[0].payment_outcome == PaymentOutcome.UNKNOWN_IN_FLIGHT
    assert all_obs[1].observation_version == 2
    assert all_obs[1].payment_outcome == PaymentOutcome.CAPTURED
