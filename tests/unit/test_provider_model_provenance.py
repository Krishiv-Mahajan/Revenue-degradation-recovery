"""
Stage 5 model provenance: provider-evaluation training produces a distinct, data-fingerprinted
ProviderLogisticRegressionModel that Stage 8 classifies as UNKNOWN until explicitly registered, while
the default path and the committed synthetic artifact are unchanged.

Training uses small in-memory datasets; no database is used.
"""
import os
import re
from dataclasses import make_dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import joblib
import numpy as np
import pytest

from src.core.attribution import attribution_service
from src.core.attribution.attribution_service import classify_model_provenance
from src.core.domain.attribution_models import ModelProvenance
from src.core.ml.model import (
    ProviderLogisticRegressionModel,
    SyntheticLogisticRegressionModel,
    load_production_model,
    load_synthetic_development_model,
)
from src.core.ml.training import (
    CONTAMINATION_LOOKBACK,
    LABEL_HORIZON,
    EvaluationDataContaminationError,
    Stage5PipelineValidator,
    TrainingPopulation,
    compute_dataset_fingerprint,
)

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
ARTIFACT = os.path.join(os.path.dirname(__file__), "../../models/synthetic-development-v1.joblib")
VERSION = re.compile(r"provider-eval-[0-9a-f]{12}")


FEATURES = ["amount_minor_units", "hour_of_day", "day_of_week", "global_30m_failure_rate",
            "payment_method_30m_failure_rate", "payment_method", "currency", "bank", "wallet",
            "degradation_severity"]
_RowSnapshot = make_dataclass("_RowSnapshot", FEATURES)


def _dataset(n=50):
    """Deterministic samples with both classes in every chronological split."""
    X_raw, y_raw, times = [], [], []
    for i in range(n):
        X_raw.append({
            "amount_minor_units": 1000 + 37 * i, "hour_of_day": i % 24, "day_of_week": i % 7,
            "global_30m_failure_rate": None, "payment_method_30m_failure_rate": (i % 5) / 10,
            "payment_method": ["card", "upi"][i % 2], "currency": "INR", "bank": None, "wallet": None,
            "degradation_severity": None,
        })
        y_raw.append(1 if i % 3 == 0 else 0)
        times.append(T0 + timedelta(minutes=i))
    return X_raw, y_raw, times


def _snapshot_session():
    session = MagicMock()
    session.in_transaction = MagicMock(return_value=False)
    session.connection = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value="repeatable read")))
    session.sync_session.get_transaction = MagicMock(return_value=SimpleNamespace(is_active=True))
    return session


async def _extracted_validator(dataset, validator=None, guard=None):
    """Runs the real provider extract_dataset over `dataset`; its queries are replaced by the samples."""
    X_raw, y_raw, times = dataset
    validator = validator or Stage5PipelineValidator(
        _snapshot_session(), population=TrainingPopulation.PROVIDER_EVALUATION
    )
    events = [SimpleNamespace(payment_id=f"pay_{i}", ingested_at=t) for i, t in enumerate(times)]
    samples = {e.payment_id: (row, label) for e, row, label in zip(events, X_raw, y_raw)}
    validator.get_authorized_events = AsyncMock(return_value=events)
    validator.assert_evaluation_window_uncontaminated = guard or AsyncMock()
    validator.get_terminal_label = AsyncMock(side_effect=lambda pid, t: samples[pid][1])
    validator.reconstructor.reconstruct_features = AsyncMock(
        side_effect=lambda pid, t: _RowSnapshot(**samples[pid][0])
    )
    assert await validator.extract_dataset() == (X_raw, y_raw, times)
    return validator


async def _train_provider(dataset=None):
    dataset = dataset or _dataset()
    return (await _extracted_validator(dataset)).run_pipeline(*dataset)


# --- default path ---------------------------------------------------------------------------

def test_default_training_returns_the_unchanged_synthetic_identity():
    X_raw, y_raw, times = _dataset()
    model, metrics = Stage5PipelineValidator(AsyncMock()).run_pipeline(X_raw, y_raw, times)

    assert type(model) is SyntheticLogisticRegressionModel
    assert (model.model_name, model.model_version, model.get_feature_schema_version()) == (
        "SyntheticLogisticRegressionModel", "synthetic-development-v1", "v1.0.0-synthetic"
    )
    assert model.status == "SYNTHETIC / DEVELOPMENT — NOT EMPIRICAL"
    assert model.training_population == "SYNTHETIC_DEVELOPMENT" and model.training_metadata is None
    assert set(metrics) == {"pr_auc", "roc_auc", "brier_score"}


def test_committed_synthetic_artifact_keeps_its_identity_and_gains_default_provenance():
    model = load_synthetic_development_model(ARTIFACT)
    assert type(model) is SyntheticLogisticRegressionModel
    assert (model.model_name, model.model_version) == ("SyntheticLogisticRegressionModel", "synthetic-development-v1")
    assert model.training_population == "SYNTHETIC_DEVELOPMENT"
    assert model.training_metadata is None
    assert classify_model_provenance(model.model_name, model.model_version) == ModelProvenance.SYNTHETIC_DEVELOPMENT


# --- prediction parity ----------------------------------------------------------------------

def _reference_predict(model, feature_vector):
    """The pre-refactor SyntheticLogisticRegressionModel.predict, reproduced verbatim."""
    row = []
    for feature in model.feature_names:
        val = feature_vector.get(feature)
        if feature in model.encoders:
            row.append(model.encoders[feature].get(val, 0))
        else:
            row.append(float(val) if val is not None else 0.0)
    return float(model.model.predict_proba(np.array([row]))[0][1])


VECTORS = [
    {"amount_minor_units": 1000, "hour_of_day": 12, "day_of_week": 2, "payment_method": "UPI", "currency": "INR"},
    {"amount_minor_units": 50000, "hour_of_day": 0, "day_of_week": 6, "payment_method": "card", "currency": "USD",
     "global_30m_failure_rate": 0.8, "payment_method_30m_failure_rate": 0.4, "bank": "HDFC"},
    {"amount_minor_units": None, "payment_method": "unseen-method", "degradation_severity": "CRITICAL"},
    {},
]


@pytest.mark.parametrize("vector", VECTORS)
def test_synthetic_artifact_predictions_match_the_original_implementation(vector):
    model = load_synthetic_development_model(ARTIFACT)
    assert model.predict(vector) == _reference_predict(model, vector)


@pytest.mark.parametrize("vector", VECTORS)
def test_provider_model_predicts_identically_to_the_synthetic_model_with_the_same_estimator(vector):
    synthetic = load_synthetic_development_model(ARTIFACT)
    provider = ProviderLogisticRegressionModel(
        synthetic.model, synthetic.feature_names, synthetic.encoders,
        model_version="provider-eval-0123456789ab", training_metadata={},
    )
    assert provider.predict(vector) == synthetic.predict(vector)


# --- provider identity and metadata ---------------------------------------------------------

async def test_provider_training_returns_a_distinct_fingerprinted_identity():
    X_raw, y_raw, times = _dataset()
    validator = await _extracted_validator((X_raw, y_raw, times))
    model, metrics = validator.run_pipeline(X_raw, y_raw, times)

    assert type(model) is ProviderLogisticRegressionModel
    assert not isinstance(model, SyntheticLogisticRegressionModel)
    assert model.model_name == "ProviderLogisticRegressionModel"
    assert "synthetic" not in model.model_name.lower()
    assert VERSION.fullmatch(model.model_version) and len(model.model_version) <= 50
    assert model.status == "PROVIDER-TRAINED — NOT VALIDATED"
    assert model.training_population == "PROVIDER_EVALUATION"

    fingerprint = compute_dataset_fingerprint(X_raw, y_raw, times)
    start, end = validator.checked_contamination_window
    assert (start, end) == (times[0] - CONTAMINATION_LOOKBACK, times[-1] + LABEL_HORIZON)
    assert model.model_version == f"provider-eval-{fingerprint[:12]}"
    assert model.training_metadata == {
        "training_population": "PROVIDER_EVALUATION",
        "sample_window": {"start": times[0].isoformat(), "end": times[-1].isoformat()},
        "sample_count": 50,
        "metrics": {k: float(v) for k, v in metrics.items()},
        "metrics_computed": True,
        "contamination_window": {"start": start.isoformat(), "end": end.isoformat()},
        "contamination_lookback_seconds": int(CONTAMINATION_LOOKBACK.total_seconds()),
        "label_horizon_seconds": int(LABEL_HORIZON.total_seconds()),
        "dataset_fingerprint": fingerprint,
    }


def test_provider_training_without_a_contamination_check_is_refused():
    X_raw, y_raw, times = _dataset()
    validator = Stage5PipelineValidator(AsyncMock(), population=TrainingPopulation.PROVIDER_EVALUATION)
    with pytest.raises(RuntimeError, match="contamination check"):
        validator.run_pipeline(X_raw, y_raw, times)


def _subset(dataset):
    X_raw, y_raw, times = dataset
    return X_raw[:-1], y_raw[:-1], times[:-1]


@pytest.mark.parametrize("stale", ["label", "feature", "time", "subset"])
async def test_provider_training_refuses_data_other_than_the_extracted_dataset(stale):
    validator = await _extracted_validator(_dataset())
    other = _subset(_dataset()) if stale == "subset" else _changed(stale)
    with pytest.raises(RuntimeError, match="do not match the dataset extracted"):
        validator.run_pipeline(*other)


async def test_a_new_extraction_replaces_the_checked_dataset():
    first, second = _dataset(), _changed("label")
    validator = await _extracted_validator(first)
    await _extracted_validator(second, validator=validator)

    with pytest.raises(RuntimeError, match="do not match the dataset extracted"):
        validator.run_pipeline(*first)
    model, _ = validator.run_pipeline(*second)
    assert model.training_metadata["dataset_fingerprint"] == compute_dataset_fingerprint(*second)


async def test_a_failed_extraction_cannot_reuse_the_previous_checked_dataset():
    validator = await _extracted_validator(_dataset())
    refusing_guard = AsyncMock(side_effect=EvaluationDataContaminationError("contaminated"))

    with pytest.raises(EvaluationDataContaminationError):
        await _extracted_validator(_dataset(), validator=validator, guard=refusing_guard)

    assert validator.checked_contamination_window is None
    with pytest.raises(RuntimeError, match="contamination check first"):
        validator.run_pipeline(*_dataset())


async def test_provider_training_refuses_an_extraction_without_samples():
    validator = await _extracted_validator(([], [], []))
    with pytest.raises(RuntimeError, match="contamination check first"):
        validator.run_pipeline([], [], [])


async def test_extract_dataset_records_the_window_its_guard_checked():
    session = MagicMock()
    session.in_transaction = MagicMock(return_value=False)
    session.connection = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value="repeatable read")))
    transaction = SimpleNamespace(is_active=True)
    session.sync_session.get_transaction = MagicMock(return_value=transaction)
    validator = Stage5PipelineValidator(session, population=TrainingPopulation.PROVIDER_EVALUATION)
    events = [SimpleNamespace(payment_id="pay_A", ingested_at=T0),
              SimpleNamespace(payment_id="pay_B", ingested_at=T0 + timedelta(hours=1))]
    validator.get_authorized_events = AsyncMock(return_value=events)
    validator.assert_evaluation_window_uncontaminated = AsyncMock()
    validator.get_terminal_label = AsyncMock(return_value=-1)

    assert validator.checked_contamination_window is None
    await validator.extract_dataset()

    expected = (T0 - CONTAMINATION_LOOKBACK, T0 + timedelta(hours=1) + LABEL_HORIZON)
    validator.assert_evaluation_window_uncontaminated.assert_awaited_once_with(*expected)
    assert validator.checked_contamination_window == expected


@pytest.mark.parametrize("version", [
    "provider-eval-0123456789AB", "provider-eval-0123456789a", "provider-eval-0123456789abc",
    "synthetic-development-v1", "", None,
])
def test_provider_model_rejects_versions_outside_the_convention(version):
    with pytest.raises(ValueError):
        ProviderLogisticRegressionModel(MagicMock(), [], {}, model_version=version, training_metadata={})


# --- deterministic fingerprints -------------------------------------------------------------

async def test_identical_data_yields_identical_version_and_fingerprint():
    first, _ = await _train_provider()
    second, _ = await _train_provider()
    assert first.model_version == second.model_version
    assert first.training_metadata["dataset_fingerprint"] == second.training_metadata["dataset_fingerprint"]


def test_fingerprint_ignores_feature_key_order():
    X_raw, y_raw, times = _dataset()
    reordered = [dict(reversed(list(row.items()))) for row in X_raw]
    assert compute_dataset_fingerprint(reordered, y_raw, times) == compute_dataset_fingerprint(X_raw, y_raw, times)


def _changed(kind):
    X_raw, y_raw, times = _dataset()
    if kind == "label":
        y_raw[10] = 1 - y_raw[10]
    elif kind == "feature":
        X_raw[10] = {**X_raw[10], "amount_minor_units": X_raw[10]["amount_minor_units"] + 1}
    else:
        times[10] = times[10] + timedelta(seconds=1)
    return X_raw, y_raw, times


@pytest.mark.parametrize("kind", ["label", "feature", "time"])
def test_any_data_change_yields_a_new_fingerprint(kind):
    assert compute_dataset_fingerprint(*_changed(kind)) != compute_dataset_fingerprint(*_dataset())


def test_fingerprint_rejects_misaligned_inputs():
    X_raw, y_raw, times = _dataset()
    with pytest.raises(ValueError):
        compute_dataset_fingerprint(X_raw, y_raw[:-1], times)


@pytest.mark.parametrize("offset", [timedelta(hours=5, minutes=30), timedelta(hours=-7), timedelta(0)])
def test_fingerprint_is_equal_for_utc_equivalent_timestamps(offset):
    X_raw, y_raw, times = _dataset()
    shifted = [t.astimezone(timezone(offset)) for t in times]
    assert compute_dataset_fingerprint(X_raw, y_raw, shifted) == compute_dataset_fingerprint(X_raw, y_raw, times)


async def test_provider_version_is_equal_for_utc_equivalent_sample_times():
    X_raw, y_raw, times = _dataset()
    ist_times = [t.astimezone(timezone(timedelta(hours=5, minutes=30))) for t in times]
    utc_model, _ = await _train_provider((X_raw, y_raw, times))
    ist_model, _ = await _train_provider((X_raw, y_raw, ist_times))
    assert ist_model.model_version == utc_model.model_version


def test_fingerprint_rejects_naive_timestamps():
    X_raw, y_raw, times = _dataset()
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_dataset_fingerprint(X_raw, y_raw, [t.replace(tzinfo=None) for t in times])


def test_fingerprint_depends_on_sample_order_so_extraction_order_must_be_stable():
    X_raw, y_raw, times = _dataset()
    tied = [T0] * len(times)
    swapped = [X_raw[1], X_raw[0], *X_raw[2:]], [y_raw[1], y_raw[0], *y_raw[2:]]
    assert compute_dataset_fingerprint(*swapped, tied) != compute_dataset_fingerprint(X_raw, y_raw, tied)


def test_fingerprint_documents_what_it_does_not_identify():
    doc = " ".join(compute_dataset_fingerprint.__doc__.split())
    for excluded in ("training code", "hyperparameters", "artifact identity"):
        assert excluded in doc


# --- Stage 8 provenance classification ------------------------------------------------------

async def test_provider_trained_models_are_unknown_by_default():
    model, _ = await _train_provider()
    assert attribution_service.VERIFIED_EMPIRICAL_MODELS == frozenset()
    assert classify_model_provenance(model.model_name, model.model_version) == ModelProvenance.UNKNOWN


async def test_only_the_exact_registered_provider_version_is_empirical(monkeypatch):
    model, _ = await _train_provider()
    other, _ = await _train_provider(_changed("label"))
    monkeypatch.setattr(attribution_service, "VERIFIED_EMPIRICAL_MODELS",
                        frozenset({(model.model_name, model.model_version)}))

    assert classify_model_provenance(model.model_name, model.model_version) == ModelProvenance.EMPIRICAL_PRODUCTION
    assert other.model_version != model.model_version
    assert classify_model_provenance(other.model_name, other.model_version) == ModelProvenance.UNKNOWN


# --- loaders --------------------------------------------------------------------------------

async def test_synthetic_loader_rejects_a_provider_artifact(tmp_path):
    model, _ = await _train_provider()
    path = tmp_path / "provider.joblib"
    joblib.dump(model, path)

    with pytest.raises(TypeError, match="Expected SyntheticLogisticRegressionModel"):
        load_synthetic_development_model(str(path))

    loaded = load_production_model(str(path))
    assert type(loaded) is ProviderLogisticRegressionModel
    assert (loaded.model_name, loaded.model_version) == (model.model_name, model.model_version)
    assert loaded.training_metadata == model.training_metadata
