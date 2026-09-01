from datetime import datetime, timezone, timedelta
from uuid import uuid4
import pytest

from src.core.domain.models import PaymentEvent
from src.core.domain.analytics_models import PaymentHealthSnapshot
from src.core.analytics.calculator import calculate_health_metrics_for_window, calculate_baseline

def _create_event(
    event_type: str,
    amount: int,
    dt: datetime,
    payment_id: str = "P1",
    method: str = "card"
) -> PaymentEvent:
    return PaymentEvent(
        event_id=uuid4(),
        source_system="razorpay",
        source_event_id=str(uuid4()),
        payment_id=payment_id,
        order_id="O1",
        timestamp=dt,
        event_type=event_type,
        currency="INR",
        amount_minor_units=amount,
        payment_status="captured" if event_type == "payment.captured" else "failed",
        payment_method=method,
        bank=None,
        wallet=None,
        error_code=None,
        error_description=None,
        error_source=None,
        error_step=None,
        error_reason=None,
        ingested_at=datetime.now(timezone.utc)
    )

def test_metric_correctness():
    window_start = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(minutes=5)

    events = [
        _create_event("payment.captured", 500, window_start + timedelta(minutes=1), "P1"),
        _create_event("payment.captured", 1000, window_start + timedelta(minutes=2), "P2"),
        _create_event("payment.failed", 200, window_start + timedelta(minutes=3), "P3")
    ]

    snapshots = calculate_health_metrics_for_window(events, window_start, window_end, threshold=5)

    # Find GLOBAL
    global_snap = next(s for s in snapshots if s.segment_dimension == 'GLOBAL')
    assert global_snap.transaction_count == 3
    assert global_snap.successful_transaction_count == 2
    assert global_snap.failed_transaction_count == 1
    assert global_snap.success_rate == 2/3
    assert global_snap.failure_rate == 1/3
    assert global_snap.total_gmv_minor_units == 1700
    assert global_snap.successful_gmv_minor_units == 1500
    assert global_snap.failed_gmv_minor_units == 200
    assert global_snap.insufficient_volume is True # threshold is 5

def test_zero_denominator():
    window_start = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(minutes=5)

    # Authorized events don't count as terminal transactions in Stage 2
    events = [
        _create_event("payment.authorized", 500, window_start + timedelta(minutes=1), "P1")
    ]

    snapshots = calculate_health_metrics_for_window(events, window_start, window_end)
    global_snap = next(s for s in snapshots if s.segment_dimension == 'GLOBAL')

    assert global_snap.transaction_count == 0
    assert global_snap.success_rate is None
    assert global_snap.failure_rate is None

def test_event_semantics_temporal():
    # payment.failed P1 at 10:01
    # payment.captured P1 at 10:07
    fail_time = datetime(2023, 1, 1, 10, 1, tzinfo=timezone.utc)
    cap_time = datetime(2023, 1, 1, 10, 7, tzinfo=timezone.utc)

    events = [
        _create_event("payment.failed", 500, fail_time, "P1"),
        _create_event("payment.captured", 500, cap_time, "P1")
    ]

    # 10:00-10:05 window
    w1_start = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    w1_end = w1_start + timedelta(minutes=5)

    s1 = calculate_health_metrics_for_window(events, w1_start, w1_end)
    g1 = next(s for s in s1 if s.segment_dimension == 'GLOBAL')

    assert g1.transaction_count == 1
    assert g1.failed_transaction_count == 1
    assert g1.successful_transaction_count == 0
    assert g1.failed_gmv_minor_units == 500
    assert g1.successful_gmv_minor_units == 0

    # 10:05-10:10 window
    w2_start = datetime(2023, 1, 1, 10, 5, tzinfo=timezone.utc)
    w2_end = w2_start + timedelta(minutes=5)

    s2 = calculate_health_metrics_for_window(events, w2_start, w2_end)
    g2 = next(s for s in s2 if s.segment_dimension == 'GLOBAL')

    assert g2.transaction_count == 1
    assert g2.failed_transaction_count == 0
    assert g2.successful_transaction_count == 1
    assert g2.failed_gmv_minor_units == 0
    assert g2.successful_gmv_minor_units == 500

def test_window_half_open_boundaries():
    w_start = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    w_end = w_start + timedelta(minutes=5)

    events = [
        _create_event("payment.captured", 100, w_start, "P1"),       # In window
        _create_event("payment.captured", 200, w_end, "P2"),         # Outside window (>= end)
        _create_event("payment.captured", 300, w_start - timedelta(seconds=1), "P3") # Outside
    ]

    s = calculate_health_metrics_for_window(events, w_start, w_end)
    g = next(x for x in s if x.segment_dimension == 'GLOBAL')

    assert g.transaction_count == 1
    assert g.total_gmv_minor_units == 100

def test_baseline_calculation():
    # Create mock historical snapshots
    def _mock_snap(rate: float) -> PaymentHealthSnapshot:
        return PaymentHealthSnapshot(
            snapshot_id=uuid4(),
            window_start=datetime.now(timezone.utc),
            window_end=datetime.now(timezone.utc),
            segment_dimension="GLOBAL",
            segment_value="ALL",
            transaction_count=100,
            successful_transaction_count=0,
            failed_transaction_count=0,
            success_rate=rate,
            failure_rate=None,
            total_gmv_minor_units=0,
            successful_gmv_minor_units=0,
            failed_gmv_minor_units=0,
            baseline_success_rate=None,
            insufficient_volume=False,
            calculated_at=datetime.now(timezone.utc)
        )

    hist = [_mock_snap(0.90), _mock_snap(0.92), _mock_snap(0.95), _mock_snap(0.85)]
    baseline = calculate_baseline(hist)
    # Median of 0.85, 0.90, 0.92, 0.95 is (0.90 + 0.92) / 2 = 0.91
    assert baseline == 0.91

    # Insufficient history
    assert calculate_baseline([]) is None

def test_metric_reconciliation_invariants():
    w_start = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    w_end = w_start + timedelta(minutes=5)

    events = [
        _create_event("payment.captured", 150, w_start, "P1"),
        _create_event("payment.failed", 50, w_start, "P2"),
        _create_event("payment.captured", 300, w_start, "P3")
    ]

    snapshots = calculate_health_metrics_for_window(events, w_start, w_end)

    for s in snapshots:
        assert s.total_gmv_minor_units == s.successful_gmv_minor_units + s.failed_gmv_minor_units
        assert s.transaction_count == s.successful_transaction_count + s.failed_transaction_count

def test_low_volume_boundaries():
    w_start = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    w_end = w_start + timedelta(minutes=5)

    def run_with_n_events(n):
        events = [_create_event("payment.captured", 10, w_start, f"P{i}") for i in range(n)]
        s = calculate_health_metrics_for_window(events, w_start, w_end, threshold=50)
        return next(x for x in s if x.segment_dimension == 'GLOBAL').insufficient_volume

    assert run_with_n_events(49) is True
    assert run_with_n_events(50) is False
    assert run_with_n_events(51) is False
