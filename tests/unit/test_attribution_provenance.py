"""
Stage 8 model provenance: a Stage 5 prediction counts as EMPIRICAL_PRODUCTION evidence only when its
exact (model_name, model_version) is registered as empirically validated. Development models (the
deterministic heuristic and the synthetic logistic model) are SYNTHETIC_DEVELOPMENT, and any other
unregistered model is UNKNOWN.

The attribution service runs against a mocked repository for one treated, captured payment; no
database is used.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.attribution import attribution_service
from src.core.attribution.attribution_service import CounterfactualAttributionService
from src.core.domain.attribution_models import AttributionStatus, ModelProvenance, TreatmentStatus
from src.core.ml.model import DeterministicBaselineModel, SyntheticLogisticRegressionModel

T0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
AS_OF = T0 + timedelta(seconds=25)
AMOUNT = 50000
EMPIRICAL_MODEL = ("Stage5EmpiricalModel", "2026.10.0")


def _identity(model):
    return model.model_name, model.model_version


DETERMINISTIC_BASELINE = _identity(DeterministicBaselineModel())
SYNTHETIC_DEVELOPMENT = _identity(SyntheticLogisticRegressionModel(MagicMock(), [], {}))


def _treated_capture_repo(model_name, model_version):
    """One ACT decision whose successful execution preceded a capture 14s later (STRONG diagnosis)."""
    command = MagicMock(
        command_id=uuid.uuid4(),
        decision_id=uuid.uuid4(),
        payment_attempt_id="pay_1",
        source_system="razorpay",
        route_key="RETRY_SECONDARY_GATEWAY",
        command_status="SUCCEEDED",
        created_at=T0,
    )
    attempt = MagicMock(
        attempt_id=uuid.uuid4(),
        execution_status="SUCCESS",
        is_simulation=True,
        started_at=T0,
        completed_at=T0 + timedelta(seconds=1),
    )
    observation = MagicMock(
        observation_id=uuid.uuid4(),
        terminal_event_id=uuid.uuid4(),
        terminal_event_type="payment.captured",
        terminal_event_timestamp=T0 + timedelta(seconds=15),
        payment_outcome="CAPTURED",
        observation_audit_payload={},
    )
    decision = MagicMock(
        decision_id=command.decision_id,
        source_system="razorpay",
        decision_type="ACT",
        stage5_prediction_id=uuid.uuid4(),
        diagnosis_confidence="STRONG",
        decided_at=T0,
    )
    prediction = MagicMock(
        source_system="razorpay",
        model_name=model_name,
        model_version=model_version,
        failure_probability=0.85,
        prediction_status="PREDICTED",
    )
    repo = AsyncMock()
    repo.get_attribution_by_as_of.return_value = None
    repo.get_latest_attribution.return_value = None
    repo.get_command.return_value = command
    repo.get_latest_execution_attempt.return_value = attempt
    repo.get_latest_outcome_observation.return_value = observation
    repo.get_decision.return_value = decision
    repo.get_prediction.return_value = prediction
    repo.get_canonical_payment_amount.return_value = AMOUNT
    repo.append_attribution.side_effect = lambda attribution: attribution
    return command, repo


async def _attribute(model_name, model_version):
    command, repo = _treated_capture_repo(model_name, model_version)
    return await CounterfactualAttributionService(repo, attempt_repository=AsyncMock()).attribute_payment_intervention(
        command.command_id, as_of_timestamp=AS_OF
    )


def _assert_attributed(attribution, provenance, confidence, protected_gmv, is_synthetic):
    # p0 = 0.85, c_diagnosis = 1.0 (STRONG), c_timing = 1.0 (14s), so confidence = c_provenance.
    assert attribution.treatment_status == TreatmentStatus.TREATED
    assert attribution.attribution_status == AttributionStatus.ATTRIBUTED
    assert attribution.attribution_audit_payload["stage5_model_provenance"] == provenance.value
    assert attribution.attribution_confidence == Decimal(confidence)
    assert attribution.attributed_protected_gmv_minor_units == protected_gmv
    assert attribution.is_synthetic_baseline is is_synthetic


@pytest.mark.parametrize("model", [
    pytest.param(DETERMINISTIC_BASELINE, id="deterministic-baseline"),
    pytest.param(SYNTHETIC_DEVELOPMENT, id="synthetic-logistic"),
])
async def test_development_model_predictions_are_synthetic_development(model):
    attribution = await _attribute(*model)
    # Manually calculated: protected = 50,000 * 0.85 * 0.6 = 25,500.
    _assert_attributed(attribution, ModelProvenance.SYNTHETIC_DEVELOPMENT, "0.6000", 25500, True)


@pytest.mark.parametrize("model_name", ["test_model", "Stage5EmpiricalModel", "", None])
async def test_unregistered_model_predictions_are_unknown_not_empirical(model_name):
    attribution = await _attribute(model_name, "1.0.0")
    # Manually calculated: protected = 50,000 * 0.85 * 0.3 = 12,750.
    _assert_attributed(attribution, ModelProvenance.UNKNOWN, "0.3000", 12750, True)


async def test_registered_empirical_model_predictions_are_empirical_production(monkeypatch):
    monkeypatch.setattr(attribution_service, "VERIFIED_EMPIRICAL_MODELS", frozenset({EMPIRICAL_MODEL}))
    attribution = await _attribute(*EMPIRICAL_MODEL)
    # Manually calculated: protected = 50,000 * 0.85 * 1.0 = 42,500.
    _assert_attributed(attribution, ModelProvenance.EMPIRICAL_PRODUCTION, "1.0000", 42500, False)


def test_registration_requires_the_exact_model_version(monkeypatch):
    monkeypatch.setattr(attribution_service, "VERIFIED_EMPIRICAL_MODELS", frozenset({EMPIRICAL_MODEL}))
    name, _ = EMPIRICAL_MODEL
    assert attribution_service.classify_model_provenance(name, "2026.10.1") == ModelProvenance.UNKNOWN


@pytest.mark.parametrize("model", [DETERMINISTIC_BASELINE, SYNTHETIC_DEVELOPMENT],
                         ids=["deterministic-baseline", "synthetic-logistic"])
def test_development_models_cannot_be_registered_as_empirical(model, monkeypatch):
    monkeypatch.setattr(attribution_service, "VERIFIED_EMPIRICAL_MODELS", frozenset({model}))
    assert attribution_service.classify_model_provenance(*model) == ModelProvenance.SYNTHETIC_DEVELOPMENT


def test_no_stage5_model_is_registered_as_empirically_validated():
    assert attribution_service.VERIFIED_EMPIRICAL_MODELS == frozenset()
