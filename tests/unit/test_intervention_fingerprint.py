"""
Unit tests for Stage 6 Deterministic Input Fingerprinting.
"""
import uuid
from datetime import datetime, timezone, timedelta

from src.core.domain.intervention_models import (
    compute_decision_fingerprint,
    make_decision_id,
    NAMESPACE_STAGE6,
)


def test_fingerprint_reproducibility():
    t_decide = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    pred_id = uuid.uuid4()
    ep_id = uuid.uuid4()

    fp1 = compute_decision_fingerprint(
        payment_attempt_id="pay_1",
        t_decide=t_decide,
        amount_minor_units=1000,
        currency="INR",
        stage5_prediction_id=pred_id,
        failure_probability=0.75,
        prediction_status="PREDICTED",
        episode_id=ep_id,
        episode_status="ACTIVE",
        rca_classification="SEGMENT_SPECIFIC",
        rca_candidate_dimension="BANK",
        rca_candidate_value="HDFC",
        rca_evidence_strength="STRONG",
        policy_id="default-recovery-v1",
        policy_version="1.0.0",
    )

    fp2 = compute_decision_fingerprint(
        payment_attempt_id="pay_1",
        t_decide=t_decide,
        amount_minor_units=1000,
        currency="INR",
        stage5_prediction_id=pred_id,
        failure_probability=0.75,
        prediction_status="PREDICTED",
        episode_id=ep_id,
        episode_status="ACTIVE",
        rca_classification="SEGMENT_SPECIFIC",
        rca_candidate_dimension="BANK",
        rca_candidate_value="HDFC",
        rca_evidence_strength="STRONG",
        policy_id="default-recovery-v1",
        policy_version="1.0.0",
    )

    assert fp1 == fp2
    assert fp1.startswith("sha256:")


def test_fingerprint_sensitivity_to_t_decide():
    # Demonstrating user requirement 5:
    # Evaluation identity = payment attempt + decision timestamp + upstream state + policy version.
    t1 = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=1)

    fp1 = compute_decision_fingerprint(
        payment_attempt_id="pay_1",
        t_decide=t1,
        amount_minor_units=1000,
        currency="INR",
        stage5_prediction_id=None,
        failure_probability=0.75,
        prediction_status="PREDICTED",
        episode_id=None,
        episode_status=None,
        rca_classification=None,
        rca_candidate_dimension=None,
        rca_candidate_value=None,
        rca_evidence_strength=None,
        policy_id="default-recovery-v1",
        policy_version="1.0.0",
    )

    fp2 = compute_decision_fingerprint(
        payment_attempt_id="pay_1",
        t_decide=t2,
        amount_minor_units=1000,
        currency="INR",
        stage5_prediction_id=None,
        failure_probability=0.75,
        prediction_status="PREDICTED",
        episode_id=None,
        episode_status=None,
        rca_classification=None,
        rca_candidate_dimension=None,
        rca_candidate_value=None,
        rca_evidence_strength=None,
        policy_id="default-recovery-v1",
        policy_version="1.0.0",
    )

    assert fp1 != fp2


def test_make_decision_id_deterministic():
    id1 = make_decision_id("pay_123", 1)
    id2 = make_decision_id("pay_123", 1)
    id_v2 = make_decision_id("pay_123", 2)

    assert id1 == id2
    assert id1 != id_v2
    assert isinstance(id1, uuid.UUID)
