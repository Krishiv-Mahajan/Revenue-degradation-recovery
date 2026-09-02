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
        self.model_version = "untrained-heuristic-v1"
        self._feature_schema_version = "v1.0.0"

    def predict(self, feature_vector: Dict[str, Any]) -> float:
        """
        Deterministically produces a synthetic failure probability based on features.
        Output is bounded strictly to [0.0, 1.0].
        """
        # Base failure risk assumption
        prob = 0.05
        
        # Adjust based on historical global failure rate if available
        if feature_vector.get("global_30m_failure_rate") is not None:
            # Shift towards the recent historical global rate
            prob = 0.5 * prob + 0.5 * feature_vector["global_30m_failure_rate"]
            
        # Strongly adjust if in active degradation
        if feature_vector.get("is_in_active_degradation") is True:
            severity = feature_vector.get("degradation_severity")
            if severity == "HIGH":
                prob += 0.3
            elif severity == "MEDIUM":
                prob += 0.15
            elif severity == "LOW":
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

class SyntheticLogisticRegressionModel(FailurePredictionModel):
    def __init__(self, model, feature_names, encoders):
        self.model = model
        self.feature_names = feature_names
        self.encoders = encoders
        self.model_name = "SyntheticLogisticRegressionModel"
        self.model_version = "synthetic-development-v1"
        self._feature_schema_version = "v1.0.0-synthetic"

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


def get_production_model() -> FailurePredictionModel:
    """
    Attempts to load the persisted synthetic development model.
    Falls back to the DeterministicBaselineModel if the artifact doesn't exist.
    """
    model_path = os.path.join(os.path.dirname(__file__), "../../../models/synthetic-development-v1.joblib")
    if os.path.exists(model_path):
        try:
            logger.info("SYNTHETIC / DEVELOPMENT - Loading synthetic model")
            return joblib.load(model_path)
        except Exception as e:
            logger.error(f"Failed to load synthetic model: {e}")
            
    logger.info("Falling back to DeterministicBaselineModel")
    return DeterministicBaselineModel()
