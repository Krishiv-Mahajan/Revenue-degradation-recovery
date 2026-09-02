"""
Unit tests for Stage 7 InterventionCommand creation and validation.
"""
import uuid
from datetime import datetime, timezone
import pytest

from src.core.domain.execution_exceptions import InvalidDecisionForExecutionError
from src.core.domain.execution_models import (
    CommandStatus,
    make_command_id,
    make_observation_id,
)
from src.core.domain.intervention_models import (
    DecisionType,
    GateVerdict,
    InterventionDecision,
    InterventionRouteKey,
)


def _make_sample_decision(
    decision_type: DecisionType = DecisionType.ACT,
    route: InterventionRouteKey = InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
    verdict: GateVerdict = GateVerdict.PASSED,
) -> InterventionDecision:
    now = datetime.now(timezone.utc)
    return InterventionDecision(
        decision_id=uuid.uuid4(),
        payment_attempt_id="pay_sample_123",
        decision_version=1,
        decided_at=now,
        decision_type=decision_type,
        selected_route_id=route if decision_type == DecisionType.ACT else None,
        failure_probability=0.85,
        stage5_prediction_id=uuid.uuid4(),
        diagnosis_confidence="STRONG",
        decision_confidence=0.9,
        gate_verdict=verdict,
        policy_id="test_policy",
        policy_version="1.0.0",
        input_fingerprint="fp123",
        evaluation_audit_payload={},
        created_at=now,
    )


def test_deterministic_command_id():
    """Deterministic command_id derives 1-to-1 from decision_id."""
    dec_id = uuid.uuid4()
    cmd_id_1 = make_command_id(dec_id)
    cmd_id_2 = make_command_id(dec_id)
    assert cmd_id_1 == cmd_id_2

    diff_dec_id = uuid.uuid4()
    assert make_command_id(diff_dec_id) != cmd_id_1


def test_deterministic_observation_id():
    """Deterministic observation_id derives from command_id and observation_version."""
    cmd_id = uuid.uuid4()
    obs_id_1 = make_observation_id(cmd_id, 1)
    obs_id_2 = make_observation_id(cmd_id, 1)
    obs_id_v2 = make_observation_id(cmd_id, 2)

    assert obs_id_1 == obs_id_2
    assert obs_id_1 != obs_id_v2
