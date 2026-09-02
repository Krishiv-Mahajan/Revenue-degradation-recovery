"""
Pure deterministic state machine transition rules for Stage 7 InterventionCommand.

Legal transitions:
- PENDING -> NOT_NEEDED  (Pre-execution only: terminal payment outcome detected before claim/dispatch)
- PENDING -> EXPIRED     (Pre-execution only: command reached expiry deadline before dispatch)
- PENDING -> EXECUTING   (Worker atomically claims command)
- EXECUTING -> SUCCEEDED (Executor returned verified success result)
- EXECUTING -> FAILED    (Executor returned error or timed out)

Strictly prohibited:
- EXECUTING -> NOT_NEEDED (Once execution starts, attempt is factual)
- EXECUTING -> EXPIRED    (In-flight execution timeout is FAILED, not EXPIRED)
- Terminal states (SUCCEEDED, FAILED, EXPIRED, NOT_NEEDED) cannot transition to any other state.
"""
from __future__ import annotations

from typing import Dict, Set

from src.core.domain.execution_exceptions import IllegalStateTransitionError
from src.core.domain.execution_models import CommandStatus

LEGAL_TRANSITIONS: Dict[CommandStatus, Set[CommandStatus]] = {
    CommandStatus.PENDING: {
        CommandStatus.EXECUTING,
        CommandStatus.NOT_NEEDED,
        CommandStatus.EXPIRED,
    },
    CommandStatus.EXECUTING: {
        CommandStatus.SUCCEEDED,
        CommandStatus.FAILED,
    },
    CommandStatus.SUCCEEDED: set(),
    CommandStatus.FAILED: set(),
    CommandStatus.EXPIRED: set(),
    CommandStatus.NOT_NEEDED: set(),
}


def can_transition(current_status: CommandStatus, target_status: CommandStatus) -> bool:
    """
    Returns True if target_status is a legal transition from current_status.
    """
    return target_status in LEGAL_TRANSITIONS.get(current_status, set())


def validate_transition(current_status: CommandStatus, target_status: CommandStatus) -> None:
    """
    Validates that target_status is a legal transition from current_status.
    Raises IllegalStateTransitionError if illegal.
    """
    if current_status == target_status:
        return

    allowed = LEGAL_TRANSITIONS.get(current_status, set())
    if target_status not in allowed:
        raise IllegalStateTransitionError(
            f"Illegal state transition from {current_status.value} to {target_status.value}. "
            f"Allowed transitions from {current_status.value}: {[s.value for s in allowed]}"
        )
