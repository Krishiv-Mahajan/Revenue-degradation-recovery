import pytest
import pytest_asyncio
from datetime import datetime, timezone
import uuid
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from src.infrastructure.models import Base
from src.infrastructure.repository import SQLIngestionRepository
from src.core.domain.models import RawIngestionRecord, PaymentEvent
from src.core.domain.exceptions import DuplicateEventConflictError

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"

@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

@pytest.mark.asyncio
async def test_repository_save_and_get(db_session: AsyncSession):
    repo = SQLIngestionRepository(db_session)
    
    raw = RawIngestionRecord(
        source_system="test_system",
        source_event_id="evt_1",
        received_at=datetime.now(timezone.utc),
        raw_payload={"foo": "bar"},
        payload_hash="hash_1"
    )
    
    event = PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id="evt_1",
        payment_id="pay_1",
        order_id=None,
        timestamp=datetime.now(timezone.utc),
        event_type="test",
        currency="INR",
        amount_minor_units=1000,
        payment_status="failed",
        error_code=None,
        error_description=None,
        error_source=None,
        error_step=None,
        error_reason=None,
        ingested_at=datetime.now(timezone.utc)
    )
    
    await repo.save_ingestion(raw, event)
    
    fetched = await repo.get_raw_record("test_system", "evt_1")
    assert fetched is not None
    assert fetched.payload_hash == "hash_1"
    assert fetched.raw_payload == {"foo": "bar"}

@pytest.mark.asyncio
async def test_repository_duplicate_conflict(db_session: AsyncSession):
    repo = SQLIngestionRepository(db_session)
    
    raw = RawIngestionRecord(
        source_system="test_system",
        source_event_id="evt_2",
        received_at=datetime.now(timezone.utc),
        raw_payload={},
        payload_hash="hash_2"
    )
    
    event = PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id="evt_2",
        payment_id="pay_2",
        timestamp=datetime.now(timezone.utc),
        event_type="test",
        currency="INR",
        amount_minor_units=1000,
        payment_status="failed",
        ingested_at=datetime.now(timezone.utc)
    )
    
    await repo.save_ingestion(raw, event)
    
    # Try saving same identity again
    raw2 = RawIngestionRecord(
        source_system="test_system",
        source_event_id="evt_2",
        received_at=datetime.now(timezone.utc),
        raw_payload={},
        payload_hash="diff_hash"
    )
    event2 = PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id="evt_2",
        payment_id="pay_3",
        timestamp=datetime.now(timezone.utc),
        event_type="test",
        currency="INR",
        amount_minor_units=1000,
        payment_status="failed",
        ingested_at=datetime.now(timezone.utc)
    )
    
    with pytest.raises(DuplicateEventConflictError):
        await repo.save_ingestion(raw2, event2)
