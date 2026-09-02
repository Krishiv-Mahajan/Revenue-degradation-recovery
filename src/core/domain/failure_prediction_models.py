"""
Stage 5 — Failure Prediction domain models.

Predicts failure probability for an eligible payment attempt.
See stage5_design.md for full specification.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

# Deterministic namespace for UUID5 identity (§25C of design).
NAMESPACE_STAGE5: uuid.UUID = uuid.UUID("c3d4e5f6-a7b8-9012-cdef-345678901234")


class PredictionStatus(str, Enum):
    PREDICTED = "PREDICTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


@dataclass(frozen=True)
class FailurePrediction:
    """
    Immutable, versioned failure prediction for a payment attempt.
    Append-only — never updated or deleted after insertion.
    """
    prediction_id: uuid.UUID
    payment_attempt_id: str
    prediction_version: int
    predicted_at: datetime
    prediction_horizon: str
    failure_probability: Optional[float]
    risk_band: Optional[str]
    prediction_status: str
    model_name: str
    model_version: str
    feature_schema_version: str
    feature_snapshot: dict
    input_fingerprint: str
    created_at: datetime


def make_prediction_id(payment_attempt_id: str, prediction_version: int) -> uuid.UUID:
    """
    Deterministic UUID5 for a prediction.
    """
    return uuid.uuid5(NAMESPACE_STAGE5, f"{payment_attempt_id}:{prediction_version}")


def compute_input_fingerprint(
    payment_attempt_id: str,
    predicted_at: datetime,
    prediction_horizon: str,
    feature_snapshot: dict,
    model_name: str,
    model_version: str,
    feature_schema_version: str,
) -> str:
    """
    Deterministic SHA-256 fingerprint of the prediction input state.
    Same inputs + same model/schema => same fingerprint.
    """
    fingerprint_inputs = {
        "payment_attempt_id": payment_attempt_id,
        "predicted_at": predicted_at.isoformat(),
        "prediction_horizon": prediction_horizon,
        "feature_snapshot": feature_snapshot,
        "model_name": model_name,
        "model_version": model_version,
        "feature_schema_version": feature_schema_version,
    }
    
    # Sort keys for deterministic JSON serialization
    raw = json.dumps(fingerprint_inputs, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
