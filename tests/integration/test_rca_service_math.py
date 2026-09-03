import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.infrastructure.models import Base, PaymentEventModel, PaymentHealthSnapshotModel, DegradationSignalModel
from src.infrastructure.rca_repository import RCARepository
from src.core.rca.service import RCAService
from src.core.domain.degradation_models import DegradationEpisode, EpisodeStatus, Severity

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"

T0 = datetime(2024, 1, 8, 10, 0, tzinfo=timezone.utc)

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
    async with async_session() as sess:
        yield sess

async def setup_historical_baseline(session: AsyncSession, start_time: datetime, rate: float = 0.0):
    """Insert 4 weeks of GLOBAL snapshots to provide an episode baseline rate."""
    for i in range(1, 5):
        hist_start = start_time - timedelta(weeks=i)
        snap = PaymentHealthSnapshotModel(
            snapshot_id=uuid.uuid4(),
            window_start=hist_start,
            window_end=hist_start + timedelta(minutes=5),
            segment_dimension="GLOBAL",
            segment_value="GLOBAL",
            transaction_count=1000,
            successful_transaction_count=1000 - int(1000 * rate),
            failed_transaction_count=int(1000 * rate),
            success_rate=1.0 - rate,
            failure_rate=rate,
            total_gmv_minor_units=100000,
            successful_gmv_minor_units=100000,
            failed_gmv_minor_units=0,
            baseline_success_rate=1.0 - rate,
            insufficient_volume=False,
            calculated_at=datetime.now(timezone.utc),
        )
        session.add(snap)
        
        # We also need snapshots for HDFC, UPI, INR so they pass the valid_windows check
        for dim, val in [("bank", "HDFC"), ("bank", "SBI"), ("payment_method", "UPI"), ("currency", "INR")]:
            dim_snap = PaymentHealthSnapshotModel(
                snapshot_id=uuid.uuid4(),
                window_start=hist_start,
                window_end=hist_start + timedelta(minutes=5),
                segment_dimension=dim,
                segment_value=val,
                transaction_count=1000,
                successful_transaction_count=1000 - int(1000 * rate),
                failed_transaction_count=int(1000 * rate),
                success_rate=1.0 - rate,
                failure_rate=rate,
                total_gmv_minor_units=100000,
                successful_gmv_minor_units=100000,
                failed_gmv_minor_units=0,
                baseline_success_rate=1.0 - rate,
                insufficient_volume=False,
                calculated_at=datetime.now(timezone.utc),
            )
            session.add(dim_snap)
    await session.commit()

def make_failed_event(time: datetime, bank: str, method: str, currency: str):
    return PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="TEST",
        source_event_id=str(uuid.uuid4()),
        payment_id=str(uuid.uuid4()),
        timestamp=time,
        event_type="payment.failed",
        bank=bank,
        payment_method=method,
        currency=currency,
        amount_minor_units=1000,
        payment_status="FAILED",
        ingested_at=datetime.now(timezone.utc),
        error_code="ERR",
        error_source="BANK",
        error_step="AUTHORIZATION"
    )

def make_bad_signal(time: datetime):
    return DegradationSignalModel(
        signal_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        window_start=time,
        evaluation_version=1,
        signal_type="BAD",
        evaluation_timestamp=datetime.now(timezone.utc)
    )

@pytest.mark.asyncio
async def test_rca_service_overlapping_dimensions(session: AsyncSession):
    """
    100 episode failures
    HDFC excess = 100
    UPI excess = 100
    INR excess = 100
    """
    await setup_historical_baseline(session, T0, rate=0.0)
    
    # Insert 100 failed events overlapping completely
    for _ in range(100):
        session.add(make_failed_event(T0 + timedelta(minutes=1), "HDFC", "UPI", "INR"))
    session.add(make_bad_signal(T0))
    await session.commit()
    
    episode = DegradationEpisode(
        episode_id=uuid.uuid4(),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        started_at_window=T0,
        ended_at_window=T0 + timedelta(minutes=5),
        status=EpisodeStatus.ACTIVE,
        peak_absolute_drop=0.15,
        affected_window_count=1,
        severity=Severity.HIGH
    )
    
    repo = RCARepository(session)
    service = RCAService(session, repo)
    await service.evaluate_episode(episode)
    
    eval_record = await repo.get_latest_evaluation_for_episode(episode.episode_id)
    assert eval_record is not None
    
    # episode totals in payload
    payload = eval_record.evidence_audit_payload
    assert payload["episode_totals"]["actual_failures"] == 100
    assert payload["episode_totals"]["expected_failures"] == 0.0
    assert payload["episode_totals"]["episode_total_excess_failures"] == 100.0
    
    candidates = await repo.get_candidates_for_evaluation(eval_record.evaluation_id)
    assert len(candidates) >= 3
    
    cand_map = { (c.candidate_dimension, c.candidate_value): c for c in candidates }
    assert cand_map[("BANK", "HDFC")].excess_failure_contribution == 1.0
    assert cand_map[("PAYMENT_METHOD", "UPI")].excess_failure_contribution == 1.0
    assert cand_map[("CURRENCY", "INR")].excess_failure_contribution == 1.0

@pytest.mark.asyncio
async def test_rca_service_mutually_exclusive(session: AsyncSession):
    """
    HDFC = 60
    SBI = 40
    """
    await setup_historical_baseline(session, T0, rate=0.0)
    
    # 60 HDFC
    for _ in range(60):
        session.add(make_failed_event(T0 + timedelta(minutes=1), "HDFC", "UPI", "INR"))
    # 40 SBI
    for _ in range(40):
        session.add(make_failed_event(T0 + timedelta(minutes=1), "SBI", "UPI", "INR"))
    session.add(make_bad_signal(T0))
    await session.commit()
    
    episode = DegradationEpisode(
        episode_id=uuid.uuid4(),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        started_at_window=T0,
        ended_at_window=T0 + timedelta(minutes=5),
        status=EpisodeStatus.ACTIVE,
        peak_absolute_drop=0.15,
        affected_window_count=1,
        severity=Severity.HIGH
    )
    
    repo = RCARepository(session)
    service = RCAService(session, repo)
    await service.evaluate_episode(episode)
    
    eval_record = await repo.get_latest_evaluation_for_episode(episode.episode_id)
    
    candidates = await repo.get_candidates_for_evaluation(eval_record.evaluation_id)
    
    cand_map = { (c.candidate_dimension, c.candidate_value): c for c in candidates }
    assert abs(cand_map[("BANK", "HDFC")].excess_failure_contribution - 0.60) < 1e-9
    assert abs(cand_map[("BANK", "SBI")].excess_failure_contribution - 0.40) < 1e-9
    # UPI gets 100 failures -> 100 excess -> 1.0 contribution
    assert cand_map[("PAYMENT_METHOD", "UPI")].excess_failure_contribution == 1.0
