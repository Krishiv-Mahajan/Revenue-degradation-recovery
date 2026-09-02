"""
Unit tests for Stage 7 state machine transition validation.
"""
import pytest

from src.core.domain.execution_exceptions import IllegalStateTransitionError
from src.core.domain.execution_models import CommandStatus
from src.core.execution.state_machine import can_transition, validate_transition


def test_legal_transitions_from_pending():
    """PENDING can legally transition to EXECUTING, NOT_NEEDED, and EXPIRED."""
    assert can_transition(CommandStatus.PENDING, CommandStatus.EXECUTING)
    assert can_transition(CommandStatus.PENDING, CommandStatus.NOT_NEEDED)
    assert can_transition(CommandStatus.PENDING, CommandStatus.EXPIRED)

    # Validations do not raise
    validate_transition(CommandStatus.PENDING, CommandStatus.EXECUTING)
    validate_transition(CommandStatus.PENDING, CommandStatus.NOT_NEEDED)
    validate_transition(CommandStatus.PENDING, CommandStatus.EXPIRED)


def test_illegal_transitions_from_pending():
    """PENDING cannot transition directly to SUCCEEDED or FAILED without executing."""
    assert not can_transition(CommandStatus.PENDING, CommandStatus.SUCCEEDED)
    assert not can_transition(CommandStatus.PENDING, CommandStatus.FAILED)

    with pytest.raises(IllegalStateTransitionError):
        validate_transition(CommandStatus.PENDING, CommandStatus.SUCCEEDED)

    with pytest.raises(IllegalStateTransitionError):
        validate_transition(CommandStatus.PENDING, CommandStatus.FAILED)


def test_legal_transitions_from_executing():
    """EXECUTING can legally transition to SUCCEEDED and FAILED."""
    assert can_transition(CommandStatus.EXECUTING, CommandStatus.SUCCEEDED)
    assert can_transition(CommandStatus.EXECUTING, CommandStatus.FAILED)

    validate_transition(CommandStatus.EXECUTING, CommandStatus.SUCCEEDED)
    validate_transition(CommandStatus.EXECUTING, CommandStatus.FAILED)


def test_strictly_prohibited_transitions_from_executing():
    """
    CRITICAL INVARIANTS:
    1. EXECUTING -> NOT_NEEDED is strictly prohibited (attempt was factual).
    2. EXECUTING -> EXPIRED is strictly prohibited (timeouts produce FAILED, not EXPIRED).
    """
    assert not can_transition(CommandStatus.EXECUTING, CommandStatus.NOT_NEEDED)
    assert not can_transition(CommandStatus.EXECUTING, CommandStatus.EXPIRED)
    assert not can_transition(CommandStatus.EXECUTING, CommandStatus.PENDING)

    with pytest.raises(IllegalStateTransitionError):
        validate_transition(CommandStatus.EXECUTING, CommandStatus.NOT_NEEDED)

    with pytest.raises(IllegalStateTransitionError):
        validate_transition(CommandStatus.EXECUTING, CommandStatus.EXPIRED)


def test_terminal_states_are_immutable():
    """Terminal states (SUCCEEDED, FAILED, EXPIRED, NOT_NEEDED) cannot transition to any other state."""
    terminal_states = [
        CommandStatus.SUCCEEDED,
        CommandStatus.FAILED,
        CommandStatus.EXPIRED,
        CommandStatus.NOT_NEEDED,
    ]
    all_states = list(CommandStatus)

    for term in terminal_states:
        for other in all_states:
            if term == other:
                # Same state is a no-op
                validate_transition(term, other)
            else:
                assert not can_transition(term, other)
                with pytest.raises(IllegalStateTransitionError):
                    validate_transition(term, other)
