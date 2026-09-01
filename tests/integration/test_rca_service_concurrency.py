import asyncio
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.infrastructure.models import Base
from src.infrastructure.rca_repository import RCARepository
from src.core.rca.service import RCAService
from src.core.domain.degradation_models import DegradationEpisode, EpisodeStatus

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"

@pytest.fixture
async def db_engine():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()

@pytest.mark.asyncio
async def test_concurrent_evaluate_episode_serializes_safely(db_engine, monkeypatch):
    """
    Simulate two concurrent evaluate_episode() calls for the same episode.
    Ensure both succeed, and the versions are sequential without throwing an IntegrityError.
    """
    episode_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    ended_at = datetime.now(timezone.utc)
    
    from src.core.domain.degradation_models import Severity
    episode = DegradationEpisode(
        episode_id=episode_id,
        segment_dimension="PAYMENT_METHOD",
        segment_value="UPI",
        started_at_window=started_at,
        ended_at_window=ended_at,
        status=EpisodeStatus.INVALIDATED,
        peak_absolute_drop=0.15,
        affected_window_count=5,
        severity=Severity.HIGH
    )
    
    # Delay the initial DB check to guarantee both tasks pass the idempotency 
    # check before either can acquire the lock.
    original_get = RCARepository.get_latest_evaluation_for_episode
    async def delayed_get(self, *args, **kwargs):
        res = await original_get(self, *args, **kwargs)
        await asyncio.sleep(0.1)
        return res
    monkeypatch.setattr(RCARepository, "get_latest_evaluation_for_episode", delayed_get)
    
    async_session_maker = sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    
    async def worker():
        async with async_session_maker() as session:
            repo = RCARepository(session)
            service = RCAService(session, repo)
            await service.evaluate_episode(episode)
        
    # Run two concurrent tasks
    results = await asyncio.gather(worker(), worker(), return_exceptions=True)
        
    # Assert no exceptions were thrown (e.g. IntegrityError)
    for res in results:
        assert not isinstance(res, Exception), f"Task raised an exception: {res}"
        
    # Verify the results in the DB
    async with async_session_maker() as session:
        repo = RCARepository(session)
        max_version = await repo.get_max_evaluation_version(episode_id)
        assert max_version == 2, f"Expected 2 versions created sequentially, got {max_version}"
