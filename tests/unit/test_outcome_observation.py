"""
Unit tests for Stage 7 payment outcome observation domain logic.
"""
import uuid
from datetime import datetime, timezone
import pytest

from src.core.domain.execution_models import (
    PaymentOutcome,
    PaymentOutcomeObservation,
    make_observation_id,
)


def test_observation_model_creation():
    cmd_id = uuid.uuid4()
    obs_id = make_observation_id(cmd_id, 1)
    now = datetime.now(timezone.utc)

    obs = PaymentOutcomeObservation(
        observation_id=obs_id,
        command_id=cmd_id,
        payment_attempt_id="pay_123",
        observation_version=1,
        observed_at=now,
        as_of_timestamp=now,
        payment_outcome=PaymentOutcome.CAPTURED,
        terminal_event_id=uuid.uuid4(),
        terminal_event_type="payment.captured",
        terminal_event_timestamp=now,
        time_to_outcome_ms=450,
        observation_audit_payload={"conflict_detected": False},
        created_at=now,
    )

    assert obs.observation_id == obs_id
    assert obs.payment_outcome == PaymentOutcome.CAPTURED
    assert obs.time_to_outcome_ms == 450
    assert obs.observation_audit_payload["conflict_detected"] is False
