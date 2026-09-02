import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from src.infrastructure.models import Base, PaymentEventModel
from src.core.services.failure_prediction_service import FailurePredictionService
from src.core.services.feature_reconstruction_service import FeatureReconstructionService
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.infrastructure.failure_prediction_repository import FailurePredictionRepository
from src.core.ml.model import get_production_model

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
async def test_lifecycle_authorized_to_failed(db_session: AsyncSession):
    """Fixture A: payment.authorized -> payment.failed"""
    repo = FeatureReconstructionRepository(db_session)
    feat_svc = FeatureReconstructionService(repo)
    pred_repo = FailurePredictionRepository(db_session)
    model = get_production_model()
    svc = FailurePredictionService(db_session, feat_svc, pred_repo, model)
    payment_id = "pay_A"
    
    T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    create_payment_event(db_session, payment_id, "payment.authorized", T)
    
    # Eligibility evaluated before the terminal event exists (Fixture C)
    await db_session.flush()
    is_eligible = await svc.is_eligible_for_prediction(payment_id, T)
    assert is_eligible is True
    
    label_before = await svc.lookup_terminal_label(payment_id, T, observation_window_minutes=30)
    assert label_before is None
    
    # Terminal event arrives
    T_failed = T + timedelta(minutes=5)
    create_payment_event(db_session, payment_id, "payment.failed", T_failed)
    await db_session.flush()
    
    # Terminal label lookup after prediction (Fixture D)
    label_after = await svc.lookup_terminal_label(payment_id, T, observation_window_minutes=30)
    assert label_after == 1

@pytest.mark.asyncio
async def test_lifecycle_authorized_to_captured(db_session: AsyncSession):
    """Fixture B: payment.authorized -> payment.captured"""
    repo = FeatureReconstructionRepository(db_session)
    feat_svc = FeatureReconstructionService(repo)
    pred_repo = FailurePredictionRepository(db_session)
    model = get_production_model()
    svc = FailurePredictionService(db_session, feat_svc, pred_repo, model)
    payment_id = "pay_B"
    
    T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    create_payment_event(db_session, payment_id, "payment.authorized", T)
    await db_session.flush()
    
    T_captured = T + timedelta(minutes=10)
    create_payment_event(db_session, payment_id, "payment.captured", T_captured)
    await db_session.flush()
    
    label = await svc.lookup_terminal_label(payment_id, T, observation_window_minutes=30)
    assert label == 0

@pytest.mark.asyncio
async def test_future_terminal_information_leak(db_session: AsyncSession):
    """Fixture E: Future terminal information cannot leak into the prediction context."""
    repo = FeatureReconstructionRepository(db_session)
    feat_svc = FeatureReconstructionService(repo)
    pred_repo = FailurePredictionRepository(db_session)
    model = get_production_model()
    svc = FailurePredictionService(db_session, feat_svc, pred_repo, model)
    payment_id = "pay_E"
    
    T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    # Event arrives after T
    T_failed = T + timedelta(minutes=5)
    
    # But let's say it's somehow in the DB when we check eligibility at T (retroactive run)
    create_payment_event(db_session, payment_id, "payment.authorized", T)
    create_payment_event(db_session, payment_id, "payment.failed", T_failed)
    await db_session.flush()
    
    # At time T, the future event MUST NOT affect eligibility (it wasn't ingested before T)
    is_eligible = await svc.is_eligible_for_prediction(payment_id, T)
    assert is_eligible is True

@pytest.mark.asyncio
async def test_ineligible_prior_terminal_event(db_session: AsyncSession):
    """If a terminal event was ingested BEFORE T, it is ineligible."""
    repo = FeatureReconstructionRepository(db_session)
    feat_svc = FeatureReconstructionService(repo)
    pred_repo = FailurePredictionRepository(db_session)
    model = get_production_model()
    svc = FailurePredictionService(db_session, feat_svc, pred_repo, model)
    payment_id = "pay_F"
    
    T_terminal = datetime(2026, 9, 1, 9, 50, tzinfo=timezone.utc)
    T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    
    create_payment_event(db_session, payment_id, "payment.failed", T_terminal)
    create_payment_event(db_session, payment_id, "payment.authorized", T)
    await db_session.flush()
    
    is_eligible = await svc.is_eligible_for_prediction(payment_id, T)
    assert is_eligible is False
