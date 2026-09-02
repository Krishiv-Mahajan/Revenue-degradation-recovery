"""
Stage 7 — Payment Outcome Observation Service.

Orchestrates Phase 7B:
1. Queries Stage 1 canonical payment_events strictly as-of declared as_of_timestamp boundary
2. Evaluates canonical terminal events (payment.captured, payment.failed)
3. Maps to PaymentOutcome (CAPTURED, FAILED, UNKNOWN_IN_FLIGHT)
4. Preserves conflicting source facts in audit payload with explicit conflict_detected flag
5. Appends immutable, versioned PaymentOutcomeObservation records
"""
from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.execution_exceptions import CommandNotFoundError
from src.core.domain.execution_models import (
    PaymentOutcome,
    PaymentOutcomeObservation,
    make_observation_id,
)
from src.infrastructure.execution_repository import ExecutionRepository


class PaymentOutcomeObservationService:
    """
    Service for factual observation of canonical payment outcomes.
    Separated from intervention execution dispatch.
    """

    def __init__(
        self,
        session: AsyncSession,
        execution_repo: ExecutionRepository,
    ) -> None:
        self.session = session
        self.execution_repo = execution_repo

    async def observe(
        self,
        command_id: uuid.UUID,
        as_of_timestamp: Optional[datetime] = None,
    ) -> PaymentOutcomeObservation:
        """
        Evaluate and persist a factual payment outcome observation as-of as_of_timestamp.
        Strictly observes Stage 1 canonical facts without guessing or inferring.
        """
        as_of = as_of_timestamp or datetime.now(timezone.utc)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)

        # 1. Fetch command
        cmd = await self.execution_repo.get_command_by_id(command_id)
        if cmd is None:
            raise CommandNotFoundError(f"InterventionCommand with id={command_id} not found.")

        # 2. Query canonical terminal events strictly <= as_of
        terminal_events = await self.execution_repo.get_canonical_terminal_events(
            payment_attempt_id=cmd.payment_attempt_id,
            as_of=as_of,
        )

        now = datetime.now(timezone.utc)

        # 3. Derive PaymentOutcome from canonical facts
        if not terminal_events:
            outcome = PaymentOutcome.UNKNOWN_IN_FLIGHT
            terminal_event_id = None
            terminal_event_type = None
            terminal_event_timestamp = None
            time_to_outcome_ms = None
            audit_payload: Dict[str, Any] = {
                "terminal_event_count": 0,
                "conflict_detected": False,
                "conflicting_events": [],
            }
        else:
            first_event = terminal_events[0]
            distinct_types = {e.event_type for e in terminal_events}
            has_conflict = len(distinct_types) > 1

            if first_event.event_type == "payment.captured":
                outcome = PaymentOutcome.CAPTURED
            else:
                outcome = PaymentOutcome.FAILED

            terminal_event_id = first_event.event_id
            terminal_event_type = first_event.event_type
            terminal_event_timestamp = first_event.timestamp

            # Calculate time to outcome from command creation
            delta_ms = int((first_event.timestamp - cmd.created_at).total_seconds() * 1000)
            time_to_outcome_ms = max(0, delta_ms)

            conflicting_list = [
                {
                    "event_id": str(e.event_id),
                    "event_type": e.event_type,
                    "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                    "ingested_at": e.ingested_at.isoformat() if e.ingested_at else None,
                }
                for e in terminal_events
            ]

            audit_payload = {
                "terminal_event_count": len(terminal_events),
                "conflict_detected": has_conflict,
                "conflicting_events": conflicting_list if has_conflict else [],
                "canonical_selected_event": {
                    "event_id": str(first_event.event_id),
                    "event_type": first_event.event_type,
                    "ingested_at": first_event.ingested_at.isoformat() if first_event.ingested_at else None,
                },
            }

        # 4. Version allocation and deterministic observation ID
        max_ver = await self.execution_repo.get_max_observation_version(command_id)
        new_version = max_ver + 1
        observation_id = make_observation_id(command_id, new_version)

        observation = PaymentOutcomeObservation(
            observation_id=observation_id,
            command_id=command_id,
            payment_attempt_id=cmd.payment_attempt_id,
            observation_version=new_version,
            observed_at=now,
            as_of_timestamp=as_of,
            payment_outcome=outcome,
            terminal_event_id=terminal_event_id,
            terminal_event_type=terminal_event_type,
            terminal_event_timestamp=terminal_event_timestamp,
            time_to_outcome_ms=time_to_outcome_ms,
            observation_audit_payload=audit_payload,
            created_at=now,
        )

        await self.execution_repo.insert_payment_outcome_observation(observation)
        return observation
