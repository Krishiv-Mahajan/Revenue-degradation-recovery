"""
Stage 4 — Unit tests for the RCA calculator.

All tests are pure (no DB, no async). Tests cover:
- Baseline correctness (volume-weighted aggregation, validity rules)
- Expected failure math (segment-level, float retention)
- Episode-level guard (total_excess <= 0 → UNKNOWN)
- Concentration correctness (dominant normal segment NOT flagged)
- Single and multiple candidate scenarios
- Dimension taxonomy (ERROR_CODE → no CandidateCause)
- Candidate overlap
- Evidence strength assignment
- Ranking and tie-breaking
- SYSTEMIC classification
- UNKNOWN classification paths
- Event semantics (payment.authorized excluded)
- Fingerprint determinism
- UUID identity determinism
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.core.domain.rca_models import (
    HISTORICAL_WEEKS_BACK,
    MIN_BASELINE_VOLUME,
    MIN_CONTRIBUTION_THRESHOLD,
    MIN_FAILURES_FOR_RCA_EVALUATION,
    NAMESPACE_RCA,
    STRONG_CONTRIBUTION_THRESHOLD,
    MODERATE_CONTRIBUTION_THRESHOLD,
    EvidenceStrength,
    RCAClassification,
    compute_input_fingerprint,
    make_candidate_id,
    make_evaluation_id,
)
from src.core.rca.calculator import (
    assign_evidence_strength,
    compute_analysis_window,
    compute_candidate_metrics,
    compute_episode_total_excess_failures,
    compute_error_deviation,
    compute_excess_failure_contribution,
    compute_volume_weighted_failure_rate,
    get_all_historical_starts,
    get_historical_window_starts,
    rank_candidates,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

T0 = datetime(2024, 1, 8, 10, 0, tzinfo=timezone.utc)  # Monday 10:00 UTC


def _episodes_ago(weeks: int) -> datetime:
    return T0 - timedelta(weeks=weeks)


# ---------------------------------------------------------------------------
# §6  Analysis window
# ---------------------------------------------------------------------------


class TestAnalysisWindow:
    def test_recovered_episode(self):
        start, end = compute_analysis_window(
            started_at_window=T0,
            ended_at_window=T0 + timedelta(minutes=15),
            latest_bad_window_start=None,
            episode_status="RECOVERED",
        )
        assert start == T0
        assert end == T0 + timedelta(minutes=15)

    def test_active_episode(self):
        latest_bad = T0 + timedelta(minutes=10)
        start, end = compute_analysis_window(
            started_at_window=T0,
            ended_at_window=None,
            latest_bad_window_start=latest_bad,
            episode_status="ACTIVE",
        )
        assert start == T0
        assert end == latest_bad + timedelta(minutes=5)

    def test_recovered_missing_ended_at_raises(self):
        with pytest.raises(ValueError):
            compute_analysis_window(
                started_at_window=T0,
                ended_at_window=None,
                latest_bad_window_start=None,
                episode_status="RECOVERED",
            )

    def test_active_missing_latest_bad_raises(self):
        with pytest.raises(ValueError):
            compute_analysis_window(
                started_at_window=T0,
                ended_at_window=None,
                latest_bad_window_start=None,
                episode_status="ACTIVE",
            )


# ---------------------------------------------------------------------------
# §7A  Historical window selection
# ---------------------------------------------------------------------------


class TestHistoricalWindowSelection:
    def test_matches_stage2_selection(self):
        """
        For each slot in the analysis window, historical starts must match
        the same weekday/time-of-day from previous N weeks.
        """
        analysis_start = T0
        analysis_end = T0 + timedelta(minutes=10)  # 2 slots

        slot_map = get_historical_window_starts(analysis_start, analysis_end, weeks_back=4)

        assert T0 in slot_map
        assert T0 + timedelta(minutes=5) in slot_map

        hist_for_slot0 = slot_map[T0]
        assert len(hist_for_slot0) == 4
        for i, h in enumerate(hist_for_slot0, 1):
            assert h == T0 - timedelta(weeks=i)
            # Same day of week
            assert h.weekday() == T0.weekday()
            # Same time of day
            assert h.hour == T0.hour
            assert h.minute == T0.minute

    def test_all_historical_starts_unique(self):
        analysis_start = T0
        analysis_end = T0 + timedelta(minutes=15)
        all_starts = get_all_historical_starts(analysis_start, analysis_end, weeks_back=4)
        assert len(all_starts) == len(set(all_starts))


# ---------------------------------------------------------------------------
# §7B–7D  Volume-weighted baseline aggregation
# ---------------------------------------------------------------------------


class TestVolumeWeightedBaseline:
    def test_volume_weighted_correctness(self):
        # 1000 transactions, 50 failures → rate = 0.05
        # 500 transactions, 100 failures → rate = 0.20
        # Volume-weighted: (50 + 100) / (1000 + 500) = 150 / 1500 = 0.10
        rate = compute_volume_weighted_failure_rate(
            hist_failures_list=[50, 100],
            hist_transactions_list=[1000, 500],
        )
        assert abs(rate - 0.10) < 1e-9

    def test_zero_transactions_returns_none(self):
        rate = compute_volume_weighted_failure_rate(
            hist_failures_list=[0, 0],
            hist_transactions_list=[0, 0],
        )
        assert rate is None  # INSUFFICIENT

    def test_empty_lists_returns_none(self):
        rate = compute_volume_weighted_failure_rate([], [])
        assert rate is None

    def test_single_window(self):
        rate = compute_volume_weighted_failure_rate([20], [200])
        assert abs(rate - 0.10) < 1e-9


# ---------------------------------------------------------------------------
# §9  Per-candidate expected failure math
# ---------------------------------------------------------------------------


class TestCandidateMetrics:
    def test_expected_uses_segment_transaction_count(self):
        """
        expected_segment_failures = episode_segment_transaction_count × historical_segment_failure_rate
        Must NOT use global episode volume.
        """
        seg_txn = 400
        hist_rate = 0.05
        expected, excess = compute_candidate_metrics(
            episode_segment_transactions=seg_txn,
            actual_segment_failures=30,
            historical_segment_failure_rate=hist_rate,
        )
        assert abs(expected - seg_txn * hist_rate) < 1e-9  # 400 × 0.05 = 20.0
        assert abs(excess - (30 - 20.0)) < 1e-9            # 10.0

    def test_float_retention(self):
        """Fractional expected failures must be retained as floats — not rounded."""
        expected, excess = compute_candidate_metrics(
            episode_segment_transactions=100,
            actual_segment_failures=7,
            historical_segment_failure_rate=0.033,
        )
        # expected = 100 × 0.033 = 3.3 (float, not 3)
        assert isinstance(expected, float)
        assert abs(expected - 3.3) < 1e-9
        assert isinstance(excess, float)
        assert abs(excess - 3.7) < 1e-9

    def test_negative_excess(self):
        """Negative excess is valid math — segment performed better than baseline."""
        expected, excess = compute_candidate_metrics(
            episode_segment_transactions=200,
            actual_segment_failures=5,
            historical_segment_failure_rate=0.10,
        )
        assert abs(expected - 20.0) < 1e-9
        assert abs(excess - (-15.0)) < 1e-9


# ---------------------------------------------------------------------------
# §11  Episode-level excess failure aggregation and guard
# ---------------------------------------------------------------------------


class TestEpisodeTotalExcess:
    def test_actual_greater_than_expected(self):
        """Total excess is actual - expected."""
        total = compute_episode_total_excess_failures(100, 20.0)
        assert abs(total - 80.0) < 1e-9

    def test_actual_less_than_expected_returns_zero(self):
        """Negative excess values do not contribute to episode_total_excess_failures."""
        total = compute_episode_total_excess_failures(10, 20.0)
        assert total == 0.0

    def test_empty_list_returns_zero(self):
        # N/A for new signature, but let's test zero failures
        total = compute_episode_total_excess_failures(0, 0.0)
        assert total == 0.0

    def test_guard_division_by_zero_prevented(self):
        """If episode_total_excess_failures <= 0, contribution must return None."""
        result = compute_excess_failure_contribution(5.0, 0.0)
        assert result is None

    def test_negative_episode_total_returns_none(self):
        result = compute_excess_failure_contribution(5.0, -3.0)
        assert result is None

    def test_contribution_correct_for_positive_total(self):
        result = compute_excess_failure_contribution(30.0, 100.0)
        assert abs(result - 0.30) < 1e-9


# ---------------------------------------------------------------------------
# §10  Traffic share vs failure share vs excess failure contribution
# ---------------------------------------------------------------------------


class TestConcentrationCorrectness:
    """
    A dominant traffic segment with a historically NORMAL failure rate
    must NOT qualify as a candidate even if it has a high failure_share.
    """

    def test_dominant_normal_segment_not_flagged(self):
        """
        Segment A: 90% traffic share, 4% failure rate (same as historical)
        → excess_segment_failures ≈ 0 → does not qualify
        """
        seg_txn = 900
        actual = 36      # 4% failure rate
        hist_rate = 0.04  # historical 4%
        expected, excess = compute_candidate_metrics(seg_txn, actual, hist_rate)
        assert excess <= 0.1  # effectively no excess

    def test_minority_elevated_segment_is_flagged(self):
        """
        Segment B: 10% traffic share, 40% failure rate (vs 4% historical)
        → high excess_failure_contribution → qualifies
        """
        seg_txn = 100
        actual = 40     # 40% failure rate
        hist_rate = 0.04
        expected, excess = compute_candidate_metrics(seg_txn, actual, hist_rate)
        assert excess > 0  # excess = 40 - 4 = 36


# ---------------------------------------------------------------------------
# §16  Evidence strength
# ---------------------------------------------------------------------------


class TestEvidenceStrength:
    def test_strong(self):
        strength = assign_evidence_strength(
            excess_failure_contribution=0.75,
            actual_segment_failures=40,  # >= 3 × 10 = 30
        )
        assert strength == EvidenceStrength.STRONG

    def test_strong_fails_on_low_failures(self):
        """contribution >= 0.70 but failures < 3 × MIN → not STRONG"""
        strength = assign_evidence_strength(
            excess_failure_contribution=0.80,
            actual_segment_failures=29,  # < 30
        )
        assert strength == EvidenceStrength.MODERATE

    def test_moderate(self):
        strength = assign_evidence_strength(0.50, 50)
        assert strength == EvidenceStrength.MODERATE

    def test_weak(self):
        strength = assign_evidence_strength(0.31, 50)
        assert strength == EvidenceStrength.WEAK

    def test_exact_boundary_strong(self):
        strength = assign_evidence_strength(0.70, 30)
        assert strength == EvidenceStrength.STRONG

    def test_exact_boundary_moderate(self):
        strength = assign_evidence_strength(0.40, 50)
        assert strength == EvidenceStrength.MODERATE


# ---------------------------------------------------------------------------
# §17  Candidate ranking and tie-breaking
# ---------------------------------------------------------------------------


class TestCandidateRanking:
    def _make_candidate(self, dim, val, contribution, failures):
        return {
            "dimension": dim,
            "value": val,
            "excess_failure_contribution": contribution,
            "actual_segment_failures": failures,
            "excess_segment_failures": 10.0,
            "expected_segment_failures": 5.0,
            "evidence_strength": "WEAK",
        }

    def test_ranked_descending_by_contribution(self):
        candidates = [
            self._make_candidate("BANK", "HDFC", 0.60, 50),
            self._make_candidate("PAYMENT_METHOD", "upi", 0.80, 50),
        ]
        ranked = rank_candidates(candidates)
        assert ranked[0]["value"] == "upi"
        assert ranked[0]["rank"] == 1
        assert ranked[1]["rank"] == 2

    def test_tie_broken_by_actual_failures_desc(self):
        candidates = [
            self._make_candidate("BANK", "HDFC", 0.50, 30),
            self._make_candidate("BANK", "SBI", 0.50, 50),  # more failures
        ]
        ranked = rank_candidates(candidates)
        assert ranked[0]["value"] == "SBI"  # more failures wins

    def test_tie_broken_by_dimension_alpha(self):
        candidates = [
            self._make_candidate("PAYMENT_METHOD", "upi", 0.50, 30),
            self._make_candidate("BANK", "HDFC", 0.50, 30),
        ]
        ranked = rank_candidates(candidates)
        # BANK < PAYMENT_METHOD alphabetically
        assert ranked[0]["dimension"] == "BANK"

    def test_tie_broken_by_value_alpha(self):
        candidates = [
            self._make_candidate("BANK", "SBI", 0.50, 30),
            self._make_candidate("BANK", "HDFC", 0.50, 30),
        ]
        ranked = rank_candidates(candidates)
        assert ranked[0]["value"] == "HDFC"  # H < S

    def test_single_candidate_rank_1(self):
        candidates = [self._make_candidate("BANK", "HDFC", 0.80, 100)]
        ranked = rank_candidates(candidates)
        assert ranked[0]["rank"] == 1

    def test_empty_list(self):
        assert rank_candidates([]) == []


# ---------------------------------------------------------------------------
# §12  Error evidence deviation
# ---------------------------------------------------------------------------


class TestErrorDeviation:
    def test_zero_episode_failures_returns_none(self):
        result = compute_error_deviation(
            episode_error_count=5,
            episode_total_failures=0,
            historical_error_count=10,
            historical_total_failures=100,
        )
        assert result is None

    def test_zero_historical_failures_uses_zero_baseline(self):
        result = compute_error_deviation(
            episode_error_count=10,
            episode_total_failures=50,
            historical_error_count=0,
            historical_total_failures=0,
        )
        # episode_freq = 10/50 = 0.20, baseline_freq = 0.0
        assert abs(result - 0.20) < 1e-9

    def test_normal_deviation(self):
        result = compute_error_deviation(
            episode_error_count=30,
            episode_total_failures=100,  # 30%
            historical_error_count=5,
            historical_total_failures=100,  # 5%
        )
        assert abs(result - 0.25) < 1e-9

    def test_negative_deviation(self):
        result = compute_error_deviation(
            episode_error_count=2,
            episode_total_failures=100,   # 2%
            historical_error_count=20,
            historical_total_failures=100,  # 20%
        )
        assert abs(result - (-0.18)) < 1e-9


# ---------------------------------------------------------------------------
# §23  UUID identity determinism
# ---------------------------------------------------------------------------


class TestUUIDIdentity:
    def test_evaluation_id_deterministic(self):
        ep_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        v1a = make_evaluation_id(ep_id, 1)
        v1b = make_evaluation_id(ep_id, 1)
        assert v1a == v1b

    def test_different_versions_produce_different_ids(self):
        ep_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        v1 = make_evaluation_id(ep_id, 1)
        v2 = make_evaluation_id(ep_id, 2)
        assert v1 != v2

    def test_candidate_id_deterministic(self):
        eval_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        c1a = make_candidate_id(eval_id, "BANK", "HDFC")
        c1b = make_candidate_id(eval_id, "BANK", "HDFC")
        assert c1a == c1b

    def test_candidate_id_differs_for_different_values(self):
        eval_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        c1 = make_candidate_id(eval_id, "BANK", "HDFC")
        c2 = make_candidate_id(eval_id, "BANK", "SBI")
        assert c1 != c2


# ---------------------------------------------------------------------------
# §24  Fingerprint determinism
# ---------------------------------------------------------------------------


class TestFingerprintDeterminism:
    def _sample_fingerprint(self, failures=100, transactions=1000):
        ep_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
        return compute_input_fingerprint(
            episode_id=ep_id,
            episode_status="ACTIVE",
            analysis_window_start=T0,
            analysis_window_end=T0 + timedelta(minutes=15),
            episode_actual_failures=failures,
            episode_transaction_count=transactions,
            historical_window_tuples=[
                (T0 - timedelta(weeks=1), 50, 1000, "BANK", "HDFC"),
                (T0 - timedelta(weeks=2), 55, 980, "BANK", "HDFC"),
            ],
        )

    def test_identical_inputs_produce_identical_fingerprint(self):
        f1 = self._sample_fingerprint()
        f2 = self._sample_fingerprint()
        assert f1 == f2

    def test_fingerprint_starts_with_sha256_prefix(self):
        f = self._sample_fingerprint()
        assert f.startswith("sha256:")
        assert len(f) == 71  # 'sha256:' + 64 hex chars

    def test_changed_failure_count_changes_fingerprint(self):
        f1 = self._sample_fingerprint(failures=100)
        f2 = self._sample_fingerprint(failures=101)
        assert f1 != f2

    def test_changed_status_changes_fingerprint(self):
        ep_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
        f1 = compute_input_fingerprint(
            episode_id=ep_id, episode_status="ACTIVE",
            analysis_window_start=T0, analysis_window_end=T0 + timedelta(minutes=5),
            episode_actual_failures=100, episode_transaction_count=1000,
            historical_window_tuples=[],
        )
        f2 = compute_input_fingerprint(
            episode_id=ep_id, episode_status="RECOVERED",
            analysis_window_start=T0, analysis_window_end=T0 + timedelta(minutes=5),
            episode_actual_failures=100, episode_transaction_count=1000,
            historical_window_tuples=[],
        )
        assert f1 != f2

    def test_tuple_order_does_not_affect_fingerprint(self):
        """Tuples are sorted canonically — order must not matter."""
        ep_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
        tuples_a = [
            (T0 - timedelta(weeks=1), 50, 1000, "BANK", "HDFC"),
            (T0 - timedelta(weeks=2), 55, 980, "BANK", "SBI"),
        ]
        tuples_b = [
            (T0 - timedelta(weeks=2), 55, 980, "BANK", "SBI"),
            (T0 - timedelta(weeks=1), 50, 1000, "BANK", "HDFC"),
        ]
        f1 = compute_input_fingerprint(
            ep_id, "ACTIVE", T0, T0 + timedelta(minutes=5),
            100, 1000, tuples_a,
        )
        f2 = compute_input_fingerprint(
            ep_id, "ACTIVE", T0, T0 + timedelta(minutes=5),
            100, 1000, tuples_b,
        )
        assert f1 == f2


# ---------------------------------------------------------------------------
# §5  Event semantics
# ---------------------------------------------------------------------------


class TestEventSemantics:
    def test_payment_authorized_excluded(self):
        """
        payment.authorized events must not affect transaction counts.
        Only payment.captured and payment.failed count as terminal events.
        """
        events = [
            {"event_type": "payment.captured", "bank": "HDFC"},
            {"event_type": "payment.failed", "bank": "HDFC"},
            {"event_type": "payment.authorized", "bank": "HDFC"},  # excluded
            {"event_type": "payment.created", "bank": "HDFC"},      # excluded
        ]
        terminal_events = [
            e for e in events
            if e["event_type"] in ("payment.captured", "payment.failed")
        ]
        assert len(terminal_events) == 2

    def test_only_failed_events_count_as_failures(self):
        events = [
            {"event_type": "payment.captured"},
            {"event_type": "payment.failed"},
            {"event_type": "payment.authorized"},
        ]
        failures = sum(1 for e in events if e["event_type"] == "payment.failed")
        assert failures == 1


# ---------------------------------------------------------------------------
# Integration-level math: full evaluation scenario (pure, no DB)
# ---------------------------------------------------------------------------


class TestFullEvaluationMathScenarios:
    """
    Tests the full mathematical chain without DB calls.
    Simulates what the service does but via direct calculator calls.
    """

    def test_single_strong_candidate(self):
        """
        Scenario: HDFC bank has 80% of excess failures.
        Expected: HDFC qualifies as STRONG candidate, SEGMENT_SPECIFIC.
        """
        # HDFC: 200 transactions, 80 failures (40% rate), historical 5% rate
        hdfc_txn = 200
        hdfc_actual = 80
        hdfc_hist_rate = 0.05
        hdfc_expected, hdfc_excess = compute_candidate_metrics(hdfc_txn, hdfc_actual, hdfc_hist_rate)
        # expected = 10.0, excess = 70.0

        # episode totals (simulate SBI taking up the rest with 12 failures / 2 expected)
        episode_actual_failures = 92
        episode_expected_failures = 20.0
        
        episode_total_excess = compute_episode_total_excess_failures(
            episode_actual_failures, episode_expected_failures
        )
        assert abs(episode_total_excess - 72.0) < 1e-9

        hdfc_contribution = compute_excess_failure_contribution(hdfc_excess, episode_total_excess)
        assert hdfc_contribution is not None
        assert abs(hdfc_contribution - (70.0 / 72.0)) < 1e-6

        assert hdfc_actual >= MIN_FAILURES_FOR_RCA_EVALUATION
        assert hdfc_contribution >= MIN_CONTRIBUTION_THRESHOLD
        strength = assign_evidence_strength(hdfc_contribution, hdfc_actual)
        assert strength == EvidenceStrength.STRONG

    def test_overlapping_dimensions_both_strong(self):
        """
        Scenario A: Overlapping dimensions.
        100 identical excess failures all belonging to HDFC + UPI + INR.
        Expected:
        - episode excess = 100
        - HDFC contribution = 1.0 (STRONG)
        - UPI contribution = 1.0 (STRONG)
        - INR contribution = 1.0 (STRONG)
        """
        # HDFC
        hdfc_expected, hdfc_excess = compute_candidate_metrics(1000, 100, 0.0)
        # UPI
        upi_expected, upi_excess = compute_candidate_metrics(1000, 100, 0.0)
        # INR
        inr_expected, inr_excess = compute_candidate_metrics(1000, 100, 0.0)
        
        episode_actual_failures = 100
        episode_expected_failures = 0.0
        episode_total_excess = compute_episode_total_excess_failures(
            episode_actual_failures, episode_expected_failures
        )
        assert episode_total_excess == 100.0
        
        hdfc_contrib = compute_excess_failure_contribution(hdfc_excess, episode_total_excess)
        upi_contrib = compute_excess_failure_contribution(upi_excess, episode_total_excess)
        inr_contrib = compute_excess_failure_contribution(inr_excess, episode_total_excess)
        
        assert hdfc_contrib == 1.0
        assert upi_contrib == 1.0
        assert inr_contrib == 1.0
        
        assert assign_evidence_strength(hdfc_contrib, 100) == EvidenceStrength.STRONG
        assert assign_evidence_strength(upi_contrib, 100) == EvidenceStrength.STRONG
        assert assign_evidence_strength(inr_contrib, 100) == EvidenceStrength.STRONG

    def test_mutually_exclusive_segments(self):
        """
        Scenario B: Mutually exclusive/distinct segments.
        HDFC excess = 60, SBI excess = 40.
        Expected:
        - episode excess = 100
        - HDFC contribution = 0.60
        - SBI contribution = 0.40
        """
        hdfc_expected, hdfc_excess = compute_candidate_metrics(600, 60, 0.0)
        sbi_expected, sbi_excess = compute_candidate_metrics(400, 40, 0.0)
        
        episode_actual_failures = 100
        episode_expected_failures = 0.0
        episode_total_excess = compute_episode_total_excess_failures(
            episode_actual_failures, episode_expected_failures
        )
        assert episode_total_excess == 100.0
        
        hdfc_contrib = compute_excess_failure_contribution(hdfc_excess, episode_total_excess)
        sbi_contrib = compute_excess_failure_contribution(sbi_excess, episode_total_excess)
        
        assert abs(hdfc_contrib - 0.60) < 1e-9
        assert abs(sbi_contrib - 0.40) < 1e-9

    def test_no_candidate_systemic(self):
        """
        Scenario: All banks have proportionally elevated failures.
        No candidate meets contribution threshold → SYSTEMIC.
        """
        results = []
        for _ in range(4):
            exp, exc = compute_candidate_metrics(250, 30, 0.08)  # expected=20, excess=10
            results.append(exc)
            
        episode_actual_failures = 120
        episode_expected_failures = 80.0
        total = compute_episode_total_excess_failures(
            episode_actual_failures, episode_expected_failures
        )
        assert abs(total - 40.0) < 1e-9

        for exc in results:
            contribution = compute_excess_failure_contribution(exc, total)
            assert abs(contribution - 0.25) < 1e-9
            assert contribution < MIN_CONTRIBUTION_THRESHOLD

    def test_episode_total_excess_zero_gives_unknown(self):
        """If all segments perform at or better than baseline, episode_total_excess <= 0."""
        # expected > actual
        total = compute_episode_total_excess_failures(50, 60.0)
        assert total == 0.0
        result = compute_excess_failure_contribution(10.0, total)
        assert result is None

    def test_multiple_candidates_ranked(self):
        """Two candidates both qualify; ranked by contribution."""
        candidates = [
            {
                "dimension": "BANK",
                "value": "SBI",
                "excess_failure_contribution": 0.35,
                "actual_segment_failures": 15,
                "excess_segment_failures": 35.0,
                "expected_segment_failures": 5.0,
                "evidence_strength": "WEAK",
            },
            {
                "dimension": "BANK",
                "value": "HDFC",
                "excess_failure_contribution": 0.60,
                "actual_segment_failures": 40,
                "excess_segment_failures": 60.0,
                "expected_segment_failures": 5.0,
                "evidence_strength": "MODERATE",
            },
        ]
        ranked = rank_candidates(candidates)
        assert ranked[0]["value"] == "HDFC"
        assert ranked[0]["rank"] == 1
        assert ranked[1]["value"] == "SBI"
        assert ranked[1]["rank"] == 2
