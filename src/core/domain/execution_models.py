"""
Stage 7 — Intervention Execution + Outcome Observation domain models.

Separates:
1. Command (identity/payload immutable; command_status, status_reason, updated_at mutable).
2. ExecutionAttempt (append-only factual record of intervention dispatch).
3. PaymentOutcomeObservation (append-only factual observation of payment lifecycle outcome).

Strictly enforces: Execution Result != Payment Outcome.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from src.core.domain.intervention_models import InterventionRouteKey

# Deterministic namespaces for Stage 7 UUID5 generation
NAMESPACE_STAGE7_COMMAND: uuid.UUID = uuid.UUID("f7a8b9c0-d1e2-3456-7890-abcdef123456")
NAMESPACE_STAGE7_OBSERVATION: uuid.UUID = uuid.UUID("a1b2c3d4-e5f6-7890-1234-567890abcdef")


class CommandStatus(str, Enum):
    """
    Lifecycle status of an InterventionCommand.
    """
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    NOT_NEEDED = "NOT_NEEDED"


class ExecutionResultStatus(str, Enum):
    """
    Status of an individual intervention execution attempt.
    """
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    TIMEOUT = "TIMEOUT"


class PaymentOutcome(str, Enum):
    """
    Canonical observed payment outcome from Stage 1 events.
    Never inferred from execution success.
    """
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    UNKNOWN_IN_FLIGHT = "UNKNOWN_IN_FLIGHT"


@dataclass(frozen=True)
class InterventionCommand:
    """
    Domain representation of an intent to execute a Stage 6 intervention.

    The command's identity/payload is immutable; command_status, status_reason,
    and updated_at are the authoritative mutable lifecycle fields.
    Execution attempts and payment outcome observations remain append-only factual records.
    """
    command_id: uuid.UUID
    decision_id: uuid.UUID
    payment_attempt_id: str
    decision_version: int
    route_key: InterventionRouteKey
    policy_id: str
    policy_version: str
    command_status: CommandStatus
    status_reason: Optional[str]
    idempotency_key: str
    created_at: datetime
    expires_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ExecutionAttempt:
    """
    Append-only factual record of an intervention dispatch attempt.
    """
    attempt_id: uuid.UUID
    command_id: uuid.UUID
    attempt_number: int
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    execution_status: ExecutionResultStatus
    executor_name: str
    is_simulation: bool
    provider_action_type: str
    provider_response_code: Optional[str]
    provider_response_payload: dict[str, Any]
    error_message: Optional[str]
    created_at: datetime


@dataclass(frozen=True)
class PaymentOutcomeObservation:
    """
    Append-only factual observation of the payment attempt's canonical lifecycle outcome.
    Never inferred from execution success.
    """
    observation_id: uuid.UUID
    command_id: uuid.UUID
    payment_attempt_id: str
    observation_version: int
    observed_at: datetime
    as_of_timestamp: datetime
    payment_outcome: PaymentOutcome
    terminal_event_id: Optional[uuid.UUID]
    terminal_event_type: Optional[str]
    terminal_event_timestamp: Optional[datetime]
    time_to_outcome_ms: Optional[int]
    observation_audit_payload: dict[str, Any]
    created_at: datetime


def make_command_id(decision_id: uuid.UUID) -> uuid.UUID:
    """
    Deterministic UUID5 for an intervention command derived 1-to-1 from decision_id.
    """
    return uuid.uuid5(NAMESPACE_STAGE7_COMMAND, f"cmd:{decision_id}")


def make_observation_id(command_id: uuid.UUID, observation_version: int) -> uuid.UUID:
    """
    Deterministic UUID5 for a payment outcome observation derived from command_id and observation_version.
    """
    return uuid.uuid5(NAMESPACE_STAGE7_OBSERVATION, f"obs:{command_id}:{observation_version}")
