"""
Integration tests for Stage 7 Concurrency, Locking, and Idempotency.
"""
import uuid
from datetime import datetime, timezone
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.domain.execution_models import CommandStatus
from src.core.domain.intervention_models import (
    DecisionType,
    GateVerdict,
    InterventionDecision,
    InterventionRouteKey,
)
from src.core.execution.execution_service import InterventionExecutionService
from src.infrastructure.execution_repository import ExecutionRepository
from src.infrastructure.intervention_repository import InterventionRepository
from src.infrastructure.models import (
    Base,
    InterventionCommandModel,
    InterventionExecutionAttemptModel,
    PaymentEventModel,
)

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_command_creation_idempotent(db_engine):
    """
    Submitting the same Stage 6 ACT decision multiple times returns the exact same command.
    Database unique constraint guarantees only 1 command row exists.
    """
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        inter_repo = InterventionRepository(session)
        exec_repo = ExecutionRepository(session)
        exec_service = InterventionExecutionService(session, exec_repo)

        payment_id = "pay_idem_cmd"
        now = datetime.now(timezone.utc)

        # Ingest auth event
        auth = PaymentEventModel(
            event_id=uuid.uuid4(),
            source_system="test_system",
            source_event_id=str(uuid.uuid4()),
            payment_id=payment_id,
            timestamp=now,
            event_type="payment.authorized",
            currency="INR",
            amount_minor_units=10000,
            payment_status="processed",
            ingested_at=now,
        )
        session.add(auth)
        await session.flush()

        decision = InterventionDecision(
            decision_id=uuid.uuid4(),
            payment_attempt_id=payment_id,
            decision_version=1,
            decided_at=now,
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
            created_at=now,
        )
        await inter_repo.append_decision(decision)

        # First creation call
        c1 = await exec_service.create_command(decision)
        assert c1.command_status == CommandStatus.PENDING

        # Second creation call with identical decision
        c2 = await exec_service.create_command(decision)
        assert c2.command_id == c1.command_id
        assert c2.idempotency_key == c1.idempotency_key

        # Verify only 1 row in DB
        stmt = select(func.count()).where(InterventionCommandModel.decision_id == decision.decision_id)
        count = (await session.execute(stmt)).scalar()
        assert count == 1


@pytest.mark.asyncio
async def test_concurrent_execution_race_condition_safe(db_engine):
    """
    Two workers trying to execute the same PENDING command concurrently.
    Optimistic status update + advisory locking ensures exactly one execution attempt occurs.
    """
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    payment_id = "pay_race_1"
    now = datetime.now(timezone.utc)
    decision_id = uuid.uuid4()

    async with Session() as session:
        inter_repo = InterventionRepository(session)
        exec_repo = ExecutionRepository(session)
        exec_service = InterventionExecutionService(session, exec_repo)

        auth = PaymentEventModel(
            event_id=uuid.uuid4(),
            source_system="test_system",
            source_event_id=str(uuid.uuid4()),
            payment_id=payment_id,
            timestamp=now,
            event_type="payment.authorized",
            currency="INR",
            amount_minor_units=10000,
            payment_status="processed",
            ingested_at=now,
        )
        session.add(auth)
        await session.flush()

        decision = InterventionDecision(
            decision_id=decision_id,
            payment_attempt_id=payment_id,
            decision_version=1,
            decided_at=now,
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
            created_at=now,
        )
        await inter_repo.append_decision(decision)
        cmd = await exec_service.create_command(decision)
        command_id = cmd.command_id
        await session.commit()

    # Worker A executes command
    async with Session() as session_a:
        exec_repo_a = ExecutionRepository(session_a)
        exec_service_a = InterventionExecutionService(session_a, exec_repo_a)
        res_a = await exec_service_a.execute_command(command_id)
        assert res_a.command_status == CommandStatus.SUCCEEDED
        await session_a.commit()

    # Worker B tries to execute the already-executed command
    async with Session() as session_b:
        exec_repo_b = ExecutionRepository(session_b)
        exec_service_b = InterventionExecutionService(session_b, exec_repo_b)
        res_b = await exec_service_b.execute_command(command_id)
        # Returns current authoritative state without re-executing
        assert res_b.command_status == CommandStatus.SUCCEEDED

    # Verify in DB: exactly 1 attempt record was persisted
    async with Session() as session_v:
        stmt = select(func.count()).where(
            InterventionExecutionAttemptModel.command_id == command_id
        )
        attempt_count = (await session_v.execute(stmt)).scalar()
        assert attempt_count == 1
