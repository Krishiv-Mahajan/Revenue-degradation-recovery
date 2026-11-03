"""Live Stage 5 model selection: the heuristic unless a pinned, validated artifact is configured."""
import hashlib
import os

import pytest

from src.core.ml.model import DeterministicBaselineModel, FailurePredictionModel
from src.core.ml import selection
from src.core.ml.selection import (
    MAX_HOLDOUT_BRIER_SCORE,
    MIN_HOLDOUT_ROC_AUC,
    select_live_model,
)

COMMITTED_SYNTHETIC_ARTIFACT = os.path.join(
    os.path.dirname(__file__), "../../models/synthetic-development-v1.joblib"
)


class StubModel(FailurePredictionModel):
    model_name = "StubLogisticModel"
    model_version = "stub-1"

    def __init__(self, metadata=None, schema="v1.0.0"):
        self.training_metadata = metadata
        self._schema = schema

    def predict(self, feature_vector):
        return 0.42

    def get_feature_schema_version(self):
        return self._schema


GOOD_METADATA = {"metrics_computed": True, "metrics": {"roc_auc": 0.81, "brier_score": 0.12}}


@pytest.fixture(autouse=True)
def _empty_cache():
    selection._artifact_cache.clear()
    yield
    selection._artifact_cache.clear()


@pytest.fixture
def artifact(tmp_path):
    path = tmp_path / "model.joblib"
    path.write_bytes(b"artifact-bytes")
    return str(path), hashlib.sha256(b"artifact-bytes").hexdigest()


def _artifact_env(path, sha):
    return {"STAGE5_MODEL_MODE": "artifact", "STAGE5_MODEL_PATH": path, "STAGE5_MODEL_SHA256": sha}


def _select(env, loader=None):
    return select_live_model(DeterministicBaselineModel, env=env, loader=loader or (lambda p: StubModel(GOOD_METADATA)))


def test_default_is_the_heuristic():
    chosen = _select({})
    assert isinstance(chosen.model, DeterministicBaselineModel)
    assert (chosen.requested_mode, chosen.selected, chosen.reason) == ("heuristic", "heuristic", "heuristic configured")


def test_the_heuristic_factory_is_called_at_selection_time():
    sentinel = StubModel()
    chosen = select_live_model(lambda: sentinel, env={})
    assert chosen.model is sentinel


def test_unknown_mode_falls_back():
    chosen = _select({"STAGE5_MODEL_MODE": "llm"})
    assert (chosen.selected, chosen.reason) == ("heuristic", "fallback: unknown STAGE5_MODEL_MODE 'llm'")


@pytest.mark.parametrize("env_update, reason", [
    ({"STAGE5_MODEL_PATH": ""}, "fallback: STAGE5_MODEL_PATH is not set"),
    ({"STAGE5_MODEL_SHA256": ""}, "fallback: STAGE5_MODEL_SHA256 must pin the artifact (64 hex characters)"),
    ({"STAGE5_MODEL_SHA256": "abc"}, "fallback: STAGE5_MODEL_SHA256 must pin the artifact (64 hex characters)"),
    ({"STAGE5_MODEL_PATH": "/nonexistent/model.joblib"}, "fallback: artifact file not found"),
    ({"STAGE5_MODEL_SHA256": "0" * 64}, "fallback: artifact SHA-256 does not match the pinned value"),
])
def test_unpinned_or_missing_artifacts_are_refused_before_loading(artifact, env_update, reason):
    calls = []
    env = {**_artifact_env(*artifact), **env_update}
    chosen = _select(env, loader=lambda p: calls.append(p) or StubModel(GOOD_METADATA))
    assert (chosen.selected, chosen.reason) == ("heuristic", reason)
    assert calls == []


@pytest.mark.parametrize("loaded, reason", [
    (object(), "fallback: artifact does not implement FailurePredictionModel"),
    (StubModel(GOOD_METADATA, schema="v2.0.0"), "fallback: feature schema 'v2.0.0' is not compatible with v1.0.0"),
    (StubModel(None), "fallback: artifact has no computed held-out metrics"),
    (StubModel({"metrics_computed": False, "metrics": {"roc_auc": 0.9, "brier_score": 0.1}}),
     "fallback: artifact has no computed held-out metrics"),
    (StubModel({"metrics_computed": True, "metrics": {"roc_auc": 0.55, "brier_score": 0.1}}),
     f"fallback: held-out ROC AUC 0.55 is below {MIN_HOLDOUT_ROC_AUC}"),
    (StubModel({"metrics_computed": True, "metrics": {"roc_auc": 0.9, "brier_score": 0.3}}),
     f"fallback: held-out Brier score 0.3 is above {MAX_HOLDOUT_BRIER_SCORE}"),
])
def test_artifacts_without_acceptable_validation_fall_back(artifact, loaded, reason):
    chosen = _select(_artifact_env(*artifact), loader=lambda p: loaded)
    assert isinstance(chosen.model, DeterministicBaselineModel)
    assert (chosen.selected, chosen.reason) == ("heuristic", reason)


def test_a_loader_error_falls_back_without_raising(artifact):
    def broken(path):
        raise EOFError("truncated pickle")

    chosen = _select(_artifact_env(*artifact), loader=broken)
    assert (chosen.selected, chosen.reason) == ("heuristic", "fallback: artifact could not be loaded (EOFError)")


def test_a_pinned_validated_artifact_is_selected_and_cached(artifact):
    calls = []

    def loader(path):
        calls.append(path)
        return StubModel(GOOD_METADATA)

    first = _select(_artifact_env(*artifact), loader=loader)
    second = _select(_artifact_env(*artifact), loader=loader)

    assert (first.selected, first.reason) == ("artifact", "artifact passed validation")
    assert first.model is second.model and len(calls) == 1
    assert first.describe() == {
        "requested_mode": "artifact", "selected": "artifact", "model_name": "StubLogisticModel",
        "model_version": "stub-1", "reason": "artifact passed validation",
        "validation": {"metrics_computed": True, "roc_auc": 0.81, "brier_score": 0.12,
                       "min_roc_auc": MIN_HOLDOUT_ROC_AUC, "max_brier_score": MAX_HOLDOUT_BRIER_SCORE},
    }


def test_the_committed_synthetic_artifact_is_refused_for_lacking_held_out_metrics():
    path = os.path.abspath(COMMITTED_SYNTHETIC_ARTIFACT)
    with open(path, "rb") as handle:
        sha = hashlib.sha256(handle.read()).hexdigest()

    chosen = select_live_model(DeterministicBaselineModel, env=_artifact_env(path, sha))

    assert isinstance(chosen.model, DeterministicBaselineModel)
    assert chosen.reason == "fallback: artifact has no computed held-out metrics"
