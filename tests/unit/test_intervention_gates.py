"""
Unit tests for Stage 6 Hard Safety Gates and Route Eligibility Filters.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from src.core.domain.failure_prediction_models import FailurePrediction
from src.core.domain.intervention_models import GateVerdict, InterventionRouteKey
from src.core.intervention.gates import (
    evaluate_amount_and_currency,
    evaluate_episode_and_traffic_shift,
    evaluate_kill_switch,
    evaluate_pre_terminal_invariance,
    evaluate_route_eligibility,
    evaluate_stage5_prediction_validity,
)
from src.core.intervention.policy import (
    InterventionPolicy,
    RoutePolicyDefinition,
    get_default_policy,
)


def test_gate1_kill_switch():
    policy = get_default_policy()
    res = evaluate_kill_switch(policy)
    assert res.passed is True
    assert res.verdict == GateVerdict.PASSED

    # Trigger kill switch
    killed_policy = InterventionPolicy(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        kill_switch_enabled=True,
        act_risk_threshold=policy.act_risk_threshold,
        monitor_risk_threshold=policy.monitor_risk_threshold,
        min_policy_utility_threshold=policy.min_policy_utility_threshold,
        global_rate_limit_per_minute=policy.global_rate_limit_per_minute,
        routes=policy.routes,
    )
    res_killed = evaluate_kill_switch(killed_policy)
    assert res_killed.passed is False
    assert res_killed.verdict == GateVerdict.FAILED_KILL_SWITCH


def test_gate2_amount_and_currency():
    # Valid
    res = evaluate_amount_and_currency(1000, "INR")
    assert res.passed is True

    # Zero amount
    res_zero = evaluate_amount_and_currency(0, "INR")
    assert res_zero.passed is False
    assert res_zero.verdict == GateVerdict.FAILED_INVALID_AMOUNT

    # Negative amount
    res_neg = evaluate_amount_and_currency(-500, "INR")
    assert res_neg.passed is False
    assert res_neg.verdict == GateVerdict.FAILED_INVALID_AMOUNT

    # Invalid currency code
    res_curr = evaluate_amount_and_currency(1000, "IN")
    assert res_curr.passed is False
    assert res_curr.verdict == GateVerdict.FAILED_INVALID_AMOUNT


def test_gate3_pre_terminal_invariance():
    # No terminal event ingested before T_decide
    res_clean = evaluate_pre_terminal_invariance(terminal_event_exists=False)
    assert res_clean.passed is True

    # Terminal event already ingested
    res_settled = evaluate_pre_terminal_invariance(terminal_event_exists=True)
    assert res_settled.passed is False
    assert res_settled.verdict == GateVerdict.FAILED_TERMINAL_OUTCOME_INGESTED


def test_gate4_stage5_prediction_validity():
    # Missing prediction
    res_none = evaluate_stage5_prediction_validity(None)
    assert res_none.passed is False
    assert res_none.verdict == GateVerdict.FAILED_STAGE5_UNAVAILABLE

    # Prediction with status INSUFFICIENT_DATA
    pred_insufficient = FailurePrediction(
        prediction_id=uuid.uuid4(),
        payment_attempt_id="pay_123",
        prediction_version=1,
        predicted_at=datetime.now(timezone.utc),
        prediction_horizon="30m",
        failure_probability=None,
        risk_band=None,
        prediction_status="INSUFFICIENT_DATA",
        model_name="m",
        model_version="1.0",
        feature_schema_version="1.0",
        feature_snapshot={},
        input_fingerprint="fp",
        created_at=datetime.now(timezone.utc),
    )
    res_insuf = evaluate_stage5_prediction_validity(pred_insufficient)
    assert res_insuf.passed is False
    assert res_insuf.verdict == GateVerdict.FAILED_STAGE5_INELIGIBLE

    # Prediction with status NOT_ELIGIBLE
    pred_ineligible = FailurePrediction(
        prediction_id=uuid.uuid4(),
        payment_attempt_id="pay_123",
        prediction_version=1,
        predicted_at=datetime.now(timezone.utc),
        prediction_horizon="30m",
        failure_probability=None,
        risk_band=None,
        prediction_status="NOT_ELIGIBLE",
        model_name="m",
        model_version="1.0",
        feature_schema_version="1.0",
        feature_snapshot={},
        input_fingerprint="fp",
        created_at=datetime.now(timezone.utc),
    )
    res_inel = evaluate_stage5_prediction_validity(pred_ineligible)
    assert res_inel.passed is False
    assert res_inel.verdict == GateVerdict.FAILED_STAGE5_INELIGIBLE

    # Valid PREDICTED
    pred_valid = FailurePrediction(
        prediction_id=uuid.uuid4(),
        payment_attempt_id="pay_123",
        prediction_version=1,
        predicted_at=datetime.now(timezone.utc),
        prediction_horizon="30m",
        failure_probability=0.82,
        risk_band="HIGH",
        prediction_status="PREDICTED",
        model_name="m",
        model_version="1.0",
        feature_schema_version="1.0",
        feature_snapshot={},
        input_fingerprint="fp",
        created_at=datetime.now(timezone.utc),
    )
    res_val = evaluate_stage5_prediction_validity(pred_valid)
    assert res_val.passed is True
    assert res_val.verdict == GateVerdict.PASSED


def test_gate5_episode_and_traffic_shift():
    # Active normal episode
    res = evaluate_episode_and_traffic_shift("ACTIVE", {})
    assert res.passed is True

    # Invalidated episode
    res_inv = evaluate_episode_and_traffic_shift("INVALIDATED", {})
    assert res_inv.passed is False
    assert res_inv.verdict == GateVerdict.FAILED_EPISODE_INVALIDATED

    # Traffic mix shift flagged in RCA
    res_shift = evaluate_episode_and_traffic_shift("ACTIVE", {"traffic_mix_shift": True})
    assert res_shift.passed is False
    assert res_shift.verdict == GateVerdict.FAILED_UNRELIABLE_DIAGNOSIS


def test_route_eligibility_filtering():
    route = RoutePolicyDefinition(
        route_key=InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH,
        target_dimensions=("PAYMENT_METHOD",),
        policy_recovery_rate=Decimal("0.65"),
        policy_cost_minor_units=300,
        cooldown_seconds=600,
        requires_diagnosis=True,
        requires_strong_rca=True,
        enabled=True,
    )

    # Eligible case
    eligible, reason = evaluate_route_eligibility(
        route=route,
        rca_classification="SEGMENT_SPECIFIC",
        rca_evidence_strength="STRONG",
        rca_candidate_dimension="PAYMENT_METHOD",
        rca_candidate_value="upi",
        payment_method="upi",
        bank="HDFC",
        is_on_cooldown=False,
    )
    assert eligible is True

    # Blocked by cooldown
    cd, _ = evaluate_route_eligibility(
        route=route,
        rca_classification="SEGMENT_SPECIFIC",
        rca_evidence_strength="STRONG",
        rca_candidate_dimension="PAYMENT_METHOD",
        rca_candidate_value="upi",
        payment_method="upi",
        bank="HDFC",
        is_on_cooldown=True,
    )
    assert cd is False

    # Blocked by UNKNOWN RCA classification when route requires diagnosis
    unk, _ = evaluate_route_eligibility(
        route=route,
        rca_classification="UNKNOWN",
        rca_evidence_strength=None,
        rca_candidate_dimension=None,
        rca_candidate_value=None,
        payment_method="upi",
        bank="HDFC",
        is_on_cooldown=False,
    )
    assert unk is False

    # Blocked by WEAK RCA evidence when route requires strong RCA
    weak, _ = evaluate_route_eligibility(
        route=route,
        rca_classification="SEGMENT_SPECIFIC",
        rca_evidence_strength="WEAK",
        rca_candidate_dimension="PAYMENT_METHOD",
        rca_candidate_value="upi",
        payment_method="upi",
        bank="HDFC",
        is_on_cooldown=False,
    )
    assert weak is False
