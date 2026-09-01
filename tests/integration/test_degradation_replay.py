import pytest
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.infrastructure.models import DegradationSignalModel, Base
from src.infrastructure.degradation_repository import DegradationRepository
from src.core.degradation.service import DegradationService
from src.core.domain.analytics_models import PaymentHealthSnapshot
from src.core.domain.degradation_models import EpisodeStatus
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

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
    async with async_session() as sess:
        yield sess

@pytest.fixture
def repo(session: AsyncSession):
    return DegradationRepository(session)

@pytest.fixture
def service(repo):
    return DegradationService(repo)

def _make_snapshot(window_start: datetime, success_rate: float, baseline: float = 0.99) -> PaymentHealthSnapshot:
    return PaymentHealthSnapshot(
        snapshot_id=uuid.uuid4(),
        window_start=window_start,
        window_end=window_start + timedelta(minutes=5),
        segment_dimension="GLOBAL",
        segment_value="GLOBAL",
        transaction_count=1000,
        successful_transaction_count=int(1000 * success_rate),
        failed_transaction_count=1000 - int(1000 * success_rate),
        total_gmv_minor_units=100000,
        successful_gmv_minor_units=int(100000 * success_rate),
        failed_gmv_minor_units=100000 - int(100000 * success_rate),
        success_rate=success_rate,
        insufficient_volume=False,
        baseline_success_rate=baseline,
        calculated_at=datetime.now(timezone.utc)
    )

@pytest.mark.asyncio
async def test_replay_idempotency_and_immutability(service: DegradationService, session: AsyncSession):
    t0 = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    
    # Timeline: BAD -> BAD -> BAD
    snapshots = [
        _make_snapshot(t0, 0.85),
        _make_snapshot(t0 + timedelta(minutes=5), 0.85),
        _make_snapshot(t0 + timedelta(minutes=10), 0.85)
    ]
    
    await service.process_snapshots(snapshots, datetime.now(timezone.utc))
    
    # Assert episode created
    episodes = await service.repository.get_episodes_for_segment("GLOBAL", "GLOBAL")
    assert len(episodes) == 1
    assert episodes[0].status == EpisodeStatus.ACTIVE
    
    # Assert signals created with version 1
    stmt = select(DegradationSignalModel)
    result = await session.execute(stmt)
    signals = result.scalars().all()
    assert len(signals) == 3
    assert all(s.evaluation_version == 1 for s in signals)
    
    # Now simulate late event changing middle window to NORMAL
    snapshots_replay = [
        _make_snapshot(t0, 0.85),
        _make_snapshot(t0 + timedelta(minutes=5), 0.99), # CHANGED TO NORMAL
        _make_snapshot(t0 + timedelta(minutes=10), 0.85)
    ]
    
    await service.process_snapshots(snapshots_replay, datetime.now(timezone.utc))
    
    # Assert episode invalidated
    episodes = await service.repository.get_episodes_for_segment("GLOBAL", "GLOBAL")
    assert len(episodes) == 1
    assert episodes[0].status == EpisodeStatus.INVALIDATED
    
    # Assert physical immutability:
    # We should now have 6 signals in the DB.
    # The original 3 with version 1 are completely untouched.
    # 3 new ones with version 2.
    stmt = select(DegradationSignalModel)
    result = await session.execute(stmt)
    all_signals = result.scalars().all()
    assert len(all_signals) == 6
    
    version_1 = [s for s in all_signals if s.evaluation_version == 1]
    assert len(version_1) == 3
    
    version_2 = [s for s in all_signals if s.evaluation_version == 2]
    assert len(version_2) == 3
