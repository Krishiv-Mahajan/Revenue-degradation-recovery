"""
Stage 6 — Deterministic Decision Calculator.

Pure mathematical logic for:
- Policy utility computation (Decimal fixed-point / minor units)
- Confidence derivation (Stage 6 decision confidence distinct from Stage 5 failure_probability)
- Candidate scoring and deterministic tie-breaking
- Verdict determination
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from typing import List, Optional, Tuple

from src.core.domain.intervention_models import (
    CandidateRouteScore,
    DecisionType,
    GateVerdict,
    InterventionRouteKey,
)
from src.core.intervention.policy import InterventionPolicy, RoutePolicyDefinition


def calculate_policy_utility(
    amount_minor_units: int,
    failure_probability: float,
    recovery_rate: Decimal,
    cost_minor_units: int,
) -> Tuple[Decimal, Decimal]:
    """
    Computes expected recovery benefit and policy utility using Decimal fixed-point arithmetic.
    Rounds monetary expected benefit half-even to integer minor units to preserve financial invariants.

    Returns:
        (expected_recovery_benefit, policy_utility)
    """
    # Use exact string conversions to prevent float imprecision
    dec_amount = Decimal(str(int(amount_minor_units)))
    dec_prob = Decimal(str(round(float(failure_probability), 6)))

    gross = dec_prob * dec_amount * recovery_rate
    expected_benefit = gross.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    policy_utility = expected_benefit - Decimal(int(cost_minor_units))

    return expected_benefit, policy_utility


def calculate_route_alignment(
    route: RoutePolicyDefinition,
    rca_candidate_dimension: Optional[str],
    rca_candidate_value: Optional[str],
    payment_method: Optional[str],
    bank: Optional[str],
    wallet: Optional[str],
) -> float:
    """
    Computes route alignment with diagnosed root causes and payment attributes.
    Returns score in [0.0, 1.0].
    """
    # 1. Exact match with diagnosed structural root cause
    if rca_candidate_dimension and rca_candidate_value:
        dim_upper = rca_candidate_dimension.upper()
        if dim_upper == "BANK" and "BANK" in route.target_dimensions:
            if bank and bank.lower() == rca_candidate_value.lower():
                return 1.0
        if dim_upper == "PAYMENT_METHOD" and "PAYMENT_METHOD" in route.target_dimensions:
            if payment_method and payment_method.lower() == rca_candidate_value.lower():
                return 1.0

    # 2. General targeting of the transaction's method/bank
    if payment_method and "PAYMENT_METHOD" in route.target_dimensions:
        return 0.80
    if bank and "BANK" in route.target_dimensions:
        return 0.75

    # 3. Systemic / Global route alignment
    if "GLOBAL" in route.target_dimensions:
        return 0.70

    return 0.50


def calculate_act_confidence(
    failure_probability: float,
    act_risk_threshold: float,
    rca_evidence_strength: Optional[str],
    route_alignment_score: float,
) -> float:
    """
    Computes Stage 6's confidence in an ACT decision verdict.
    Weighted combination of:
    - Prediction certainty: distance above action threshold
    - RCA evidence weight
    - Route alignment score
    Returns float in [0.0, 1.0].
    """
    # Prediction certainty component [0.5, 1.0]
    if act_risk_threshold < 1.0:
        norm_dist = (failure_probability - act_risk_threshold) / (1.0 - act_risk_threshold)
        c_pred = 0.5 + 0.5 * max(0.0, min(1.0, norm_dist))
    else:
        c_pred = 1.0

    # RCA evidence strength component
    if rca_evidence_strength:
        ev_upper = rca_evidence_strength.upper()
        if ev_upper == "STRONG":
            c_rca = 1.0
        elif ev_upper == "MODERATE":
            c_rca = 0.75
        elif ev_upper == "WEAK":
            c_rca = 0.40
        else:
            c_rca = 0.50
    else:
        c_rca = 0.50

    # Route alignment component
    c_route = max(0.0, min(1.0, route_alignment_score))

    confidence = 0.40 * c_pred + 0.35 * c_rca + 0.25 * c_route
    return float(max(0.0, min(1.0, round(confidence, 4))))


def calculate_monitor_confidence(
    failure_probability: float,
    monitor_risk_threshold: float,
    act_risk_threshold: float,
) -> float:
    """
    Computes Stage 6's confidence in a MONITOR decision verdict.
    Guaranteed mathematically bounded in [0.0, 1.0].
    Represents certainty that monitoring is preferable to acting or ignoring.
    """
    span = act_risk_threshold - monitor_risk_threshold
    if span <= 0:
        return 1.0

    midpoint = monitor_risk_threshold + (span / 2.0)
    distance = abs(failure_probability - midpoint)
    max_distance = span / 2.0

    # Highest at midpoint (1.0), tapering to 0.70 at threshold edges
    ratio = distance / max_distance if max_distance > 0 else 0.0
    confidence = 1.0 - 0.30 * min(1.0, ratio)

    return float(max(0.0, min(1.0, round(confidence, 4))))


def score_and_rank_candidates(
    candidates: List[CandidateRouteScore],
) -> List[CandidateRouteScore]:
    """
    Sorts candidate routes deterministically using:
    1. policy_utility (descending)
    2. policy_cost_minor_units (ascending)
    3. route_key.value (ascending alphabetical)
    Assigns 1-based rank.
    """
    sorted_routes = sorted(
        candidates,
        key=lambda c: (-c.policy_utility, c.policy_cost_minor_units, c.route_key.value),
    )

    ranked: List[CandidateRouteScore] = []
    for idx, cand in enumerate(sorted_routes, start=1):
        ranked.append(
            CandidateRouteScore(
                route_key=cand.route_key,
                is_eligible=cand.is_eligible,
                eligibility_reason=cand.eligibility_reason,
                expected_recovery_benefit=cand.expected_recovery_benefit,
                policy_cost_minor_units=cand.policy_cost_minor_units,
                policy_utility=cand.policy_utility,
                route_alignment_score=cand.route_alignment_score,
                rank=idx,
            )
        )
    return ranked


def determine_verdict(
    best_candidate: Optional[CandidateRouteScore],
    failure_probability: Optional[float],
    policy: InterventionPolicy,
    rca_evidence_strength: Optional[str],
) -> Tuple[DecisionType, Optional[InterventionRouteKey], float, GateVerdict]:
    """
    Deterministically computes decision verdict, selected route, and decision confidence.
    """
    prob = failure_probability if failure_probability is not None else 0.0

    if best_candidate is not None and best_candidate.policy_utility >= policy.min_policy_utility_threshold:
        if prob >= policy.act_risk_threshold:
            conf = calculate_act_confidence(
                prob,
                policy.act_risk_threshold,
                rca_evidence_strength,
                best_candidate.route_alignment_score,
            )
            return DecisionType.ACT, best_candidate.route_key, conf, GateVerdict.PASSED
        elif prob >= policy.monitor_risk_threshold:
            conf = calculate_monitor_confidence(
                prob, policy.monitor_risk_threshold, policy.act_risk_threshold
            )
            return DecisionType.MONITOR, None, conf, GateVerdict.PASSED
        else:
            conf = float(max(0.0, min(1.0, round(1.0 - prob, 4))))
            return DecisionType.NO_ACTION, None, conf, GateVerdict.PASSED

    if prob >= policy.monitor_risk_threshold:
        conf = calculate_monitor_confidence(
            prob, policy.monitor_risk_threshold, policy.act_risk_threshold
        )
        return DecisionType.MONITOR, None, conf, GateVerdict.PASSED

    conf = float(max(0.0, min(1.0, round(1.0 - prob, 4))))
    return DecisionType.NO_ACTION, None, conf, GateVerdict.PASSED
