import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.infrastructure.models import Base
from src.infrastructure.analytics_repository import AnalyticsRepository
from src.infrastructure.repository import SQLIngestionRepository
from src.core.domain.models import PaymentEvent, RawIngestionRecord
from src.core.domain.analytics_models import PaymentHealthSnapshot

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"

@pytest.fixture
async def db_engine():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()

@pytest.fixture
async def session(db_engine):
    async_session = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session

@pytest.mark.asyncio
async def test_analytics_repository_integration(session):
    ingest_repo = SQLIngestionRepository(session)
    analytics_repo = AnalyticsRepository(session)

    ts = datetime(2023, 1, 1, 10, 2, tzinfo=timezone.utc)
    raw = RawIngestionRecord(
        source_system="razorpay",
        source_event_id="evt_1234",
        received_at=datetime.now(timezone.utc),
        raw_payload={},
        payload_hash="hash_1234"
    )
    event = PaymentEvent(
        event_id=uuid4(),
        source_system="razorpay",
        source_event_id="evt_1234",
        payment_id="pay_1234",
        order_id=None,
        timestamp=ts,
        event_type="payment.captured",
        currency="INR",
        amount_minor_units=1000,
        payment_status="captured",
        ingested_at=datetime.now(timezone.utc)
    )

    await ingest_repo.save_ingestion(raw, event)

    w_start = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    w_end = w_start + timedelta(minutes=5)

    events = await analytics_repo.get_events_in_window(w_start, w_end)
    assert len(events) == 1
    assert events[0].amount_minor_units == 1000

    snap = PaymentHealthSnapshot(
        snapshot_id=uuid4(),
        window_start=w_start,
        window_end=w_end,
        segment_dimension="GLOBAL",
        segment_value="ALL",
        transaction_count=1,
        successful_transaction_count=1,
        failed_transaction_count=0,
        success_rate=1.0,
        failure_rate=0.0,
        total_gmv_minor_units=1000,
        successful_gmv_minor_units=1000,
        failed_gmv_minor_units=0,
        baseline_success_rate=None,
        insufficient_volume=True,
        calculated_at=datetime.now(timezone.utc)
    )

    await analytics_repo.upsert_snapshots([snap])

    target_w_start = w_start + timedelta(days=7)
    target_w_end = target_w_start + timedelta(minutes=5)

    hist = await analytics_repo.get_historical_snapshots(
        target_w_start, target_w_end, "GLOBAL", "ALL", weeks_back=1
    )
    assert len(hist) == 1
    assert hist[0].snapshot_id == snap.snapshot_id

    snap2 = snap.model_copy(update={'transaction_count': 2})
    await analytics_repo.upsert_snapshots([snap2])

    hist2 = await analytics_repo.get_historical_snapshots(
        target_w_start, target_w_end, "GLOBAL", "ALL", weeks_back=1
    )
    assert len(hist2) == 1
    assert hist2[0].transaction_count == 2
