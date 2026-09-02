"""
Stage 7 — Intervention Execution Service.

Orchestrates Phase 7A:
1. Validates Stage 6 ACT decision and creates deterministic InterventionCommand
2. Enforces pre-execution temporal information barriers (expiry, pre-terminal NOT_NEEDED)
3. Atomically claims commands for execution (PENDING -> EXECUTING)
4. Dispatches to InterventionExecutor
5. Persists append-only ExecutionAttempt records
6. Finalizes authoritative command status (SUCCEEDED or FAILED)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.execution_exceptions import (
    CommandNotFoundError,
    InvalidDecisionForExecutionError,
)
from src.core.domain.execution_models import (
    CommandStatus,
    ExecutionAttempt,
    ExecutionResultStatus,
    InterventionCommand,
    make_command_id,
)
from src.core.domain.intervention_models import DecisionType, GateVerdict, InterventionDecision
from src.core.execution.executor import InterventionExecutor
from src.core.execution.simulator import SimulatorInterventionExecutor
from src.core.execution.state_machine import validate_transition
from src.infrastructure.execution_repository import ExecutionRepository


class InterventionExecutionService:
    """
    Service coordinating the lifecycle and dispatch of Stage 7 intervention commands.
    """

    def __init__(
        self,
        session: AsyncSession,
        execution_repo: ExecutionRepository,
        executor: Optional[InterventionExecutor] = None,
    ) -> None:
        self.session = session
        self.execution_repo = execution_repo
        self.executor = executor or SimulatorInterventionExecutor()

    async def create_command(
        self,
        decision: InterventionDecision,
        expires_in_seconds: int = 300,
        t_create: Optional[datetime] = None,
    ) -> InterventionCommand:
        """
        Create a deterministic InterventionCommand from a Stage 6 ACT decision.
        Idempotent: Identical decision returns existing command without re-inserting.
        """
        # 1. Enforce Stage 6 -> Stage 7 Contract
        if decision.decision_type != DecisionType.ACT:
            raise InvalidDecisionForExecutionError(
                f"Cannot create execution command for decision_type={decision.decision_type.value}. "
                f"Only ACT decisions are executable."
            )

        if decision.selected_route_id is None:
            raise InvalidDecisionForExecutionError(
                "Cannot create execution command: selected_route_id is None."
            )

        if decision.gate_verdict != GateVerdict.PASSED:
            raise InvalidDecisionForExecutionError(
                f"Cannot create execution command: gate_verdict={decision.gate_verdict.value} (must be PASSED)."
            )

        # 2. Check Idempotency by decision_id
        existing = await self.execution_repo.get_command_by_decision_id(decision.decision_id)
        if existing is not None:
            return existing

        # Check (payment_attempt_id, decision_version)
        existing_version = await self.execution_repo.get_command_by_attempt_and_version(
            decision.payment_attempt_id, decision.decision_version
        )
        if existing_version is not None:
            return existing_version

        # 3. Construct Command
        now = t_create or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        command_id = make_command_id(decision.decision_id)
        expires_at = now + timedelta(seconds=expires_in_seconds)

        command = InterventionCommand(
            command_id=command_id,
            decision_id=decision.decision_id,
            payment_attempt_id=decision.payment_attempt_id,
            decision_version=decision.decision_version,
            route_key=decision.selected_route_id,
            policy_id=decision.policy_id,
            policy_version=decision.policy_version,
            command_status=CommandStatus.PENDING,
            status_reason=None,
            idempotency_key=f"cmd_{decision.decision_id}",
            created_at=now,
            expires_at=expires_at,
            updated_at=now,
        )

        await self.execution_repo.insert_command(command)
        return command

    async def execute_command(
        self,
        command_id: uuid.UUID,
        context: Optional[Dict[str, Any]] = None,
        t_claim: Optional[datetime] = None,
    ) -> InterventionCommand:
        """
        Execute an InterventionCommand through the executor boundary.
        Thread/process concurrency safe via advisory lock + optimistic status claim.
        """
        claim_time = t_claim or datetime.now(timezone.utc)
        if claim_time.tzinfo is None:
            claim_time = claim_time.replace(tzinfo=timezone.utc)

        # 1. Acquire transaction-level advisory lock
        await self.execution_repo.acquire_command_lock(command_id)

        # 2. Fetch command
        cmd = await self.execution_repo.get_command_by_id(command_id)
        if cmd is None:
            raise CommandNotFoundError(f"InterventionCommand with id={command_id} not found.")

        # If command is already in a non-PENDING state, return existing authoritative state
        if cmd.command_status != CommandStatus.PENDING:
            return cmd

        # 3. Pre-execution Barrier A: Expiry check
        if claim_time >= cmd.expires_at:
            validate_transition(cmd.command_status, CommandStatus.EXPIRED)
            await self.execution_repo.update_command_status_atomic(
                command_id=command_id,
                expected_status=CommandStatus.PENDING,
                target_status=CommandStatus.EXPIRED,
                status_reason="COMMAND_EXPIRED_BEFORE_DISPATCH",
                updated_at=claim_time,
            )
            updated_cmd = await self.execution_repo.get_command_by_id(command_id)
            return updated_cmd or cmd

        # 4. Pre-execution Barrier B: Pre-terminal payment outcome check (NOT_NEEDED)
        terminal_exists = await self.execution_repo.check_terminal_event_exists_before(
            payment_attempt_id=cmd.payment_attempt_id,
            as_of=claim_time,
        )
        if terminal_exists:
            validate_transition(cmd.command_status, CommandStatus.NOT_NEEDED)
            await self.execution_repo.update_command_status_atomic(
                command_id=command_id,
                expected_status=CommandStatus.PENDING,
                target_status=CommandStatus.NOT_NEEDED,
                status_reason="TERMINAL_OUTCOME_ALREADY_INGESTED",
                updated_at=claim_time,
            )
            updated_cmd = await self.execution_repo.get_command_by_id(command_id)
            return updated_cmd or cmd

        # 5. Atomically claim command: PENDING -> EXECUTING
        validate_transition(cmd.command_status, CommandStatus.EXECUTING)
        claimed = await self.execution_repo.update_command_status_atomic(
            command_id=command_id,
            expected_status=CommandStatus.PENDING,
            target_status=CommandStatus.EXECUTING,
            status_reason=None,
            updated_at=claim_time,
        )
        if not claimed:
            # Another concurrent worker already updated or claimed the command
            updated_cmd = await self.execution_repo.get_command_by_id(command_id)
            return updated_cmd or cmd

        # Re-fetch command in EXECUTING state
        cmd = await self.execution_repo.get_command_by_id(command_id)
        assert cmd is not None

        # 6. Dispatch to Executor (No NOT_NEEDED or EXPIRED after this point)
        attempt_start = claim_time
        result = await self.executor.execute(cmd, context or {})
        attempt_end = attempt_start + timedelta(milliseconds=result.duration_ms)

        # 7. Record append-only ExecutionAttempt
        attempt_num = (await self.execution_repo.get_max_attempt_number(command_id)) + 1
        attempt = ExecutionAttempt(
            attempt_id=uuid.uuid4(),
            command_id=command_id,
            attempt_number=attempt_num,
            started_at=attempt_start,
            completed_at=attempt_end,
            duration_ms=result.duration_ms,
            execution_status=result.status,
            executor_name=result.executor_name,
            is_simulation=result.is_simulation,
            provider_action_type=result.provider_action_type,
            provider_response_code=result.provider_response_code,
            provider_response_payload=result.provider_response_payload,
            error_message=result.error_message,
            created_at=attempt_end,
        )
        await self.execution_repo.insert_execution_attempt(attempt)

        # 8. Authoritatively finalize command status
        if result.status == ExecutionResultStatus.SUCCESS:
            validate_transition(CommandStatus.EXECUTING, CommandStatus.SUCCEEDED)
            await self.execution_repo.update_command_status_atomic(
                command_id=command_id,
                expected_status=CommandStatus.EXECUTING,
                target_status=CommandStatus.SUCCEEDED,
                status_reason=None,
                updated_at=attempt_end,
            )
        else:
            # FAILURE or TIMEOUT
            # If executor timed out, status_reason is explicitly EXECUTOR_TIMEOUT
            reason = (
                "EXECUTOR_TIMEOUT"
                if result.status == ExecutionResultStatus.TIMEOUT
                else (result.error_message or "EXECUTOR_FAILURE")
            )
            validate_transition(CommandStatus.EXECUTING, CommandStatus.FAILED)
            await self.execution_repo.update_command_status_atomic(
                command_id=command_id,
                expected_status=CommandStatus.EXECUTING,
                target_status=CommandStatus.FAILED,
                status_reason=reason,
                updated_at=attempt_end,
            )

        updated_cmd = await self.execution_repo.get_command_by_id(command_id)
        assert updated_cmd is not None
        return updated_cmd
