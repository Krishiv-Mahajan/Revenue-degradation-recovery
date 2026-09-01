import uuid
from datetime import datetime, timezone
from src.core.domain.analytics_models import PaymentHealthSnapshot
from src.core.domain.degradation_models import SignalType, MIN_ABSOLUTE_RATE_DROP, MIN_RELATIVE_RATE_DROP, MIN_BASELINE_RATE_FOR_RELATIVE_TEST
from src.core.degradation.signal import evaluate_snapshot

def test_evaluate_snapshot_normal():
    snapshot = PaymentHealthSnapshot(
        snapshot_id=uuid.uuid4(),
        window_start=datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc),
        window_end=datetime(2023, 1, 1, 10, 5, tzinfo=timezone.utc),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        transaction_count=1000,
        successful_transaction_count=980,
        failed_transaction_count=20,
        total_gmv_minor_units=1000,
        successful_gmv_minor_units=980,
        failed_gmv_minor_units=20,
        success_rate=0.98,
        insufficient_volume=False,
        baseline_success_rate=0.99,
        calculated_at=datetime.now(timezone.utc)
    )
    
    signal = evaluate_snapshot(snapshot, 1, datetime.now(timezone.utc))
    assert signal.signal_type == SignalType.NORMAL
    assert round(signal.absolute_drop, 2) == 0.01

def test_evaluate_snapshot_bad_both_thresholds():
    snapshot = PaymentHealthSnapshot(
        snapshot_id=uuid.uuid4(),
        window_start=datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc),
        window_end=datetime(2023, 1, 1, 10, 5, tzinfo=timezone.utc),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        transaction_count=1000,
        successful_transaction_count=850,
        failed_transaction_count=150,
        total_gmv_minor_units=1000,
        successful_gmv_minor_units=850,
        failed_gmv_minor_units=150,
        success_rate=0.85,
        insufficient_volume=False,
        baseline_success_rate=0.99,
        calculated_at=datetime.now(timezone.utc)
    )
    
    signal = evaluate_snapshot(snapshot, 1, datetime.now(timezone.utc))
    assert signal.signal_type == SignalType.BAD
    assert signal.absolute_drop == 0.14
    assert round(signal.relative_drop, 4) == 0.1414

def test_evaluate_snapshot_bad_low_baseline():
    snapshot = PaymentHealthSnapshot(
        snapshot_id=uuid.uuid4(),
        window_start=datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc),
        window_end=datetime(2023, 1, 1, 10, 5, tzinfo=timezone.utc),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        transaction_count=100,
        successful_transaction_count=0,
        failed_transaction_count=100,
        total_gmv_minor_units=100,
        successful_gmv_minor_units=0,
        failed_gmv_minor_units=100,
        success_rate=0.0,
        insufficient_volume=False,
        baseline_success_rate=0.04,
        calculated_at=datetime.now(timezone.utc)
    )
    # Absolute drop is 0.04. But MIN_ABSOLUTE_RATE_DROP is 0.05.
    # Since baseline < 0.05, relative drop is ignored. So should be NORMAL.
    signal = evaluate_snapshot(snapshot, 1, datetime.now(timezone.utc))
    assert signal.signal_type == SignalType.NORMAL

def test_evaluate_snapshot_bad_low_baseline_above_absolute():
    snapshot = PaymentHealthSnapshot(
        snapshot_id=uuid.uuid4(),
        window_start=datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc),
        window_end=datetime(2023, 1, 1, 10, 5, tzinfo=timezone.utc),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        transaction_count=100,
        successful_transaction_count=0,
        failed_transaction_count=100,
        total_gmv_minor_units=100,
        successful_gmv_minor_units=0,
        failed_gmv_minor_units=100,
        success_rate=0.0,
        insufficient_volume=False,
        baseline_success_rate=0.06,
        calculated_at=datetime.now(timezone.utc)
    )
    signal = evaluate_snapshot(snapshot, 1, datetime.now(timezone.utc))
    assert signal.signal_type == SignalType.BAD

def test_evaluate_snapshot_zero_baseline():
    snapshot = PaymentHealthSnapshot(
        snapshot_id=uuid.uuid4(),
        window_start=datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc),
        window_end=datetime(2023, 1, 1, 10, 5, tzinfo=timezone.utc),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        transaction_count=100,
        successful_transaction_count=0,
        failed_transaction_count=100,
        total_gmv_minor_units=100,
        successful_gmv_minor_units=0,
        failed_gmv_minor_units=100,
        success_rate=0.0,
        insufficient_volume=False,
        baseline_success_rate=0.0,
        calculated_at=datetime.now(timezone.utc)
    )
    signal = evaluate_snapshot(snapshot, 1, datetime.now(timezone.utc))
    assert signal.signal_type == SignalType.NORMAL
    assert signal.absolute_drop == 0.0
    assert signal.relative_drop == 0.0

def test_evaluate_snapshot_low_volume():
    snapshot = PaymentHealthSnapshot(
        snapshot_id=uuid.uuid4(),
        window_start=datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc),
        window_end=datetime(2023, 1, 1, 10, 5, tzinfo=timezone.utc),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        transaction_count=10,
        successful_transaction_count=0,
        failed_transaction_count=10,
        total_gmv_minor_units=10,
        successful_gmv_minor_units=0,
        failed_gmv_minor_units=10,
        success_rate=0.0,
        insufficient_volume=True,
        baseline_success_rate=0.99,
        calculated_at=datetime.now(timezone.utc)
    )
    signal = evaluate_snapshot(snapshot, 1, datetime.now(timezone.utc))
    assert signal.signal_type == SignalType.LOW_VOLUME

def test_evaluate_snapshot_no_baseline():
    snapshot = PaymentHealthSnapshot(
        snapshot_id=uuid.uuid4(),
        window_start=datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc),
        window_end=datetime(2023, 1, 1, 10, 5, tzinfo=timezone.utc),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        transaction_count=1000,
        successful_transaction_count=990,
        failed_transaction_count=10,
        total_gmv_minor_units=1000,
        successful_gmv_minor_units=990,
        failed_gmv_minor_units=10,
        success_rate=0.99,
        insufficient_volume=False,
        baseline_success_rate=None,
        calculated_at=datetime.now(timezone.utc)
    )
    signal = evaluate_snapshot(snapshot, 1, datetime.now(timezone.utc))
    assert signal.signal_type == SignalType.NO_BASELINE
