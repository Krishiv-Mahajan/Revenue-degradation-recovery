"""
Unit tests for Stage 8 Attribution Confidence and Evidence formulation.
"""
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
)


def test_provenance_weights_discount_synthetic():
    """
    Synthetic model baseline explicitly discounts prediction confidence to 0.6000.
    Empirical production baseline evaluates at 1.0000.
    """
    res_synth = calculate_attribution(
        payment_amount_minor_units=10000,
        p0=Decimal("0.8000"),
        treatment_status=TreatmentStatus.TREATED,
        observed_payment_outcome="CAPTURED",
        provenance=ModelProvenance.SYNTHETIC_DEVELOPMENT,
        prediction_status="PREDICTED",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=10.0,
    )
    assert res_synth.attribution_confidence == Decimal("0.6000")

    res_prod = calculate_attribution(
        payment_amount_minor_units=10000,
        p0=Decimal("0.8000"),
        treatment_status=TreatmentStatus.TREATED,
        observed_payment_outcome="CAPTURED",
        provenance=ModelProvenance.EMPIRICAL_PRODUCTION,
        prediction_status="PREDICTED",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=10.0,
    )
    assert res_prod.attribution_confidence == Decimal("1.0000")


def test_timing_confidence_proximity_tiers():
    """
    Test deterministic timing confidence tiers:
    <= 60s -> 1.0000
    <= 300s -> 0.8500
    > 300s -> 0.6000
    """
    assert compute_timing_confidence(15.0) == Decimal("1.0000")
    assert compute_timing_confidence(60.0) == Decimal("1.0000")
    assert compute_timing_confidence(120.0) == Decimal("0.8500")
    assert compute_timing_confidence(300.0) == Decimal("0.8500")
    assert compute_timing_confidence(301.0) == Decimal("0.6000")
    assert compute_timing_confidence(None) == Decimal("0.6000")


def test_confidence_floor_breach_indeterminate():
    """
    When combined alpha is below CONFIDENCE_FLOOR (0.2500),
    status must be INDETERMINATE_INSUFFICIENT_EVIDENCE and protected GMV is 0.
    """
    # Provenance UNKNOWN (0.3000) * WEAK (0.7000) * Late (0.6000) = 0.1260 < 0.2500
    res = calculate_attribution(
        payment_amount_minor_units=50000,
        p0=Decimal("0.8500"),
        treatment_status=TreatmentStatus.TREATED,
        observed_payment_outcome="CAPTURED",
        provenance=ModelProvenance.UNKNOWN,
        prediction_status="PREDICTED",
        diagnosis_confidence="WEAK",
        timing_delta_seconds=400.0,
    )

    assert res.attribution_status == AttributionStatus.INDETERMINATE_INSUFFICIENT_EVIDENCE
    assert res.attributed_protected_gmv_minor_units == 0
    assert res.counterfactual_loss_exposure_minor_units == 0
