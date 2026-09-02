import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from src.infrastructure.models import Base, PaymentEventModel
from src.core.services.failure_prediction_service import FailurePredictionService
from src.core.services.feature_reconstruction_service import FeatureReconstructionService
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository

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

def create_payment_event(session: AsyncSession, payment_id: str, event_type: str, ingested_at: datetime):
    model = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=ingested_at,
        event_type=event_type,
        currency="INR",
        amount_minor_units=1000,
        payment_status="processed",
        ingested_at=ingested_at
    )
    session.add(model)

@pytest.mark.asyncio
async def test_temporal_eligibility_late_event(db_session: AsyncSession):
    """
    Test that a late-arriving terminal event (timestamp < T, but ingested_at > T)
    does NOT retroactively disqualify an authorized event at T.
    """
    repo = FeatureReconstructionRepository(db_session)
    feat_svc = FeatureReconstructionService(repo)
    svc = FailurePredictionService(db_session, feat_svc)
    payment_id = "pay_temporal_1"
    
    # Authorized event is available at T
    T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    create_payment_event(db_session, payment_id, "payment.authorized", T)
    
    # A failed event actually occurred *before* T, but arrived late
    # meaning its ingested_at > T
    late_ingested_at = datetime(2026, 9, 1, 10, 5, tzinfo=timezone.utc)
    
    model = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=datetime(2026, 9, 1, 9, 55, tzinfo=timezone.utc), # occurred before T
        event_type="payment.failed",
        currency="INR",
        amount_minor_units=1000,
        payment_status="processed",
        ingested_at=late_ingested_at # but ingested after T
    )
    db_session.add(model)
    await db_session.flush()
    
    # Because it was ingested after T, the payment was eligible at time T
    is_eligible = await svc.is_eligible_for_prediction(payment_id, T)
    assert is_eligible is True

@pytest.mark.asyncio
async def test_temporal_label_lookup_excludes_future_horizon(db_session: AsyncSession):
    """
    Label lookup should not see terminal events that arrive after the observation window.
    """
    repo = FeatureReconstructionRepository(db_session)
    feat_svc = FeatureReconstructionService(repo)
    svc = FailurePredictionService(db_session, feat_svc)
    payment_id = "pay_temporal_2"
    
    T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    create_payment_event(db_session, payment_id, "payment.authorized", T)
    
    # Terminal event arrives at T + 35m
    T_failed = T + timedelta(minutes=35)
    create_payment_event(db_session, payment_id, "payment.failed", T_failed)
    await db_session.flush()
    
    # With a 30m horizon, this terminal event is excluded
    label = await svc.lookup_terminal_label(payment_id, T, observation_window_minutes=30)
    assert label is None
    
    # With a 60m horizon, it is included
    label_60 = await svc.lookup_terminal_label(payment_id, T, observation_window_minutes=60)
    assert label_60 == 1
