import pytest
import os
from src.core.ml.model import (
    load_production_model,
    get_production_model,
    load_synthetic_development_model,
    get_development_model,
    DeterministicBaselineModel,
    SyntheticLogisticRegressionModel,
)


def test_load_production_model_unconfigured_fails_clearly(monkeypatch):
    monkeypatch.delenv("STAGE5_MODEL_PATH", raising=False)
    with pytest.raises(ValueError, match="Production model artifact path not configured"):
        load_production_model()


def test_load_production_model_missing_file_fails_clearly():
    with pytest.raises(FileNotFoundError, match="Required production model artifact not found"):
        load_production_model(artifact_path="/tmp/nonexistent_prod_model.joblib")


def test_get_production_model_does_not_silently_fallback(monkeypatch):
    monkeypatch.delenv("STAGE5_MODEL_PATH", raising=False)
    # Must raise, not silently return DeterministicBaselineModel
    with pytest.raises(ValueError):
        get_production_model()


def test_load_synthetic_development_model_missing_fails_clearly():
    with pytest.raises(FileNotFoundError, match="Synthetic model artifact not found"):
        load_synthetic_development_model(artifact_path="/tmp/nonexistent_syn_model.joblib")


def test_get_development_model_deterministic():
    model = get_development_model(mode="deterministic")
    assert isinstance(model, DeterministicBaselineModel)
    assert model.model_name == "DeterministicBaselineModel"
    assert model.model_version == "untrained-heuristic-v1"


def test_get_development_model_invalid_mode():
    with pytest.raises(ValueError, match="Unknown development model mode"):
        get_development_model(mode="invalid_mode")


def test_load_synthetic_development_model_if_persisted():
    default_path = os.path.join(
        os.path.dirname(__file__), "../../models/synthetic-development-v1.joblib"
    )
    if os.path.exists(default_path):
        model = load_synthetic_development_model(default_path)
        assert isinstance(model, SyntheticLogisticRegressionModel)
        assert model.model_name == "SyntheticLogisticRegressionModel"
        assert model.model_version == "synthetic-development-v1"
        assert model.status == "SYNTHETIC / DEVELOPMENT — NOT EMPIRICAL"
