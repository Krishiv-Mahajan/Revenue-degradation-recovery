import uuid
from datetime import datetime, timezone, timedelta
from src.core.domain.degradation_models import (
    DegradationSignal,
    SignalType,
    EpisodeStatus,
    Severity
)
from src.core.degradation.state_machine import reconcile_timeline

def _make_signal(window_start: datetime, sig_type: SignalType, abs_drop: float = 0.0) -> DegradationSignal:
    return DegradationSignal(
        signal_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        window_start=window_start,
        evaluation_version=1,
        signal_type=sig_type,
        baseline_success_rate=0.99,
        absolute_drop=abs_drop,
        relative_drop=abs_drop/0.99 if abs_drop else 0.0,
        evaluation_timestamp=datetime.now(timezone.utc)
    )

def test_persistence_not_met():
    t0 = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    signals = [
        _make_signal(t0, SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=5), SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=10), SignalType.NORMAL, 0.0)
    ]
    episodes = reconcile_timeline(signals)
    assert len(episodes) == 0

def test_persistence_met():
    t0 = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    signals = [
        _make_signal(t0, SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=5), SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=10), SignalType.BAD, 0.06)
    ]
    episodes = reconcile_timeline(signals)
    assert len(episodes) == 1
    assert episodes[0].status == EpisodeStatus.ACTIVE
    assert episodes[0].started_at_window == t0
    assert episodes[0].affected_window_count == 3

def test_persistence_with_pause():
    t0 = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    signals = [
        _make_signal(t0, SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=5), SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=10), SignalType.LOW_VOLUME, 0.0),
        _make_signal(t0 + timedelta(minutes=15), SignalType.BAD, 0.06)
    ]
    episodes = reconcile_timeline(signals)
    assert len(episodes) == 1
    assert episodes[0].affected_window_count == 3
    assert episodes[0].started_at_window == t0

def test_recovery_with_pause():
    t0 = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    signals = [
        _make_signal(t0, SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=5), SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=10), SignalType.BAD, 0.06),  # Episode created
        _make_signal(t0 + timedelta(minutes=15), SignalType.NORMAL, 0.0),
        _make_signal(t0 + timedelta(minutes=20), SignalType.NO_BASELINE, 0.0),
        _make_signal(t0 + timedelta(minutes=25), SignalType.NORMAL, 0.0) # Episode recovered
    ]
    episodes = reconcile_timeline(signals)
    assert len(episodes) == 1
    assert episodes[0].status == EpisodeStatus.RECOVERED
    assert episodes[0].ended_at_window == t0 + timedelta(minutes=10) # last bad window

def test_severity_escalation():
    t0 = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    signals = [
        _make_signal(t0, SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=5), SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=10), SignalType.BAD, 0.06),
        _make_signal(t0 + timedelta(minutes=15), SignalType.BAD, 0.35) # CRITICAL absolute drop
    ]
    episodes = reconcile_timeline(signals)
    assert len(episodes) == 1
    assert episodes[0].severity == Severity.CRITICAL
    assert episodes[0].peak_absolute_drop == 0.35
