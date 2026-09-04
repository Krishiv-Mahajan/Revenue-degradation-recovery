from abc import ABC, abstractmethod
from typing import Dict, Any, Tuple

class FailurePredictionModel(ABC):
    """
    Interface for Stage 5 Failure Prediction models.
    """
    model_name: str
    model_version: str

    @abstractmethod
    def predict(self, feature_vector: Dict[str, Any]) -> float:
        """
        Returns failure_probability in [0.0, 1.0].
        """
        pass

    @abstractmethod
    def get_feature_schema_version(self) -> str:
        """
        Returns the feature schema version this model expects.
        """
        pass


class DeterministicBaselineModel(FailurePredictionModel):
    """
    A deterministic heuristic placeholder model for Phase 3.
    This is explicitly NOT an empirically validated logistic regression model.
    It produces synthetic probabilities for integration and pipeline verification.
    """
    
    def __init__(self):
        self.model_name = "DeterministicBaselineModel"
        self.model_version = "untrained-heuristic-v2"
        self._feature_schema_version = "v1.0.0"

    def predict(self, feature_vector: Dict[str, Any]) -> float:
        """
        Deterministically produces a synthetic failure probability based on features.
        Output is bounded strictly to [0.0, 1.0].

        Behavioral rate selection (Policy B fallback — untrained-heuristic-v2):
          1. Prefer global_30m_failure_rate when available (non-null).
          2. Fall back to global_24h_failure_rate when 30m is null (same 0.5 coefficient).
          3. Skip behavioral adjustment when both are null.
        """
        # Base failure risk assumption
        prob = 0.05

        # Adjust based on historical global failure rate.
        # Prefer 30m window; fall back to 24h window when 30m volume is insufficient.
        # Both windows use the same 0.5 blending coefficient.
        rate = feature_vector.get("global_30m_failure_rate")
        if rate is None:
            rate = feature_vector.get("global_24h_failure_rate")
        if rate is not None:
            # Shift towards the best available recent historical global rate
            prob = 0.5 * prob + 0.5 * rate

        # Strongly adjust if in active degradation
        if feature_vector.get("is_in_active_degradation") is True:
            severity = feature_vector.get("degradation_severity")
            if severity == "CRITICAL":
                prob += 0.30
            elif severity == "HIGH":
                prob += 0.15
            elif severity == "MODERATE":
                prob += 0.05

        # Adjust if RCA explicitly implicates this segment
        if feature_vector.get("rca_candidate_matches_payment_segment") is True:
            evidence = feature_vector.get("rca_evidence_strength")
            if evidence == "STRONG":
                prob += 0.2
            elif evidence == "MODERATE":
                prob += 0.1

        # Enforce bounds
        return max(0.0, min(1.0, prob))

    def get_feature_schema_version(self) -> str:
        return self._feature_schema_version

import numpy as np
import joblib
import os
import logging

logger = logging.getLogger(__name__)

from typing import Optional

DEFAULT_SYNTHETIC_MODEL_PATH = os.path.join(
    os.path.dirname(__file__), "../../../models/synthetic-development-v1.joblib"
)


class SyntheticLogisticRegressionModel(FailurePredictionModel):
    """
    SYNTHETIC / DEVELOPMENT — NOT EMPIRICAL

    A logistic regression model trained strictly on synthetic development data
    for pipeline validation. It does NOT represent empirical Razorpay payment traffic
    and must never be treated as an empirical production model.
    """
    status: str = "SYNTHETIC / DEVELOPMENT — NOT EMPIRICAL"

    def __init__(self, model, feature_names, encoders):
        self.model = model
        self.feature_names = feature_names
        self.encoders = encoders
        self.model_name = "SyntheticLogisticRegressionModel"
        self.model_version = "synthetic-development-v1"
        self._feature_schema_version = "v1.0.0-synthetic"
        self.status = "SYNTHETIC / DEVELOPMENT — NOT EMPIRICAL"

    def predict(self, feature_vector: Dict[str, Any]) -> float:
        row = []
        for feature in self.feature_names:
            val = feature_vector.get(feature)
            # Categorical encoding fallback
            if feature in self.encoders:
                encoded = self.encoders[feature].get(val, 0)
                row.append(encoded)
            else:
                row.append(float(val) if val is not None else 0.0)
                
        X = np.array([row])
        # Logistic Regression predict_proba returns [prob_0, prob_1]
        prob = self.model.predict_proba(X)[0][1]
        return float(prob)

    def get_feature_schema_version(self) -> str:
        return self._feature_schema_version


def load_synthetic_development_model(
    artifact_path: Optional[str] = None,
) -> SyntheticLogisticRegressionModel:
    """
    Explicit synthetic-development model loader.
    Loads the synthetic-development-v1 artifact.
    Fails clearly if the artifact does not exist.
    """
    path = os.path.abspath(artifact_path or DEFAULT_SYNTHETIC_MODEL_PATH)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"SYNTHETIC / DEVELOPMENT — Synthetic model artifact not found at {path}. "
            "Train and persist the model first using scripts/train_synthetic_model.py."
        )
    logger.info("SYNTHETIC / DEVELOPMENT — Loading synthetic model from %s", path)
    model = joblib.load(path)
    if not isinstance(model, SyntheticLogisticRegressionModel):
        raise TypeError(
            f"Expected SyntheticLogisticRegressionModel artifact, got {type(model).__name__}"
        )
    return model


def load_production_model(
    artifact_path: Optional[str] = None,
) -> FailurePredictionModel:
    """
    Loads the production/empirical model artifact.
    Requires an explicitly configured artifact path (via parameter or STAGE5_MODEL_PATH).
    Fails clearly rather than silently falling back to a heuristic or placeholder.
    """
    path = artifact_path or os.getenv("STAGE5_MODEL_PATH")
    if not path:
        raise ValueError(
            "Production model artifact path not configured. "
            "Set the STAGE5_MODEL_PATH environment variable or pass artifact_path explicitly."
        )
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Required production model artifact not found at {path}. "
            "Production prediction cannot proceed without the configured model artifact."
        )
    logger.info("Loading production model from %s", path)
    model = joblib.load(path)
    if not isinstance(model, FailurePredictionModel):
        raise TypeError(
            f"Loaded model from {path} does not implement FailurePredictionModel interface."
        )
    return model


def get_production_model(
    artifact_path: Optional[str] = None,
) -> FailurePredictionModel:
    """
    Production prediction path.
    Requires an explicitly configured model artifact and fails clearly if missing.
    Must NOT silently fall back to DeterministicBaselineModel or change model semantics.
    """
    return load_production_model(artifact_path=artifact_path)


def get_development_model(
    mode: str = "synthetic",
    artifact_path: Optional[str] = None,
) -> FailurePredictionModel:
    """
    Explicit development and test model loader.
    Modes:
      - 'synthetic': loads synthetic-development-v1 artifact (fails clearly if missing)
      - 'deterministic': returns DeterministicBaselineModel heuristic for unit/contract tests
    """
    if mode == "synthetic":
        return load_synthetic_development_model(artifact_path=artifact_path)
    elif mode == "deterministic":
        return DeterministicBaselineModel()
    else:
        raise ValueError(
            f"Unknown development model mode: {mode}. Must be 'synthetic' or 'deterministic'."
        )
