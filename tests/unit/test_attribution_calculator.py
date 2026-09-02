"""
Unit tests for Stage 8 Counterfactual Attribution Calculator.
"""
import uuid
from decimal import Decimal
import pytest

from src.core.attribution.calculator import (
    calculate_attribution,
    compute_timing_confidence,
)
from src.core.domain.attribution_models import (
    AttributionStatus,
    ModelProvenance,
    TreatmentStatus,
    make_attribution_id,
)


def test_attribution_calculator_captured_treated_success():
    """
    Test standard success scenario:
    - Amount = 50,000 paise (500 INR)
    - p0 = 0.8500
    - Model provenance = SYNTHETIC_DEVELOPMENT (weight 0.6000)
    - Prediction status = PREDICTED (weight 1.0000) -> c_prediction = 0.6000
    - Diagnosis = STRONG (weight 1.0000)
    - Timing delta = 30s <= 60s (weight 1.0000)
    - alpha_confidence = 0.6000 * 1.0000 * 1.0000 = 0.6000
    - raw_exposure = 50,000 * 0.8500 = 42,500 paise
    - raw_protected = 50,000 * 0.8500 * 0.6000 = 25,500 paise
    """
    res = calculate_attribution(
        payment_amount_minor_units=50000,
        p0=Decimal("0.8500"),
        treatment_status=TreatmentStatus.TREATED,
        observed_payment_outcome="CAPTURED",
        provenance=ModelProvenance.SYNTHETIC_DEVELOPMENT,
        prediction_status="PREDICTED",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=30.0,
    )

    assert res.attribution_status == AttributionStatus.ATTRIBUTED
    assert res.counterfactual_loss_exposure_minor_units == 42500
    assert res.counterfactual_natural_success_gmv_minor_units == 7500
    assert res.attributed_protected_gmv_minor_units == 25500
    assert res.attribution_confidence == Decimal("0.6000")

    # Invariant: 0 <= protected <= loss_exposure <= amount
    assert 0 <= res.attributed_protected_gmv_minor_units <= res.counterfactual_loss_exposure_minor_units <= 50000


def test_attribution_calculator_inequality_bounding():
    """
    Verify strictly: 0 <= attributed_protected_gmv <= counterfactual_loss_exposure <= payment_amount.
    Even with high confidence or boundary probabilities, protected GMV can NEVER exceed loss exposure.
    """
    res = calculate_attribution(
        payment_amount_minor_units=10000,
        p0=Decimal("0.7000"),
        treatment_status=TreatmentStatus.TREATED,
        observed_payment_outcome="payment.captured",
        provenance=ModelProvenance.EMPIRICAL_PRODUCTION,
        prediction_status="PREDICTED",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=10.0,
    )

    assert res.attribution_status == AttributionStatus.ATTRIBUTED
    assert res.counterfactual_loss_exposure_minor_units == 7000
    assert res.attributed_protected_gmv_minor_units == 7000
    assert res.attributed_protected_gmv_minor_units <= res.counterfactual_loss_exposure_minor_units


def test_attribution_calculator_payment_failed_zero_gmv():
    """
    If payment outcome is FAILED, protected GMV is strictly 0.
    """
    res = calculate_attribution(
        payment_amount_minor_units=50000,
        p0=Decimal("0.8500"),
        treatment_status=TreatmentStatus.TREATED,
        observed_payment_outcome="payment.failed",
        provenance=ModelProvenance.SYNTHETIC_DEVELOPMENT,
        prediction_status="PREDICTED",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=30.0,
    )

    assert res.attribution_status == AttributionStatus.UNATTRIBUTED_PAYMENT_FAILED
    assert res.attributed_protected_gmv_minor_units == 0
    assert res.counterfactual_loss_exposure_minor_units == 0


def test_attribution_calculator_in_flight_zero_gmv():
    """
    If payment outcome is UNKNOWN_IN_FLIGHT, status is UNRESOLVED_IN_FLIGHT and protected GMV is 0.
    """
    res = calculate_attribution(
        payment_amount_minor_units=50000,
        p0=Decimal("0.8500"),
        treatment_status=TreatmentStatus.TREATED,
        observed_payment_outcome="UNKNOWN_IN_FLIGHT",
        provenance=ModelProvenance.SYNTHETIC_DEVELOPMENT,
        prediction_status="PREDICTED",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=None,
    )

    assert res.attribution_status == AttributionStatus.UNRESOLVED_IN_FLIGHT
    assert res.attributed_protected_gmv_minor_units == 0


def test_attribution_calculator_untreated_zero_gmv():
    """
    If transaction was UNTREATED, status is UNATTRIBUTED_UNTREATED and protected GMV is 0.
    """
    res = calculate_attribution(
        payment_amount_minor_units=50000,
        p0=Decimal("0.8500"),
        treatment_status=TreatmentStatus.UNTREATED,
        observed_payment_outcome="CAPTURED",
        provenance=ModelProvenance.SYNTHETIC_DEVELOPMENT,
        prediction_status="PREDICTED",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=30.0,
    )

    assert res.attribution_status == AttributionStatus.UNATTRIBUTED_UNTREATED
    assert res.attributed_protected_gmv_minor_units == 0


def test_attribution_calculator_execution_failed_zero_gmv():
    """
    If execution attempt failed, status is UNATTRIBUTED_EXECUTION_FAILED and protected GMV is 0.
    """
    res = calculate_attribution(
        payment_amount_minor_units=50000,
        p0=Decimal("0.8500"),
        treatment_status=TreatmentStatus.EXECUTION_FAILED,
        observed_payment_outcome="CAPTURED",
        provenance=ModelProvenance.SYNTHETIC_DEVELOPMENT,
        prediction_status="PREDICTED",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=30.0,
    )

    assert res.attribution_status == AttributionStatus.UNATTRIBUTED_EXECUTION_FAILED
    assert res.attributed_protected_gmv_minor_units == 0


def test_attribution_calculator_missing_prediction():
    """
    Missing or ineligible Stage 5 prediction yields INDETERMINATE_INSUFFICIENT_EVIDENCE.
    """
    res = calculate_attribution(
        payment_amount_minor_units=50000,
        p0=None,
        treatment_status=TreatmentStatus.TREATED,
        observed_payment_outcome="CAPTURED",
        provenance=ModelProvenance.UNKNOWN,
        prediction_status="NOT_ELIGIBLE",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=30.0,
    )

    assert res.attribution_status == AttributionStatus.INDETERMINATE_INSUFFICIENT_EVIDENCE
    assert res.attributed_protected_gmv_minor_units == 0


def test_attribution_id_determinism():
    """
    UUID5 attribution ID is completely deterministic for a given command_id and version.
    """
    cmd_id = uuid.uuid4()
    id1 = make_attribution_id(cmd_id, 1)
    id2 = make_attribution_id(cmd_id, 1)
    id3 = make_attribution_id(cmd_id, 2)

    assert id1 == id2
    assert id1 != id3
