"""
Stage 7 — Payment Outcome Observation Service.

Orchestrates Phase 7B:
1. Queries Stage 1 canonical payment_events strictly as-of declared as_of_timestamp boundary
2. Evaluates canonical terminal events (payment.captured, payment.failed), including verified capture
   outcomes of PayPal recovery checkout orders created by a real execution of the command; for a payment
   method switch offer, only the capture the offer lifecycle verified as its completion counts
3. Maps to PaymentOutcome (CAPTURED, FAILED, UNKNOWN_IN_FLIGHT); a captured recovery order is CAPTURED
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
from src.core.domain.intervention_models import InterventionRouteKey
from src.core.domain.payment_attempts import AttemptRef
from src.infrastructure.execution_repository import ExecutionRepository
from src.infrastructure.payment_attempt_repository import PaymentAttemptRepository


class PaymentOutcomeObservationService:
    """
    Service for factual observation of canonical payment outcomes.
    Separated from intervention execution dispatch.
    """

    def __init__(
        self,
        session: AsyncSession,
        execution_repo: ExecutionRepository,
        attempt_repository: Optional[PaymentAttemptRepository] = None,
    ) -> None:
        self.session = session
        self.execution_repo = execution_repo
        self.attempts = attempt_repository or PaymentAttemptRepository(session)

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

        await self.attempts.require(AttemptRef.for_key(cmd.source_system, cmd.payment_attempt_id))

        # 2. Query canonical terminal events strictly <= as_of
        terminal_events = await self.execution_repo.get_canonical_terminal_events(
            payment_attempt_id=cmd.payment_attempt_id,
            as_of=as_of,
            source_system=cmd.source_system,
        )

        # Recovery checkout orders created by a real (non-simulated) execution of this command, and
        # their verified capture outcomes as-of the same boundary.
        recovery_order_ids = await self.execution_repo.get_recovery_order_ids(command_id)
        recovery_events = await self.execution_repo.get_recovery_terminal_events(recovery_order_ids, as_of)
        switch_completion_id = None
        if cmd.route_key is InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH:
            # The customer must have accepted the offer in time and PayPal captured the offered amount; any
            # other capture of the order (after expiry, a decline, or for another amount) is not a completion.
            switch_completion_id = await self.execution_repo.get_switch_offer_completion_event_id(command_id, as_of)
            recovery_capture = next((e for e in recovery_events if e.event_id == switch_completion_id), None)
        else:
            recovery_capture = next((e for e in recovery_events if e.event_type == "payment.captured"), None)

        now = datetime.now(timezone.utc)

        # 3. Derive PaymentOutcome from canonical facts. A captured recovery order means the payer
        # completed the purchase through the recovery link, whatever the original attempt's outcome.
        if recovery_capture is not None:
            outcome = PaymentOutcome.CAPTURED
            terminal_event_id = recovery_capture.event_id
            terminal_event_type = recovery_capture.event_type
            terminal_event_timestamp = recovery_capture.timestamp
            time_to_outcome_ms = max(0, int((recovery_capture.timestamp - cmd.created_at).total_seconds() * 1000))
            original_captured = any(e.event_type == "payment.captured" for e in terminal_events)
            audit_payload = {
                "terminal_event_count": len(terminal_events) + len(recovery_events),
                # The original attempt was also captured: the payer may have paid twice.
                "conflict_detected": original_captured,
                "conflicting_events": _event_list(terminal_events) if original_captured else [],
                "canonical_selected_event": {
                    "event_id": str(recovery_capture.event_id),
                    "event_type": recovery_capture.event_type,
                    "ingested_at": recovery_capture.ingested_at.isoformat() if recovery_capture.ingested_at else None,
                },
            }
        elif not terminal_events:
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

            conflicting_list = _event_list(terminal_events)

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

        if recovery_order_ids:
            audit_payload["recovery"] = {
                "order_ids": recovery_order_ids,
                "recovered_via_recovery_order": recovery_capture is not None,
                "terminal_events": _event_list(recovery_events),
                "original_attempt_events": _event_list(terminal_events),
            }
            if cmd.route_key is InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH:
                audit_payload["recovery"]["verified_switch_completion_event_id"] = (
                    str(switch_completion_id) if switch_completion_id else None
                )

        # 4. Version allocation and deterministic observation ID
        max_ver = await self.execution_repo.get_max_observation_version(command_id)
        new_version = max_ver + 1
        observation_id = make_observation_id(command_id, new_version)

        observation = PaymentOutcomeObservation(
            observation_id=observation_id,
            command_id=command_id,
            payment_attempt_id=cmd.payment_attempt_id,
            source_system=cmd.source_system,
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


def _event_list(events) -> list:
    return [
        {
            "event_id": str(e.event_id),
            "event_type": e.event_type,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "ingested_at": e.ingested_at.isoformat() if e.ingested_at else None,
        }
        for e in events
    ]
