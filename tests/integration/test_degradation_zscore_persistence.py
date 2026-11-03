"""
Stage 2 historical statistics and Stage 3 Z-score evidence, persisted through the real services and
repositories against PostgreSQL.
"""
import statistics
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.core.analytics.service import PaymentHealthAnalyticsService
from src.core.degradation.service import DegradationService
from src.core.degradation.zscore import MIN_STDDEV_FLOOR, DetectionMode
from src.core.domain.analytics_models import PaymentHealthSnapshot
from src.infrastructure.analytics_repository import AnalyticsRepository
from src.infrastructure.degradation_repository import DegradationRepository
from src.infrastructure.models import DegradationSignalModel, PaymentEventModel, PaymentHealthSnapshotModel

WINDOW_START = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
WINDOW_END = WINDOW_START + timedelta(minutes=5)
HISTORY = [0.90, 0.92, 0.91, 0.89]


async def _seed(db_session):
    history = [
        PaymentHealthSnapshot(
            snapshot_id=uuid.uuid4(), window_start=WINDOW_START - timedelta(weeks=w),
            window_end=WINDOW_END - timedelta(weeks=w), segment_dimension="GLOBAL", segment_value="ALL",
            transaction_count=100, successful_transaction_count=int(rate * 100),
            failed_transaction_count=100 - int(rate * 100), success_rate=rate, failure_rate=1 - rate,
            total_gmv_minor_units=0, successful_gmv_minor_units=0, failed_gmv_minor_units=0,
            insufficient_volume=False, calculated_at=WINDOW_START,
        )
        for w, rate in enumerate(HISTORY, start=1)
    ]
    await AnalyticsRepository(db_session).upsert_snapshots(history)
    for i in range(100):
        at = WINDOW_START + timedelta(seconds=i)
        captured = i < 60
        db_session.add(PaymentEventModel(
            event_id=uuid.uuid4(), source_system="synthetic", source_event_id=f"evt_z_{i}", payment_id=f"pay_z_{i}",
            timestamp=at, event_type="payment.captured" if captured else "payment.failed", currency="INR",
            amount_minor_units=1000, payment_status="captured" if captured else "failed", payment_method="card",
            ingested_at=at,
        ))
    await db_session.commit()


async def _global_snapshot(db_session):
    return (await db_session.execute(select(PaymentHealthSnapshotModel).where(
        PaymentHealthSnapshotModel.window_start == WINDOW_START, PaymentHealthSnapshotModel.segment_dimension == "GLOBAL",
    ))).scalar_one()


@pytest.mark.parametrize("mode", list(DetectionMode))
async def test_statistics_and_z_score_evidence_are_persisted(db_session, mode):
    await _seed(db_session)
    await PaymentHealthAnalyticsService(AnalyticsRepository(db_session)).calculate_and_save_window(WINDOW_START, WINDOW_END)

    snapshot = await _global_snapshot(db_session)
    assert (snapshot.success_rate, snapshot.baseline_sample_count) == (0.60, 4)
    assert snapshot.baseline_mean_success_rate == pytest.approx(statistics.fmean(HISTORY))
    assert snapshot.baseline_stddev_success_rate == pytest.approx(statistics.stdev(HISTORY))

    domain = (await AnalyticsRepository(db_session).get_historical_snapshots(
        target_window_start=WINDOW_START + timedelta(weeks=1), target_window_end=WINDOW_END + timedelta(weeks=1),
        segment_dimension="GLOBAL", segment_value="ALL", weeks_back=1,
    ))[0]
    assert domain.baseline_sample_count == 4  # the repository maps the new columns back

    await DegradationService(DegradationRepository(db_session), mode=mode).process_snapshots([domain], WINDOW_END)
    await db_session.commit()
    [signal] = (await db_session.execute(select(DegradationSignalModel))).scalars().all()
    expected_z = (0.60 - statistics.fmean(HISTORY)) / MIN_STDDEV_FLOOR  # the spread (~0.013) is floored
    assert (signal.signal_type, signal.z_score_status) == ("BAD", "LOW_VARIANCE")
    assert signal.z_score == pytest.approx(expected_z, abs=1e-6)
