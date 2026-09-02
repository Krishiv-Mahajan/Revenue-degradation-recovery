"""
Pure mathematical calculator for Stage 8 Counterfactual Attribution.

Enforces:
- Strict integer minor-unit money arithmetic
- Fixed-point Decimal probability and confidence arithmetic (NUMERIC(6, 4))
- Strict ROUND_HALF_EVEN rounding
- Invariant: 0 <= attributed_protected_gmv <= counterfactual_loss_exposure <= payment_amount
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Dict, Optional

from src.core.domain.attribution_models import (
    AttributionStatus,
    ModelProvenance,
    TreatmentStatus,
)

METHODOLOGY_NAME = "COUNTERFACTUAL_RISK_WEIGHTED_V1"
METHODOLOGY_VERSION = "1.0.0"

FOUR_PLACES = Decimal("0.0001")
CONFIDENCE_FLOOR = Decimal("0.2500")

# Provenance evidence discounts
PROVENANCE_WEIGHTS = {
    ModelProvenance.EMPIRICAL_PRODUCTION: Decimal("1.0000"),
    ModelProvenance.SYNTHETIC_DEVELOPMENT: Decimal("0.6000"),
    ModelProvenance.UNKNOWN: Decimal("0.3000"),
}

# RCA Diagnosis evidence weights
DIAGNOSIS_WEIGHTS = {
    "STRONG": Decimal("1.0000"),
    "MODERATE": Decimal("0.8500"),
    "WEAK": Decimal("0.7000"),
    "NONE": Decimal("0.6000"),
}


def compute_timing_confidence(delta_seconds: Optional[float]) -> Decimal:
    """
    Deterministic timing proximity evidence factor.
    delta_seconds = t_terminal_event - t_exec_completed
    """
    if delta_seconds is None:
        return Decimal("0.6000")
    if delta_seconds <= 60.0:
        return Decimal("1.0000")
    elif delta_seconds <= 300.0:
        return Decimal("0.8500")
    else:
        return Decimal("0.6000")


@dataclass(frozen=True)
class AttributionCalculationResult:
    attribution_status: AttributionStatus
    counterfactual_loss_exposure_minor_units: int
    counterfactual_natural_success_gmv_minor_units: int
    attributed_protected_gmv_minor_units: int
    attribution_confidence: Decimal
    audit_details: Dict[str, Any]


def calculate_attribution(
    payment_amount_minor_units: int,
    p0: Optional[Decimal],
    treatment_status: TreatmentStatus,
    observed_payment_outcome: str,
    provenance: ModelProvenance,
    prediction_status: str,
    diagnosis_confidence: Optional[str],
    timing_delta_seconds: Optional[float],
) -> AttributionCalculationResult:
    """
    Calculate risk-weighted counterfactual attribution under explicit methodology assumptions.
    """
    normalized_outcome = observed_payment_outcome.upper()
    is_captured = normalized_outcome in ("CAPTURED", "PAYMENT.CAPTURED")
    is_failed = normalized_outcome in ("FAILED", "PAYMENT.FAILED")
    is_in_flight = normalized_outcome in ("UNKNOWN_IN_FLIGHT", "IN_FLIGHT")

    # 1. Evaluate Terminal / Flight Gate
    if is_in_flight:
        return AttributionCalculationResult(
            attribution_status=AttributionStatus.UNRESOLVED_IN_FLIGHT,
            counterfactual_loss_exposure_minor_units=0,
            counterfactual_natural_success_gmv_minor_units=0,
            attributed_protected_gmv_minor_units=0,
            attribution_confidence=Decimal("0.0000"),
            audit_details={"reason": "PAYMENT_STILL_IN_FLIGHT"},
        )

    # 2. Evaluate Treatment Gate
    if treatment_status == TreatmentStatus.UNTREATED:
        return AttributionCalculationResult(
            attribution_status=AttributionStatus.UNATTRIBUTED_UNTREATED,
            counterfactual_loss_exposure_minor_units=0,
            counterfactual_natural_success_gmv_minor_units=0,
            attributed_protected_gmv_minor_units=0,
            attribution_confidence=Decimal("0.0000"),
            audit_details={"reason": "TRANSACTION_WAS_NOT_TREATED"},
        )

    if treatment_status == TreatmentStatus.EXECUTION_FAILED:
        return AttributionCalculationResult(
            attribution_status=AttributionStatus.UNATTRIBUTED_EXECUTION_FAILED,
            counterfactual_loss_exposure_minor_units=0,
            counterfactual_natural_success_gmv_minor_units=0,
            attributed_protected_gmv_minor_units=0,
            attribution_confidence=Decimal("0.0000"),
            audit_details={"reason": "INTERVENTION_EXECUTION_FAILED_OR_TIMED_OUT"},
        )

    # 3. Evaluate Factual Payment Outcome Gate
    if not is_captured:
        return AttributionCalculationResult(
            attribution_status=AttributionStatus.UNATTRIBUTED_PAYMENT_FAILED,
            counterfactual_loss_exposure_minor_units=0,
            counterfactual_natural_success_gmv_minor_units=0,
            attributed_protected_gmv_minor_units=0,
            attribution_confidence=Decimal("0.0000"),
            audit_details={"reason": "PAYMENT_TERMINAL_OUTCOME_WAS_NOT_CAPTURED"},
        )

    # 4. Evaluate Stage 5 Prediction Evidence
    c_provenance = PROVENANCE_WEIGHTS.get(provenance, Decimal("0.3000"))
    c_status = Decimal("1.0000") if prediction_status.upper() == "PREDICTED" else Decimal("0.0000")
    c_prediction = (c_provenance * c_status).quantize(FOUR_PLACES, rounding=ROUND_HALF_EVEN)

    if p0 is None or c_status == Decimal("0.0000"):
        return AttributionCalculationResult(
            attribution_status=AttributionStatus.INDETERMINATE_INSUFFICIENT_EVIDENCE,
            counterfactual_loss_exposure_minor_units=0,
            counterfactual_natural_success_gmv_minor_units=0,
            attributed_protected_gmv_minor_units=0,
            attribution_confidence=Decimal("0.0000"),
            audit_details={"reason": "MISSING_OR_INVALID_STAGE5_PREDICTION"},
        )

    # 5. Evaluate Diagnosis and Timing Evidence
    diag_key = (diagnosis_confidence or "NONE").upper()
    c_diagnosis = DIAGNOSIS_WEIGHTS.get(diag_key, Decimal("0.6000"))
    c_timing = compute_timing_confidence(timing_delta_seconds)

    alpha_confidence = (c_prediction * c_diagnosis * c_timing).quantize(FOUR_PLACES, rounding=ROUND_HALF_EVEN)

    # 6. Confidence Floor Check
    if alpha_confidence < CONFIDENCE_FLOOR:
        return AttributionCalculationResult(
            attribution_status=AttributionStatus.INDETERMINATE_INSUFFICIENT_EVIDENCE,
            counterfactual_loss_exposure_minor_units=0,
            counterfactual_natural_success_gmv_minor_units=0,
            attributed_protected_gmv_minor_units=0,
            attribution_confidence=alpha_confidence,
            audit_details={
                "reason": "ATTRIBUTION_CONFIDENCE_BELOW_FLOOR",
                "alpha_confidence": str(alpha_confidence),
                "confidence_floor": str(CONFIDENCE_FLOOR),
            },
        )

    # 7. Compute Risk-Weighted Attribution
    amount_dec = Decimal(payment_amount_minor_units)
    p0_dec = p0.quantize(FOUR_PLACES, rounding=ROUND_HALF_EVEN)

    raw_exposure = amount_dec * p0_dec
    loss_exposure = int(raw_exposure.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    loss_exposure = max(0, min(loss_exposure, payment_amount_minor_units))

    natural_success = max(0, payment_amount_minor_units - loss_exposure)

    raw_protected = amount_dec * p0_dec * alpha_confidence
    protected_gmv = int(raw_protected.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))

    # CRITICAL INVARIANT: 0 <= attributed_protected_gmv <= counterfactual_loss_exposure <= payment_amount
    protected_gmv = max(0, min(protected_gmv, loss_exposure, payment_amount_minor_units))

    audit_details = {
        "c_provenance": str(c_provenance),
        "c_status": str(c_status),
        "c_prediction": str(c_prediction),
        "c_diagnosis": str(c_diagnosis),
        "c_timing": str(c_timing),
        "alpha_confidence": str(alpha_confidence),
        "p0": str(p0_dec),
        "timing_delta_seconds": timing_delta_seconds,
    }

    return AttributionCalculationResult(
        attribution_status=AttributionStatus.ATTRIBUTED,
        counterfactual_loss_exposure_minor_units=loss_exposure,
        counterfactual_natural_success_gmv_minor_units=natural_success,
        attributed_protected_gmv_minor_units=protected_gmv,
        attribution_confidence=alpha_confidence,
        audit_details=audit_details,
    )
