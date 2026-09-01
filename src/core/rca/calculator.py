"""
Stage 4 — Root Cause Analysis calculator.

Pure, deterministic functions with no I/O. All RCA mathematics live here.
No prediction, no automated action, no revenue-at-risk. See stage4_design.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from src.core.domain.rca_models import (
    HISTORICAL_WEEKS_BACK,
    MIN_BASELINE_VOLUME,
    MIN_CONTRIBUTION_THRESHOLD,
    MIN_ERROR_DEVIATION_THRESHOLD,
    MIN_FAILURES_FOR_RCA_EVALUATION,
    MIN_VALID_HISTORICAL_WINDOWS,
    MODERATE_CONTRIBUTION_THRESHOLD,
    NAMESPACE_RCA,
    STRONG_CONTRIBUTION_THRESHOLD,
    STRONG_FAILURE_COUNT_MULTIPLIER,
    STRUCTURAL_DIMENSIONS,
    SUPPORTING_DIMENSIONS,
    CandidateCause,
    EvidenceStrength,
    RCAClassification,
    RootCauseAnalysisEvaluation,
    compute_input_fingerprint,
    make_candidate_id,
    make_evaluation_id,
)


# ---------------------------------------------------------------------------
# §6  Analysis window computation
# ---------------------------------------------------------------------------

WINDOW_MINUTES = 5  # Stage 2 tumbling-window width (must match Stage 2)


def compute_analysis_window(
    started_at_window: datetime,
    ended_at_window: Optional[datetime],
    latest_bad_window_start: Optional[datetime],
    episode_status: str,
) -> Tuple[datetime, datetime]:
    """
    Derive the [analysis_window_start, analysis_window_end) for an episode.

    RECOVERED  → [started_at_window, ended_at_window)
    ACTIVE     → [started_at_window, latest_bad_window_start + 5min)
    INVALIDATED → caller must not call this; returns the started window only.
    """
    analysis_start = started_at_window
    if episode_status == "RECOVERED":
        if ended_at_window is None:
            raise ValueError("RECOVERED episode has no ended_at_window")
        analysis_end = ended_at_window
    elif episode_status == "ACTIVE":
        if latest_bad_window_start is None:
            raise ValueError("ACTIVE episode has no latest_bad_window_start")
        analysis_end = latest_bad_window_start + timedelta(minutes=WINDOW_MINUTES)
    else:
        # INVALIDATED — produce a minimal window; RCA will immediately classify UNKNOWN
        analysis_end = started_at_window + timedelta(minutes=WINDOW_MINUTES)
    return analysis_start, analysis_end


# ---------------------------------------------------------------------------
# §7A  Historical comparison window selection
# ---------------------------------------------------------------------------

def get_historical_window_starts(
    analysis_window_start: datetime,
    analysis_window_end: datetime,
    weeks_back: int = HISTORICAL_WEEKS_BACK,
) -> Dict[datetime, List[datetime]]:
    """
    For every 5-minute tumbling window [t, t+5min) within the analysis window,
    return the N historical anchor starts matching the same weekday/time-of-day.

    Returns
    -------
    Dict mapping each analysis-window slot start → list of historical starts.
    Matches Stage 2 logic in analytics_repository.get_historical_snapshots().
    """
    result: Dict[datetime, List[datetime]] = {}
    current = analysis_window_start
    while current < analysis_window_end:
        hist_starts = [
            current - timedelta(weeks=i) for i in range(1, weeks_back + 1)
        ]
        result[current] = hist_starts
        current += timedelta(minutes=WINDOW_MINUTES)
    return result


def get_all_historical_starts(
    analysis_window_start: datetime,
    analysis_window_end: datetime,
    weeks_back: int = HISTORICAL_WEEKS_BACK,
) -> List[datetime]:
    """Flat list of all unique historical window starts across the analysis window."""
    slots = get_historical_window_starts(analysis_window_start, analysis_window_end, weeks_back)
    seen = set()
    result = []
    for starts in slots.values():
        for s in starts:
            if s not in seen:
                seen.add(s)
                result.append(s)
    return result


# ---------------------------------------------------------------------------
# §7B–7D  Volume-weighted historical baseline aggregation
# ---------------------------------------------------------------------------

def compute_volume_weighted_failure_rate(
    hist_failures_list: List[int],
    hist_transactions_list: List[int],
) -> Optional[float]:
    """
    Volume-weighted historical segment failure rate.

        historical_segment_failure_rate =
            SUM(historical_segment_failures)
            / SUM(historical_segment_transactions)

    Returns None if the result is INSUFFICIENT (zero transactions or no data).
    """
    total_failures = sum(hist_failures_list)
    total_transactions = sum(hist_transactions_list)
    if total_transactions == 0:
        return None  # INSUFFICIENT
    return total_failures / total_transactions


# ---------------------------------------------------------------------------
# §9  Per-candidate segment-level mathematics
# ---------------------------------------------------------------------------

def compute_candidate_metrics(
    episode_segment_transactions: int,
    actual_segment_failures: int,
    historical_segment_failure_rate: float,
) -> Tuple[float, float]:
    """
    Compute expected and excess segment failures.

    Returns (expected_segment_failures, excess_segment_failures) as floats.
    Values are NOT rounded — precision is retained for all downstream math.
    """
    expected = episode_segment_transactions * historical_segment_failure_rate
    excess = actual_segment_failures - expected
    return expected, excess


# ---------------------------------------------------------------------------
# §11  Episode-level excess failure aggregation + guard
# ---------------------------------------------------------------------------

def compute_episode_total_excess_failures(
    excess_values: List[float],
) -> float:
    """
    episode_total_excess_failures = SUM(max(excess, 0) for all candidates).
    Only positive excess values contribute.
    """
    return sum(v for v in excess_values if v > 0)


# ---------------------------------------------------------------------------
# §10, §13  Excess failure contribution
# ---------------------------------------------------------------------------

def compute_excess_failure_contribution(
    excess_segment_failures: float,
    episode_total_excess_failures: float,
) -> Optional[float]:
    """
    excess_failure_contribution = excess_segment_failures / episode_total_excess_failures.

    Returns None if episode_total_excess_failures <= 0 (guard: §11B).
    Only call this when excess_segment_failures > 0.
    """
    if episode_total_excess_failures <= 0:
        return None
    return excess_segment_failures / episode_total_excess_failures


# ---------------------------------------------------------------------------
# §16  Evidence strength assignment
# ---------------------------------------------------------------------------

def assign_evidence_strength(
    excess_failure_contribution: float,
    actual_segment_failures: int,
) -> EvidenceStrength:
    """
    Deterministic evidence strength based solely on contribution + failure count.

    STRONG   : contribution >= 0.70 AND failures >= 3 × MIN_FAILURES
    MODERATE : contribution >= 0.40
    WEAK     : contribution >= MIN_CONTRIBUTION_THRESHOLD (0.30)
    """
    if (
        excess_failure_contribution >= STRONG_CONTRIBUTION_THRESHOLD
        and actual_segment_failures >= STRONG_FAILURE_COUNT_MULTIPLIER * MIN_FAILURES_FOR_RCA_EVALUATION
    ):
        return EvidenceStrength.STRONG
    if excess_failure_contribution >= MODERATE_CONTRIBUTION_THRESHOLD:
        return EvidenceStrength.MODERATE
    return EvidenceStrength.WEAK


# ---------------------------------------------------------------------------
# §17  Candidate ranking
# ---------------------------------------------------------------------------

def rank_candidates(
    unranked: List[dict],
) -> List[dict]:
    """
    Sort candidates deterministically.

    Primary   : excess_failure_contribution DESC
    Secondary : actual_segment_failures DESC
    Tertiary  : dimension ASC (alphabetically)
    Quaternary: value ASC (alphabetically)

    Assigns 1-based `rank` field.
    Dict keys expected: 'dimension', 'value', 'excess_failure_contribution',
    'actual_segment_failures'.
    """
    sorted_candidates = sorted(
        unranked,
        key=lambda c: (
            -c["excess_failure_contribution"],
            -c["actual_segment_failures"],
            c["dimension"],
            c["value"],
        ),
    )
    for i, c in enumerate(sorted_candidates):
        c["rank"] = i + 1
    return sorted_candidates


# ---------------------------------------------------------------------------
# §12  Error evidence deviation analysis (supporting dimensions only)
# ---------------------------------------------------------------------------

def compute_error_deviation(
    episode_error_count: int,
    episode_total_failures: int,
    historical_error_count: int,
    historical_total_failures: int,
) -> Optional[float]:
    """
    error_deviation = episode_error_frequency - baseline_error_frequency.

    Returns None if either denominator is zero (can't compute frequency).
    """
    if episode_total_failures == 0:
        return None
    episode_freq = episode_error_count / episode_total_failures

    if historical_total_failures == 0:
        baseline_freq = 0.0
    else:
        baseline_freq = historical_error_count / historical_total_failures

    return episode_freq - baseline_freq


# ---------------------------------------------------------------------------
# Audit payload builder (§20 of design)
# ---------------------------------------------------------------------------

def build_audit_payload(
    *,
    analysis_window_start: datetime,
    analysis_window_end: datetime,
    historical_comparison_windows: List[dict],
    episode_id: str,
    segment_dimension: str,
    segment_value: str,
    episode_status: str,
    episode_transaction_count: int,
    episode_actual_failures: int,
    episode_historical_baseline_failure_rate: Optional[float],
    episode_expected_failures: float,
    episode_total_excess_failures: float,
    structural_candidates_evaluated: List[dict],
    supporting_error_evidence: List[dict],
    classification: RCAClassification,
    classification_reason: str,
    overlap_note: Optional[str],
    input_fingerprint: str,
) -> dict:
    """
    Build the immutable, reproducibility-contract JSON payload (§20).
    Stored verbatim on the RootCauseAnalysisEvaluation row.
    """
    payload = {
        "analysis_window": {
            "start": analysis_window_start.isoformat(),
            "end": analysis_window_end.isoformat(),
        },
        "historical_comparison_windows": historical_comparison_windows,
        "episode": {
            "episode_id": episode_id,
            "segment_dimension": segment_dimension,
            "segment_value": segment_value,
            "status": episode_status,
        },
        "episode_totals": {
            "transaction_count": episode_transaction_count,
            "actual_failures": episode_actual_failures,
            "historical_baseline_failure_rate": episode_historical_baseline_failure_rate,
            "expected_failures": episode_expected_failures,
            "episode_total_excess_failures": episode_total_excess_failures,
        },
        "thresholds": {
            "MIN_FAILURES_FOR_RCA_EVALUATION": MIN_FAILURES_FOR_RCA_EVALUATION,
            "MIN_CONTRIBUTION_THRESHOLD": MIN_CONTRIBUTION_THRESHOLD,
            "MIN_ERROR_DEVIATION_THRESHOLD": MIN_ERROR_DEVIATION_THRESHOLD,
            "MIN_BASELINE_VOLUME": MIN_BASELINE_VOLUME,
            "HISTORICAL_WEEKS_BACK": HISTORICAL_WEEKS_BACK,
            "MIN_VALID_HISTORICAL_WINDOWS": MIN_VALID_HISTORICAL_WINDOWS,
        },
        "structural_candidates_evaluated": structural_candidates_evaluated,
        "supporting_error_evidence": supporting_error_evidence,
        "classification": classification.value,
        "classification_reason": classification_reason,
        "input_fingerprint": input_fingerprint,
    }
    if overlap_note:
        payload["overlap_note"] = overlap_note
    return payload


# ---------------------------------------------------------------------------
# §11B  UNKNOWN shortcut payload
# ---------------------------------------------------------------------------

def build_unknown_payload(
    reason: str,
    analysis_window_start: datetime,
    analysis_window_end: datetime,
    episode_id: str,
    segment_dimension: str,
    segment_value: str,
    episode_status: str,
    input_fingerprint: str,
) -> dict:
    """Minimal audit payload for UNKNOWN evaluations."""
    return {
        "analysis_window": {
            "start": analysis_window_start.isoformat(),
            "end": analysis_window_end.isoformat(),
        },
        "episode": {
            "episode_id": episode_id,
            "segment_dimension": segment_dimension,
            "segment_value": segment_value,
            "status": episode_status,
        },
        "classification": RCAClassification.UNKNOWN.value,
        "classification_reason": reason,
        "structural_candidates_evaluated": [],
        "supporting_error_evidence": [],
        "input_fingerprint": input_fingerprint,
        "thresholds": {
            "MIN_FAILURES_FOR_RCA_EVALUATION": MIN_FAILURES_FOR_RCA_EVALUATION,
            "MIN_CONTRIBUTION_THRESHOLD": MIN_CONTRIBUTION_THRESHOLD,
            "MIN_ERROR_DEVIATION_THRESHOLD": MIN_ERROR_DEVIATION_THRESHOLD,
            "MIN_BASELINE_VOLUME": MIN_BASELINE_VOLUME,
            "HISTORICAL_WEEKS_BACK": HISTORICAL_WEEKS_BACK,
            "MIN_VALID_HISTORICAL_WINDOWS": MIN_VALID_HISTORICAL_WINDOWS,
        },
    }
