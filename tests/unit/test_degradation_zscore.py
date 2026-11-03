"""Stage 3 Z-score evidence, the combined detection rule, and the Stage 2 statistics behind them."""
import statistics
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.core.analytics.calculator import calculate_baseline_statistics
from src.core.degradation.signal import evaluate_snapshot
from src.core.degradation.state_machine import reconcile_timeline
from src.core.degradation.zscore import (
    MIN_STDDEV_FLOOR,
    DetectionMode,
    ZScoreStatus,
    compute_z_score,
    confirms_degradation,
    detection_mode,
)
from src.core.domain.analytics_models import PaymentHealthSnapshot
from src.core.domain.degradation_models import SignalType

T = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)


def _snapshot(success_rate, baseline=0.90, mean=0.90, stddev=0.01, n=4, insufficient=False, window=0):
    start = T + timedelta(minutes=5 * window)
    return PaymentHealthSnapshot(
        snapshot_id=uuid.uuid4(), window_start=start, window_end=start + timedelta(minutes=5),
        segment_dimension="GLOBAL", segment_value="ALL", transaction_count=100,
        successful_transaction_count=int((success_rate or 0) * 100), failed_transaction_count=100 - int((success_rate or 0) * 100),
        success_rate=success_rate, failure_rate=None if success_rate is None else 1 - success_rate,
        total_gmv_minor_units=0, successful_gmv_minor_units=0, failed_gmv_minor_units=0,
        baseline_success_rate=baseline, baseline_mean_success_rate=mean, baseline_stddev_success_rate=stddev,
        baseline_sample_count=n, insufficient_volume=insufficient, calculated_at=T,
    )


# --- evidence guards -------------------------------------------------------------------------------

@pytest.mark.parametrize("rate, mean, stddev, n, status", [
    (0.80, None, None, 0, ZScoreStatus.COLD_START),
    (0.80, None, None, None, ZScoreStatus.COLD_START),
    (0.80, 0.90, None, 1, ZScoreStatus.INSUFFICIENT_OBSERVATIONS),
    (0.80, 0.90, 0.05, 2, ZScoreStatus.INSUFFICIENT_OBSERVATIONS),
    (None, 0.90, 0.05, 4, ZScoreStatus.MISSING_RATE),
])
def test_unusable_inputs_give_a_status_and_no_score(rate, mean, stddev, n, status):
    evidence = compute_z_score(rate, mean, stddev, n)
    assert (evidence.z_score, evidence.status, evidence.usable) == (None, status, False)


def test_z_score_uses_the_historical_spread():
    evidence = compute_z_score(0.70, 0.90, 0.05, 4)
    assert (evidence.z_score, evidence.status, evidence.usable) == (pytest.approx(-4.0), ZScoreStatus.OK, True)


def test_low_variance_history_is_floored_instead_of_exploding():
    evidence = compute_z_score(0.89, 0.90, 0.0, 4)
    assert evidence.status is ZScoreStatus.LOW_VARIANCE
    assert evidence.z_score == pytest.approx(-0.01 / MIN_STDDEV_FLOOR)  # -0.5, not -infinity


@pytest.mark.parametrize("z, confirmed", [(-3.0, True), (-5.0, True), (-2.99, False), (1.0, False)])
def test_confirmation_requires_z_at_or_below_minus_three(z, confirmed):
    assert confirms_degradation(compute_z_score(0.90 + z * 0.05, 0.90, 0.05, 4)) is confirmed


@pytest.mark.parametrize("value, mode", [
    (None, DetectionMode.THRESHOLD), ("threshold", DetectionMode.THRESHOLD),
    ("THRESHOLD_AND_ZSCORE", DetectionMode.THRESHOLD_AND_ZSCORE), ("zscore_only", DetectionMode.THRESHOLD),
])
def test_detection_mode_defaults_to_the_frozen_threshold_rule(value, mode):
    env = {} if value is None else {"DEGRADATION_DETECTION_MODE": value}
    assert detection_mode(env) is mode


# --- signal evaluation -----------------------------------------------------------------------------

def test_default_mode_keeps_the_threshold_verdict_and_records_evidence():
    # A 10-point drop is BAD by threshold; in a volatile segment (stddev 0.08) it is only z = -1.25.
    signal = evaluate_snapshot(_snapshot(0.80, stddev=0.08), 1, T)
    assert signal.signal_type is SignalType.BAD
    assert (signal.z_score, signal.z_score_status) == (pytest.approx(-1.25), "OK")


def test_combined_mode_suppresses_a_threshold_false_positive_in_a_volatile_segment():
    signal = evaluate_snapshot(_snapshot(0.80, stddev=0.08), 1, T, DetectionMode.THRESHOLD_AND_ZSCORE)
    assert signal.signal_type is SignalType.NORMAL
    assert signal.absolute_drop == pytest.approx(0.10)  # the drop itself is still recorded


def test_combined_mode_keeps_a_statistically_confirmed_degradation():
    signal = evaluate_snapshot(_snapshot(0.60, stddev=0.02), 1, T, DetectionMode.THRESHOLD_AND_ZSCORE)
    assert (signal.signal_type, signal.z_score) == (SignalType.BAD, pytest.approx(-15.0))


@pytest.mark.parametrize("stddev, n", [(None, 0), (None, 2)])
def test_combined_mode_falls_back_to_the_threshold_rule_without_usable_evidence(stddev, n):
    snapshot = _snapshot(0.70, mean=None if n == 0 else 0.90, stddev=stddev, n=n)
    assert evaluate_snapshot(snapshot, 1, T, DetectionMode.THRESHOLD_AND_ZSCORE).signal_type is SignalType.BAD


def test_combined_mode_never_creates_bad_from_a_z_score_alone():
    # 3-point dip below every threshold, but z = -6 against a very stable history.
    signal = evaluate_snapshot(_snapshot(0.87, stddev=0.005), 1, T, DetectionMode.THRESHOLD_AND_ZSCORE)
    assert signal.signal_type is SignalType.NORMAL


@pytest.mark.parametrize("snapshot, expected", [
    (_snapshot(0.50, baseline=None, mean=None, stddev=None, n=0), SignalType.NO_BASELINE),
    (_snapshot(0.50, insufficient=True), SignalType.LOW_VOLUME),
])
def test_pause_signals_are_unchanged(snapshot, expected):
    for mode in DetectionMode:
        assert evaluate_snapshot(snapshot, 1, T, mode).signal_type is expected


def test_hysteresis_still_needs_three_confirmed_windows():
    mode = DetectionMode.THRESHOLD_AND_ZSCORE
    windows = [_snapshot(0.60, stddev=0.02, window=i) for i in range(2)]
    assert reconcile_timeline([evaluate_snapshot(s, 1, T, mode) for s in windows]) == []
    windows.append(_snapshot(0.60, stddev=0.02, window=2))
    [episode] = reconcile_timeline([evaluate_snapshot(s, 1, T, mode) for s in windows])
    assert episode.affected_window_count == 3


# --- Stage 2 statistics ----------------------------------------------------------------------------

def test_baseline_statistics_use_the_same_historical_rates_as_the_median():
    rates = [0.90, 0.88, 0.93, None]
    history = [_snapshot(r) for r in rates]
    mean, stddev, n = calculate_baseline_statistics(history)
    present = [r for r in rates if r is not None]
    assert (mean, stddev, n) == (pytest.approx(statistics.fmean(present)), pytest.approx(statistics.stdev(present)), 3)


@pytest.mark.parametrize("rates, expected", [([], (None, None, 0)), ([None], (None, None, 0)), ([0.9], (0.9, None, 1))])
def test_baseline_statistics_report_cold_starts_and_single_observations(rates, expected):
    assert calculate_baseline_statistics([_snapshot(r) for r in rates]) == expected
