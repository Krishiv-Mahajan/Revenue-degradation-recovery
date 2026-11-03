"""
Live Stage 5 model selection.

The live pipeline uses the deterministic heuristic unless an ML artifact is explicitly configured AND
passes every check below. Any failed check falls back to the heuristic and the reason is recorded in the
selection (and logged), so a misconfigured artifact can never silently change predictions.

Configuration (environment):
- STAGE5_MODEL_MODE: "heuristic" (default) or "artifact".
- STAGE5_MODEL_PATH: path of a joblib artifact (artifact mode).
- STAGE5_MODEL_SHA256: required hex SHA-256 of that file. joblib executes pickled code on load, so an
  unpinned or modified file is refused before it is ever loaded.

Artifact acceptance:
- implements FailurePredictionModel and a v1.0.0 feature schema (the live feature reconstruction);
- carries training_metadata with metrics_computed true and held-out metrics at or better than
  MIN_HOLDOUT_ROC_AUC and MAX_HOLDOUT_BRIER_SCORE.
Passing these checks makes a model eligible to predict; it does not make it empirically validated.
Stage 8 still weighs it by provenance (VERIFIED_EMPIRICAL_MODELS), unchanged.
"""
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import joblib

from src.core.ml.model import FailurePredictionModel

logger = logging.getLogger(__name__)

MODE_ENV = "STAGE5_MODEL_MODE"
PATH_ENV = "STAGE5_MODEL_PATH"
SHA256_ENV = "STAGE5_MODEL_SHA256"
HEURISTIC_MODE = "heuristic"
ARTIFACT_MODE = "artifact"

MIN_HOLDOUT_ROC_AUC = 0.60
MAX_HOLDOUT_BRIER_SCORE = 0.25
LIVE_FEATURE_SCHEMA_PREFIX = "v1.0.0"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ModelSelection:
    model: FailurePredictionModel
    requested_mode: str
    selected: str
    reason: str
    validation: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "selected": self.selected,
            "model_name": self.model.model_name,
            "model_version": self.model.model_version,
            "reason": self.reason,
            "validation": self.validation,
        }


_artifact_cache: Dict[Tuple[str, str], FailurePredictionModel] = {}


def select_live_model(
    heuristic: Callable[[], FailurePredictionModel],
    env: Optional[Mapping[str, str]] = None,
    loader: Callable[[str], Any] = joblib.load,
) -> ModelSelection:
    """Choose the live Stage 5 model; never raises for configuration problems (falls back instead)."""
    env = os.environ if env is None else env
    mode = (env.get(MODE_ENV) or HEURISTIC_MODE).strip().lower()

    if mode == HEURISTIC_MODE:
        return ModelSelection(heuristic(), mode, HEURISTIC_MODE, "heuristic configured")
    if mode != ARTIFACT_MODE:
        return _fallback(heuristic, mode, f"unknown {MODE_ENV} {mode!r}")

    path = (env.get(PATH_ENV) or "").strip()
    expected_sha = (env.get(SHA256_ENV) or "").strip().lower()
    if not path:
        return _fallback(heuristic, mode, f"{PATH_ENV} is not set")
    if not _SHA256_PATTERN.match(expected_sha):
        return _fallback(heuristic, mode, f"{SHA256_ENV} must pin the artifact (64 hex characters)")
    if not os.path.isfile(path):
        return _fallback(heuristic, mode, "artifact file not found")

    actual_sha = _sha256(path)
    if actual_sha != expected_sha:
        return _fallback(heuristic, mode, "artifact SHA-256 does not match the pinned value")

    key = (os.path.abspath(path), actual_sha)
    model = _artifact_cache.get(key)
    if model is None:
        try:
            model = loader(path)
        except Exception as exc:  # a corrupt artifact must not break the pipeline
            return _fallback(heuristic, mode, f"artifact could not be loaded ({type(exc).__name__})")

    accepted, reason, validation = validate_artifact(model)
    if not accepted:
        return _fallback(heuristic, mode, reason, validation)
    _artifact_cache[key] = model
    return ModelSelection(model, mode, ARTIFACT_MODE, "artifact passed validation", validation)


def validate_artifact(model: Any) -> Tuple[bool, str, Dict[str, Any]]:
    """Whether a loaded artifact may predict live, with the metrics that decided it."""
    if not isinstance(model, FailurePredictionModel):
        return False, "artifact does not implement FailurePredictionModel", {}
    schema = model.get_feature_schema_version()
    if not isinstance(schema, str) or not schema.startswith(LIVE_FEATURE_SCHEMA_PREFIX):
        return False, f"feature schema {schema!r} is not compatible with {LIVE_FEATURE_SCHEMA_PREFIX}", {}

    metadata = getattr(model, "training_metadata", None) or {}
    metrics = metadata.get("metrics") or {}
    validation = {
        "metrics_computed": metadata.get("metrics_computed") is True,
        "roc_auc": metrics.get("roc_auc"),
        "brier_score": metrics.get("brier_score"),
        "min_roc_auc": MIN_HOLDOUT_ROC_AUC,
        "max_brier_score": MAX_HOLDOUT_BRIER_SCORE,
    }
    if not validation["metrics_computed"]:
        return False, "artifact has no computed held-out metrics", validation
    roc_auc, brier = validation["roc_auc"], validation["brier_score"]
    if not isinstance(roc_auc, (int, float)) or roc_auc < MIN_HOLDOUT_ROC_AUC:
        return False, f"held-out ROC AUC {roc_auc} is below {MIN_HOLDOUT_ROC_AUC}", validation
    if not isinstance(brier, (int, float)) or brier > MAX_HOLDOUT_BRIER_SCORE:
        return False, f"held-out Brier score {brier} is above {MAX_HOLDOUT_BRIER_SCORE}", validation
    return True, "accepted", validation


def _fallback(heuristic, mode: str, reason: str, validation: Optional[Dict[str, Any]] = None) -> ModelSelection:
    logger.warning(f"[Stage5ModelSelection] falling back to the heuristic: {reason}")
    return ModelSelection(heuristic(), mode, HEURISTIC_MODE, f"fallback: {reason}", validation or {})


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
