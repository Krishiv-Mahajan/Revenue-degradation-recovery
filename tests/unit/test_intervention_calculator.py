"""
Unit tests for Stage 6 Deterministic Decision Calculator.
"""
from decimal import Decimal

from src.core.domain.intervention_models import (
    CandidateRouteScore,
    DecisionType,
    GateVerdict,
    InterventionRouteKey,
)
from src.core.intervention.calculator import (
    calculate_act_confidence,
    calculate_monitor_confidence,
    calculate_policy_utility,
    calculate_route_alignment,
    determine_verdict,
    score_and_rank_candidates,
)
from src.core.intervention.policy import RoutePolicyDefinition, get_default_policy


def test_calculate_policy_utility_decimal():
    # 1000 INR (100,000 minor units), P(fail) = 0.80, recovery = 0.50, cost = 200 minor units
    # Gross = 100000 * 0.80 * 0.50 = 40000 minor units
    # Utility = 40000 - 200 = 39800 minor units
    benefit, utility = calculate_policy_utility(
        amount_minor_units=100000,
        failure_probability=0.80,
        recovery_rate=Decimal("0.50"),
        cost_minor_units=200,
    )
    assert benefit == Decimal("40000")
    assert utility == Decimal("39800")
    assert isinstance(benefit, Decimal)
    assert isinstance(utility, Decimal)


def test_calculate_monitor_confidence_bounds():
    # Verify strict mathematical bounds [0.0, 1.0] across wide range including extremes
    mon_thresh = 0.35
    act_thresh = 0.70

    for prob in [0.0, 0.10, 0.35, 0.525, 0.70, 0.90, 1.0]:
        conf = calculate_monitor_confidence(prob, mon_thresh, act_thresh)
        assert 0.0 <= conf <= 1.0, f"Confidence {conf} out of bounds for prob {prob}"

    # Peaks at midpoint (0.525)
    midpoint_conf = calculate_monitor_confidence(0.525, mon_thresh, act_thresh)
    assert midpoint_conf == 1.0


def test_calculate_act_confidence_bounds():
    for prob in [0.70, 0.80, 0.95, 1.0]:
        for ev in ["STRONG", "MODERATE", "WEAK", None]:
            for align in [0.5, 0.75, 1.0]:
                conf = calculate_act_confidence(prob, 0.70, ev, align)
                assert 0.0 <= conf <= 1.0


def test_route_alignment_calculation():
    route_bank = RoutePolicyDefinition(
        route_key=InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
        target_dimensions=("BANK",),
        policy_recovery_rate=Decimal("0.55"),
        policy_cost_minor_units=150,
        cooldown_seconds=300,
        requires_diagnosis=True,
        requires_strong_rca=False,
    )

    # Diagnosed bank matches payment bank
    score_match = calculate_route_alignment(
        route=route_bank,
        rca_candidate_dimension="BANK",
        rca_candidate_value="HDFC",
        payment_method="card",
        bank="HDFC",
        wallet=None,
    )
    assert score_match == 1.0

    # General target
    score_gen = calculate_route_alignment(
        route=route_bank,
        rca_candidate_dimension="PAYMENT_METHOD",
        rca_candidate_value="upi",
        payment_method="card",
        bank="HDFC",
        wallet=None,
    )
    assert score_gen == 0.75


def test_score_and_rank_candidates_deterministic_tie_breaking():
    cand1 = CandidateRouteScore(
        route_key=InterventionRouteKey.DYNAMIC_RETRY_BACKOFF,
        is_eligible=True,
        eligibility_reason="Eligible",
        expected_recovery_benefit=Decimal("500"),
        policy_cost_minor_units=100,
        policy_utility=Decimal("400"),
        route_alignment_score=0.7,
        rank=0,
    )
    cand2 = CandidateRouteScore(
        route_key=InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
        is_eligible=True,
        eligibility_reason="Eligible",
        expected_recovery_benefit=Decimal("600"),
        policy_cost_minor_units=100,
        policy_utility=Decimal("500"),  # Higher utility
        route_alignment_score=0.8,
        rank=0,
    )
    cand3 = CandidateRouteScore(
        route_key=InterventionRouteKey.FALLBACK_PAYMENT_LINK,
        is_eligible=True,
        eligibility_reason="Eligible",
        expected_recovery_benefit=Decimal("450"),
        policy_cost_minor_units=50,
        policy_utility=Decimal("400"),  # Equal utility to cand1, but lower cost (50 vs 100)
        route_alignment_score=0.7,
        rank=0,
    )

    ranked = score_and_rank_candidates([cand1, cand2, cand3])

    assert ranked[0].route_key == InterventionRouteKey.RETRY_SECONDARY_GATEWAY
    assert ranked[0].rank == 1

    assert ranked[1].route_key == InterventionRouteKey.FALLBACK_PAYMENT_LINK
    assert ranked[1].rank == 2

    assert ranked[2].route_key == InterventionRouteKey.DYNAMIC_RETRY_BACKOFF
    assert ranked[2].rank == 3


def test_determine_verdict_tri_state():
    policy = get_default_policy()

    best_route = CandidateRouteScore(
        route_key=InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
        is_eligible=True,
        eligibility_reason="Eligible",
        expected_recovery_benefit=Decimal("600"),
        policy_cost_minor_units=100,
        policy_utility=Decimal("500"),  # > min_policy_utility_threshold (50)
        route_alignment_score=0.8,
        rank=1,
    )

    # High risk (>= 0.70) -> ACT
    v_act, route_act, conf_act, g_act = determine_verdict(best_route, 0.85, policy, "STRONG")
    assert v_act == DecisionType.ACT
    assert route_act == InterventionRouteKey.RETRY_SECONDARY_GATEWAY
    assert g_act == GateVerdict.PASSED
    assert 0.0 <= conf_act <= 1.0

    # Moderate risk (0.35 <= P < 0.70) -> MONITOR
    v_mon, route_mon, conf_mon, g_mon = determine_verdict(best_route, 0.50, policy, "STRONG")
    assert v_mon == DecisionType.MONITOR
    assert route_mon is None
    assert 0.0 <= conf_mon <= 1.0

    # Low risk (< 0.35) -> NO_ACTION
    v_no, route_no, conf_no, g_no = determine_verdict(best_route, 0.20, policy, "STRONG")
    assert v_no == DecisionType.NO_ACTION
    assert route_no is None
    assert 0.0 <= conf_no <= 1.0

    # Negative or below-threshold utility -> Even if risk is high, falls back safely to MONITOR
    poor_route = CandidateRouteScore(
        route_key=InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS,
        is_eligible=True,
        eligibility_reason="Eligible",
        expected_recovery_benefit=Decimal("10"),
        policy_cost_minor_units=500,
        policy_utility=Decimal("-490"),
        route_alignment_score=0.5,
        rank=1,
    )
    v_fallback, route_fb, _, _ = determine_verdict(poor_route, 0.85, policy, "STRONG")
    # Because risk is elevated/high, but utility is not viable to act, it monitors
    assert v_fallback == DecisionType.MONITOR
    assert route_fb is None
