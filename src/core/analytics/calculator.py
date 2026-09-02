from datetime import datetime, timezone
from typing import List, Optional, Dict
from uuid import uuid4
import statistics

from src.core.domain.models import PaymentEvent
from src.core.domain.analytics_models import PaymentHealthSnapshot

MIN_TRANSACTION_THRESHOLD = 50

def _get_segment_value(event: PaymentEvent, dimension: str) -> str:
    if dimension == 'GLOBAL':
        return 'ALL'
    val = getattr(event, dimension, None)
    return str(val) if val is not None else 'NULL'

def calculate_health_metrics_for_window(
    events: List[PaymentEvent],
    window_start: datetime,
    window_end: datetime,
    threshold: int = MIN_TRANSACTION_THRESHOLD
) -> List[PaymentHealthSnapshot]:

    dimensions = ['GLOBAL', 'currency', 'payment_method', 'bank', 'wallet', 'error_source']

    snapshots = []

    for dimension in dimensions:
        segments: Dict[str, List[PaymentEvent]] = {}

        # If no events, we still want to generate an empty GLOBAL snapshot for the timeline
        if not events and dimension == 'GLOBAL':
            segments['ALL'] = []

        for event in events:
            # Strict window check: window_start <= timestamp < window_end
            if not (window_start <= event.timestamp < window_end):
                continue

            val = _get_segment_value(event, dimension)
            if val not in segments:
                segments[val] = []
            segments[val].append(event)

        for segment_value, segment_events in segments.items():
            successful_count = 0
            failed_count = 0
            successful_gmv = 0
            failed_gmv = 0

            for event in segment_events:
                if event.event_type == 'payment.captured':
                    successful_count += 1
                    successful_gmv += event.amount_minor_units
                elif event.event_type == 'payment.failed':
                    failed_count += 1
                    failed_gmv += event.amount_minor_units

            total_count = successful_count + failed_count
            total_gmv = successful_gmv + failed_gmv

            success_rate = None
            failure_rate = None
            if total_count > 0:
                success_rate = successful_count / total_count
                failure_rate = failed_count / total_count

            insufficient_volume = total_count < threshold

            snapshot = PaymentHealthSnapshot(
                snapshot_id=uuid4(),
                window_start=window_start,
                window_end=window_end,
                segment_dimension=dimension,
                segment_value=segment_value,
                transaction_count=total_count,
                successful_transaction_count=successful_count,
                failed_transaction_count=failed_count,
                success_rate=success_rate,
                failure_rate=failure_rate,
                total_gmv_minor_units=total_gmv,
                successful_gmv_minor_units=successful_gmv,
                failed_gmv_minor_units=failed_gmv,
                baseline_success_rate=None, # Populated later
                insufficient_volume=insufficient_volume,
                calculated_at=datetime.now(timezone.utc)
            )
            snapshots.append(snapshot)

    return snapshots

def calculate_baseline(historical_snapshots: List[PaymentHealthSnapshot]) -> Optional[float]:
    """
    Given a list of historical snapshots (e.g. from the previous 4 matching weekdays),
    calculate the median success rate.
    """
    rates = [s.success_rate for s in historical_snapshots if s.success_rate is not None]
    if not rates:
        return None
    return statistics.median(rates)
