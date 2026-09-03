"""
Stage 6 — Hard Safety Gates & Route Eligibility Filters.

Deterministic evaluation of hard safety constraints that can force NO_ACTION.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from src.core.domain.failure_prediction_models import FailurePrediction
from src.core.domain.intervention_models import GateEvaluationResult, GateVerdict
from src.core.intervention.policy import InterventionPolicy, RoutePolicyDefinition


def evaluate_kill_switch(policy: InterventionPolicy) -> GateEvaluationResult:
    """
    Gate 1: Emergency policy kill switch.
    """
    if policy.kill_switch_enabled:
        return GateEvaluationResult(
            passed=False,
            verdict=GateVerdict.FAILED_KILL_SWITCH,
            details={"reason": "Policy kill switch is active"},
        )
    return GateEvaluationResult(passed=True, verdict=GateVerdict.PASSED, details={})


def evaluate_amount_and_currency(amount_minor_units: int, currency: str) -> GateEvaluationResult:
    """
    Gate 2: Amount and currency validation.
    """
    if amount_minor_units <= 0:
        return GateEvaluationResult(
            passed=False,
            verdict=GateVerdict.FAILED_INVALID_AMOUNT,
            details={"amount_minor_units": amount_minor_units, "reason": "Amount must be strictly positive"},
        )
    if not currency or len(currency) != 3:
        return GateEvaluationResult(
            passed=False,
            verdict=GateVerdict.FAILED_INVALID_AMOUNT,
            details={"currency": currency, "reason": "Invalid currency code"},
        )
    return GateEvaluationResult(passed=True, verdict=GateVerdict.PASSED, details={})


def evaluate_pre_terminal_invariance(terminal_event_exists: bool) -> GateEvaluationResult:
    """
    Gate 3: Pre-terminal state invariance.
    Intervention can never occur if terminal outcome was already ingested before T_decide.
    """
    if terminal_event_exists:
        return GateEvaluationResult(
            passed=False,
            verdict=GateVerdict.FAILED_TERMINAL_OUTCOME_INGESTED,
            details={"reason": "Terminal payment outcome already ingested before decision time"},
        )
    return GateEvaluationResult(passed=True, verdict=GateVerdict.PASSED, details={})


def evaluate_stage5_prediction_validity(
    prediction: Optional[FailurePrediction],
) -> GateEvaluationResult:
    """
    Gate 4: Stage 5 prediction validity and sufficiency.
    """
    if prediction is None:
        return GateEvaluationResult(
            passed=False,
            verdict=GateVerdict.FAILED_STAGE5_UNAVAILABLE,
            details={"reason": "No Stage 5 prediction available as-of decision time"},
        )
    if prediction.prediction_status != "PREDICTED":
        return GateEvaluationResult(
            passed=False,
            verdict=GateVerdict.FAILED_STAGE5_INELIGIBLE,
            details={
                "prediction_status": prediction.prediction_status,
                "reason": "Stage 5 prediction status is not PREDICTED",
            },
        )
    if prediction.failure_probability is None:
        return GateEvaluationResult(
            passed=False,
            verdict=GateVerdict.FAILED_STAGE5_INELIGIBLE,
            details={"reason": "Stage 5 failure_probability is null"},
        )
    return GateEvaluationResult(passed=True, verdict=GateVerdict.PASSED, details={})


def evaluate_episode_and_traffic_shift(
    episode_status: Optional[str],
    rca_audit_payload: Optional[Dict[str, Any]],
) -> GateEvaluationResult:
    """
    Gate 5: Upstream episode validity and traffic shift detection.
    """
    if episode_status == "INVALIDATED":
        return GateEvaluationResult(
            passed=False,
            verdict=GateVerdict.FAILED_EPISODE_INVALIDATED,
            details={"episode_status": episode_status, "reason": "Degradation episode was invalidated"},
        )

    if rca_audit_payload:
        if rca_audit_payload.get("traffic_mix_shift", False) or rca_audit_payload.get("unreliable_diagnosis", False):
            return GateEvaluationResult(
                passed=False,
                verdict=GateVerdict.FAILED_UNRELIABLE_DIAGNOSIS,
                details={"reason": "Traffic mix shift or unreliable diagnosis flagged in RCA audit payload"},
            )

    return GateEvaluationResult(passed=True, verdict=GateVerdict.PASSED, details={})


def evaluate_route_eligibility(
    route: RoutePolicyDefinition,
    rca_classification: Optional[str],
    rca_evidence_strength: Optional[str],
    rca_candidate_dimension: Optional[str],
    rca_candidate_value: Optional[str],
    rca_candidate_matches_payment_segment: Optional[bool],
    payment_method: Optional[str],
    bank: Optional[str],
    is_on_cooldown: bool,
) -> Tuple[bool, str]:
    """
    Evaluates whether a specific permitted route is eligible for the transaction context.
    """
    if not route.enabled:
        return False, "Route is disabled in policy"

    if is_on_cooldown:
        return False, f"Route {route.route_key.value} is on cooldown for this payment attempt"

    if route.requires_diagnosis and rca_classification == "UNKNOWN":
        return False, "Route requires diagnosed root cause, but RCA classification is UNKNOWN"

    if route.requires_strong_rca:
        ev = (rca_evidence_strength or "").upper()
        if ev not in ("STRONG", "MODERATE"):
            return False, f"Route requires STRONG/MODERATE RCA evidence, got '{rca_evidence_strength}'"

    # Route structural applicability
    is_applicable = False
    
    if route.requires_diagnosis:
        if rca_classification == "SEGMENT_SPECIFIC":
            if not rca_candidate_matches_payment_segment:
                return False, "Transaction does not match diagnosed segment"
            if rca_candidate_dimension and rca_candidate_dimension not in route.target_dimensions:
                return False, f"Diagnosed dimension {rca_candidate_dimension} not supported by route {route.target_dimensions}"
            is_applicable = True
        elif rca_classification == "SYSTEMIC":
            if "GLOBAL" not in route.target_dimensions:
                return False, f"SYSTEMIC episode requires a GLOBAL route, but route targets {route.target_dimensions}"
            is_applicable = True
    else:
        if "GLOBAL" in route.target_dimensions:
            is_applicable = True
        elif "BANK" in route.target_dimensions and bank:
            is_applicable = True
        elif "PAYMENT_METHOD" in route.target_dimensions and payment_method:
            is_applicable = True

    if not is_applicable:
        return False, f"Transaction attributes do not match route target dimensions {route.target_dimensions}"

    return True, "Eligible"
