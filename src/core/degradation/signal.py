from datetime import datetime
import uuid
from typing import Optional

from src.core.domain.analytics_models import PaymentHealthSnapshot
from src.core.domain.degradation_models import (
    DegradationSignal,
    SignalType,
    MIN_ABSOLUTE_RATE_DROP,
    MIN_RELATIVE_RATE_DROP,
    MIN_BASELINE_RATE_FOR_RELATIVE_TEST
)

def evaluate_snapshot(snapshot: PaymentHealthSnapshot, evaluation_version: int, evaluation_timestamp: datetime) -> DegradationSignal:
    absolute_drop: Optional[float] = None
    relative_drop: Optional[float] = None
    
    if snapshot.baseline_success_rate is None:
        signal_type = SignalType.NO_BASELINE
    elif snapshot.insufficient_volume:
        signal_type = SignalType.LOW_VOLUME
    else:
        baseline = snapshot.baseline_success_rate
        current = snapshot.success_rate
        
        absolute_drop = baseline - current
        if baseline > 0:
            relative_drop = (baseline - current) / baseline
        else:
            relative_drop = 0.0
            
        if baseline < MIN_BASELINE_RATE_FOR_RELATIVE_TEST:
            if absolute_drop >= MIN_ABSOLUTE_RATE_DROP:
                signal_type = SignalType.BAD
            else:
                signal_type = SignalType.NORMAL
        else:
            if absolute_drop >= MIN_ABSOLUTE_RATE_DROP and relative_drop >= MIN_RELATIVE_RATE_DROP:
                signal_type = SignalType.BAD
            else:
                signal_type = SignalType.NORMAL

    return DegradationSignal(
        signal_id=uuid.uuid4(),
        snapshot_id=snapshot.snapshot_id,
        segment_dimension=snapshot.segment_dimension,
        segment_value=snapshot.segment_value,
        window_start=snapshot.window_start,
        evaluation_version=evaluation_version,
        signal_type=signal_type,
        baseline_success_rate=snapshot.baseline_success_rate,
        absolute_drop=absolute_drop,
        relative_drop=relative_drop,
        evaluation_timestamp=evaluation_timestamp
    )
