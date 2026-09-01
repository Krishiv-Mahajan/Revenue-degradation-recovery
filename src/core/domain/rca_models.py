"""
Stage 4 — Root Cause Analysis domain models.

Deterministic, evidence-backed, append-only. No prediction, no automated action,
no revenue-at-risk. See stage4_design.md for full specification.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional

# ---------------------------------------------------------------------------
# Configuration constants (§28 of design)
# ---------------------------------------------------------------------------

HISTORICAL_WEEKS_BACK: int = 4
MIN_VALID_HISTORICAL_WINDOWS: int = 2
MIN_BASELINE_VOLUME: int = 50
MIN_FAILURES_FOR_RCA_EVALUATION: int = 10
MIN_CONTRIBUTION_THRESHOLD: float = 0.30
MIN_ERROR_DEVIATION_THRESHOLD: float = 0.20

STRONG_CONTRIBUTION_THRESHOLD: float = 0.70
STRONG_FAILURE_COUNT_MULTIPLIER: int = 3  # actual_failures >= 3 × MIN_FAILURES
MODERATE_CONTRIBUTION_THRESHOLD: float = 0.40

# Deterministic namespace for UUID5 identity (§23 of design).
# Fixed constant — must never change after the first evaluation is persisted.
NAMESPACE_RCA: uuid.UUID = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# Structural dimensions that can independently drive SEGMENT_SPECIFIC (§8A)
STRUCTURAL_DIMENSIONS: tuple = ("BANK", "CURRENCY", "PAYMENT_METHOD", "WALLET")

# Dimension-to-PaymentEvent-field mapping for structural dimensions
STRUCTURAL_DIMENSION_FIELD_MAP: dict = {
    "BANK": "bank",
    "CURRENCY": "currency",
    "PAYMENT_METHOD": "payment_method",
    "WALLET": "wallet",
}

# Supporting evidence dimensions (§8B) — do NOT create CandidateCause rows
SUPPORTING_DIMENSIONS: tuple = ("ERROR_CODE", "ERROR_SOURCE", "ERROR_STEP")

SUPPORTING_DIMENSION_FIELD_MAP: dict = {
    "ERROR_CODE": "error_code",
    "ERROR_SOURCE": "error_source",
    "ERROR_STEP": "error_step",
}

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RCAClassification(str, Enum):
    SEGMENT_SPECIFIC = "SEGMENT_SPECIFIC"
    SYSTEMIC = "SYSTEMIC"
    UNKNOWN = "UNKNOWN"


class EvidenceStrength(str, Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateCause:
    """
    A structural dimension/value pair that passed all deterministic
    candidate-cause qualification rules (§13 of design).

    CandidateCauses are evidence findings — correlated observations, NOT
    independently proven causal mechanisms. Multiple candidates within the same
    evaluation may be facets of the same underlying incident.
    """

    candidate_id: uuid.UUID          # deterministic UUID5 (§23)
    evaluation_id: uuid.UUID         # FK → RootCauseAnalysisEvaluation (immutable)
    candidate_dimension: str         # e.g., 'BANK'
    candidate_value: str             # e.g., 'HDFC'
    evidence_strength: EvidenceStrength
    excess_failure_contribution: float
    actual_segment_failures: int
    expected_segment_failures: float  # retained as float (not prematurely rounded)
    excess_segment_failures: float    # retained as float
    rank: int                         # 1-based; 1 = highest contribution


@dataclass(frozen=True)
class RootCauseAnalysisEvaluation:
    """
    Immutable, versioned snapshot of an RCA evaluation for a degradation episode.
    Append-only — never updated or deleted after insertion.
    """

    evaluation_id: uuid.UUID          # deterministic UUID5 (§23)
    episode_id: uuid.UUID             # FK → DegradationEpisode.episode_id
    evaluation_version: int           # monotonically increasing per episode_id
    classification: RCAClassification
    analysis_window_start: datetime
    analysis_window_end: datetime
    input_fingerprint: str            # SHA-256 fingerprint of RCA input state (§24)
    generated_at: datetime            # wall-clock UTC insertion time (informational)
    evidence_audit_payload: dict      # full reproducibility JSON snapshot (§20)


# ---------------------------------------------------------------------------
# Deterministic identity helpers (§23 of design)
# ---------------------------------------------------------------------------


def make_evaluation_id(episode_id: uuid.UUID, evaluation_version: int) -> uuid.UUID:
    """
    Deterministic UUID5 for an RCA evaluation.
    evaluation_id = uuid5(NAMESPACE_RCA, '{episode_id}:{evaluation_version}')
    """
    return uuid.uuid5(NAMESPACE_RCA, f"{episode_id}:{evaluation_version}")


def make_candidate_id(
    evaluation_id: uuid.UUID,
    candidate_dimension: str,
    candidate_value: str,
) -> uuid.UUID:
    """
    Deterministic UUID5 for a candidate cause row.
    candidate_id = uuid5(NAMESPACE_RCA, '{evaluation_id}:{dimension}:{value}')
    """
    return uuid.uuid5(
        NAMESPACE_RCA,
        f"{evaluation_id}:{candidate_dimension}:{candidate_value}",
    )


# ---------------------------------------------------------------------------
# Input fingerprint (§24 of design)
# ---------------------------------------------------------------------------


def compute_input_fingerprint(
    episode_id: uuid.UUID,
    episode_status: str,
    analysis_window_start: datetime,
    analysis_window_end: datetime,
    episode_actual_failures: int,
    episode_transaction_count: int,
    historical_window_tuples: List[tuple],
) -> str:
    """
    Deterministic SHA-256 fingerprint of the RCA input state.

    Parameters
    ----------
    historical_window_tuples:
        List of (hist_window_start_iso, hist_failures_int, hist_transactions_int,
                  dimension, value) — sorted before hashing.

    Returns
    -------
    Hex-encoded SHA-256 digest string, prefixed with 'sha256:'.
    """
    fingerprint_inputs = {
        "episode_id": str(episode_id),
        "episode_status": episode_status,
        "analysis_window_start": analysis_window_start.isoformat(),
        "analysis_window_end": analysis_window_end.isoformat(),
        "episode_actual_failures": int(episode_actual_failures),
        "episode_transaction_count": int(episode_transaction_count),
        "historical_windows": sorted(
            [
                (
                    str(t[0]),          # hist_window_start ISO string
                    int(t[1]),          # hist_segment_failures
                    int(t[2]),          # hist_segment_transactions
                    str(t[3]),          # dimension
                    str(t[4]),          # value
                )
                for t in historical_window_tuples
            ]
        ),
    }
    raw = json.dumps(fingerprint_inputs, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
