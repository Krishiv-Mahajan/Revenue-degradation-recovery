"""
Stage 7 — Intervention Execution and Outcome Observation Repository.

Manages persistence for:
1. intervention_commands (Authoritative lifecycle state)
2. intervention_execution_attempts (Append-only)
3. payment_outcome_observations (Append-only)
"""
from __future__ import annotations

import struct
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.execution_models import (
    CommandStatus,
    ExecutionAttempt,
    ExecutionResultStatus,
    InterventionCommand,
    PaymentOutcome,
    PaymentOutcomeObservation,
)
from src.core.domain.intervention_models import InterventionRouteKey
from src.infrastructure.models import (
    InterventionCommandModel,
    InterventionExecutionAttemptModel,
    PaymentEventModel,
    PaymentOutcomeObservationModel,
)


class ExecutionRepository:
    """
    Repository for Stage 7 commands, execution attempts, and observations.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def acquire_command_lock(self, command_id: uuid.UUID) -> None:
        """
        Acquire a transaction-level advisory lock per command_id
        to serialize worker execution and prevent duplicate execution races.
        """
        k1, k2 = struct.unpack("<ii", command_id.bytes[:8])
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:k1, :k2)"), {"k1": k1, "k2": k2}
        )

    async def get_command_by_id(self, command_id: uuid.UUID) -> Optional[InterventionCommand]:
        """
        Retrieve command by its primary key.
        """
        stmt = select(InterventionCommandModel).where(
            InterventionCommandModel.command_id == command_id
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._map_command(model)

    async def get_command_by_decision_id(self, decision_id: uuid.UUID) -> Optional[InterventionCommand]:
        """
        Retrieve command by its Stage 6 decision_id.
        """
        stmt = select(InterventionCommandModel).where(
            InterventionCommandModel.decision_id == decision_id
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._map_command(model)

    async def get_command_by_attempt_and_version(
        self, payment_attempt_id: str, decision_version: int
    ) -> Optional[InterventionCommand]:
        """
        Retrieve command by (payment_attempt_id, decision_version).
        """
        stmt = select(InterventionCommandModel).where(
            InterventionCommandModel.payment_attempt_id == payment_attempt_id,
            InterventionCommandModel.decision_version == decision_version,
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._map_command(model)

    async def insert_command(self, command: InterventionCommand) -> None:
        """
        Persist a new InterventionCommand with initial PENDING status.
        """
        model = InterventionCommandModel(
            command_id=command.command_id,
            decision_id=command.decision_id,
            payment_attempt_id=command.payment_attempt_id,
            decision_version=command.decision_version,
            route_key=command.route_key.value,
            policy_id=command.policy_id,
            policy_version=command.policy_version,
            command_status=command.command_status.value,
            status_reason=command.status_reason,
            idempotency_key=command.idempotency_key,
            created_at=command.created_at,
            expires_at=command.expires_at,
            updated_at=command.updated_at,
        )
        self.session.add(model)
        await self.session.flush()

    async def update_command_status_atomic(
        self,
        command_id: uuid.UUID,
        expected_status: CommandStatus,
        target_status: CommandStatus,
        status_reason: Optional[str] = None,
        updated_at: Optional[datetime] = None,
    ) -> bool:
        """
        Optimistically update command status from expected_status to target_status.
        Returns True if exactly 1 row was updated, False if current status was not expected_status.
        """
        t_update = updated_at or datetime.now(timezone.utc)
        stmt = (
            update(InterventionCommandModel)
            .where(
                InterventionCommandModel.command_id == command_id,
                InterventionCommandModel.command_status == expected_status.value,
            )
            .values(
                command_status=target_status.value,
                status_reason=status_reason,
                updated_at=t_update,
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount == 1

    async def set_command_status(
        self,
        command_id: uuid.UUID,
        target_status: CommandStatus,
        status_reason: Optional[str] = None,
        updated_at: Optional[datetime] = None,
    ) -> None:
        """
        Set command status to target_status.
        """
        t_update = updated_at or datetime.now(timezone.utc)
        stmt = (
            update(InterventionCommandModel)
            .where(InterventionCommandModel.command_id == command_id)
            .values(
                command_status=target_status.value,
                status_reason=status_reason,
                updated_at=t_update,
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def get_max_attempt_number(self, command_id: uuid.UUID) -> int:
        """
        Returns the current maximum attempt_number for a command. Returns 0 if none exist.
        """
        stmt = select(func.max(InterventionExecutionAttemptModel.attempt_number)).where(
            InterventionExecutionAttemptModel.command_id == command_id
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def insert_execution_attempt(self, attempt: ExecutionAttempt) -> None:
        """
        Append a new execution attempt record. (Append-only).
        """
        model = InterventionExecutionAttemptModel(
            attempt_id=attempt.attempt_id,
            command_id=attempt.command_id,
            attempt_number=attempt.attempt_number,
            started_at=attempt.started_at,
            completed_at=attempt.completed_at,
            duration_ms=attempt.duration_ms,
            execution_status=attempt.execution_status.value,
            executor_name=attempt.executor_name,
            is_simulation=attempt.is_simulation,
            provider_action_type=attempt.provider_action_type,
            provider_response_code=attempt.provider_response_code,
            provider_response_payload=attempt.provider_response_payload,
            error_message=attempt.error_message,
            created_at=attempt.created_at,
        )
        self.session.add(model)
        await self.session.flush()

    async def get_execution_attempts(self, command_id: uuid.UUID) -> List[ExecutionAttempt]:
        """
        Retrieve all execution attempts for a command ordered by attempt_number.
        """
        stmt = (
            select(InterventionExecutionAttemptModel)
            .where(InterventionExecutionAttemptModel.command_id == command_id)
            .order_by(InterventionExecutionAttemptModel.attempt_number.asc())
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()
        return [
            ExecutionAttempt(
                attempt_id=m.attempt_id,
                command_id=m.command_id,
                attempt_number=m.attempt_number,
                started_at=m.started_at,
                completed_at=m.completed_at,
                duration_ms=m.duration_ms,
                execution_status=ExecutionResultStatus(m.execution_status),
                executor_name=m.executor_name,
                is_simulation=m.is_simulation,
                provider_action_type=m.provider_action_type,
                provider_response_code=m.provider_response_code,
                provider_response_payload=m.provider_response_payload,
                error_message=m.error_message,
                created_at=m.created_at,
            )
            for m in models
        ]

    async def check_terminal_event_exists_before(
        self, payment_attempt_id: str, as_of: datetime
    ) -> bool:
        """
        Check if a terminal event (payment.captured or payment.failed)
        was ingested strictly before or at as_of timestamp.
        """
        stmt = text("""
            SELECT EXISTS (
                SELECT 1
                FROM payment_events
                WHERE payment_id = :payment_id
                  AND event_type IN ('payment.captured', 'payment.failed')
                  AND ingested_at <= :as_of
            )
        """)
        result = await self.session.execute(stmt, {"payment_id": payment_attempt_id, "as_of": as_of})
        return bool(result.scalar())

    async def get_canonical_terminal_events(
        self, payment_attempt_id: str, as_of: datetime
    ) -> List[PaymentEventModel]:
        """
        Retrieve all canonical terminal payment events ingested as-of as_of timestamp,
        ordered by ingested_at ASC.
        """
        stmt = (
            select(PaymentEventModel)
            .where(
                PaymentEventModel.payment_id == payment_attempt_id,
                PaymentEventModel.event_type.in_(["payment.captured", "payment.failed"]),
                PaymentEventModel.ingested_at <= as_of,
            )
            .order_by(PaymentEventModel.ingested_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_max_observation_version(self, command_id: uuid.UUID) -> int:
        """
        Returns current maximum observation_version for a command. Returns 0 if none exist.
        """
        stmt = select(func.max(PaymentOutcomeObservationModel.observation_version)).where(
            PaymentOutcomeObservationModel.command_id == command_id
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def insert_payment_outcome_observation(
        self, observation: PaymentOutcomeObservation
    ) -> None:
        """
        Append a new payment outcome observation record. (Append-only).
        """
        model = PaymentOutcomeObservationModel(
            observation_id=observation.observation_id,
            command_id=observation.command_id,
            payment_attempt_id=observation.payment_attempt_id,
            observation_version=observation.observation_version,
            observed_at=observation.observed_at,
            as_of_timestamp=observation.as_of_timestamp,
            payment_outcome=observation.payment_outcome.value,
            terminal_event_id=observation.terminal_event_id,
            terminal_event_type=observation.terminal_event_type,
            terminal_event_timestamp=observation.terminal_event_timestamp,
            time_to_outcome_ms=observation.time_to_outcome_ms,
            observation_audit_payload=observation.observation_audit_payload,
            created_at=observation.created_at,
        )
        self.session.add(model)
        await self.session.flush()

    async def get_latest_observation(
        self, command_id: uuid.UUID
    ) -> Optional[PaymentOutcomeObservation]:
        """
        Retrieve latest (highest observation_version) observation for a command.
        """
        stmt = (
            select(PaymentOutcomeObservationModel)
            .where(PaymentOutcomeObservationModel.command_id == command_id)
            .order_by(PaymentOutcomeObservationModel.observation_version.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._map_observation(model)

    async def get_all_observations(
        self, command_id: uuid.UUID
    ) -> List[PaymentOutcomeObservation]:
        """
        Retrieve all observation records for a command ordered by version ASC.
        """
        stmt = (
            select(PaymentOutcomeObservationModel)
            .where(PaymentOutcomeObservationModel.command_id == command_id)
            .order_by(PaymentOutcomeObservationModel.observation_version.asc())
        )
        result = await self.session.execute(stmt)
        return [self._map_observation(m) for m in result.scalars().all()]

    def _map_command(self, model: InterventionCommandModel) -> InterventionCommand:
        return InterventionCommand(
            command_id=model.command_id,
            decision_id=model.decision_id,
            payment_attempt_id=model.payment_attempt_id,
            decision_version=model.decision_version,
            route_key=InterventionRouteKey(model.route_key),
            policy_id=model.policy_id,
            policy_version=model.policy_version,
            command_status=CommandStatus(model.command_status),
            status_reason=model.status_reason,
            idempotency_key=model.idempotency_key,
            created_at=model.created_at,
            expires_at=model.expires_at,
            updated_at=model.updated_at,
        )

    def _map_observation(self, model: PaymentOutcomeObservationModel) -> PaymentOutcomeObservation:
        return PaymentOutcomeObservation(
            observation_id=model.observation_id,
            command_id=model.command_id,
            payment_attempt_id=model.payment_attempt_id,
            observation_version=model.observation_version,
            observed_at=model.observed_at,
            as_of_timestamp=model.as_of_timestamp,
            payment_outcome=PaymentOutcome(model.payment_outcome),
            terminal_event_id=model.terminal_event_id,
            terminal_event_type=model.terminal_event_type,
            terminal_event_timestamp=model.terminal_event_timestamp,
            time_to_outcome_ms=model.time_to_outcome_ms,
            observation_audit_payload=model.observation_audit_payload,
            created_at=model.created_at,
        )
