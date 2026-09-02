import pytest
import pytest_asyncio
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, func

from src.infrastructure.models import Base, PaymentEventModel, FailurePredictionModel
from src.core.services.failure_prediction_service import FailurePredictionService
from src.core.services.feature_reconstruction_service import FeatureReconstructionService
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.infrastructure.failure_prediction_repository import FailurePredictionRepository
from src.core.ml.model import get_production_model
from src.core.domain.failure_prediction_models import PredictionStatus

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
async def test_prediction_persistence_and_idempotency(db_session: AsyncSession):
    repo = FeatureReconstructionRepository(db_session)
    feat_svc = FeatureReconstructionService(repo)
    pred_repo = FailurePredictionRepository(db_session)
    model = get_production_model()
    svc = FailurePredictionService(db_session, feat_svc, pred_repo, model)
    
    T = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    payment_id = "pay_persist_1"
    
    # 1. Insert trigger event
    auth_event = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test",
        source_event_id="ev_auth_1",
        payment_id=payment_id,
        timestamp=T,
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=1000,
        payment_status="AUTHORIZED",
        payment_method="UPI",
        ingested_at=T
    )
    db_session.add(auth_event)
    
    # Insert some global failure volume to ensure it's not INSUFFICIENT_DATA
    for i in range(60):
        db_session.add(PaymentEventModel(
            event_id=uuid.uuid4(),
            source_system="test",
            source_event_id=f"ev_fail_{i}",
            payment_id=f"pay_fail_{i}",
            timestamp=T - timedelta(minutes=5),
            event_type="payment.failed",
            currency="INR",
            amount_minor_units=1000,
            payment_status="FAILED",
            payment_method="UPI",
            ingested_at=T - timedelta(minutes=4)
        ))
    
    await db_session.commit()
    
    # 2. First prediction orchestration
    pred1 = await svc.orchestrate_prediction(payment_id, T)
    
    assert pred1.prediction_status == PredictionStatus.PREDICTED.value
    assert pred1.prediction_version == 1
    assert pred1.failure_probability is not None
    assert pred1.risk_band is not None
    
    # 3. Second prediction orchestration (exactly the same inputs -> Idempotent)
    pred2 = await svc.orchestrate_prediction(payment_id, T)
    
    # It should return the exact same object/version without appending
    assert pred2.prediction_version == 1
    assert pred2.prediction_id == pred1.prediction_id
    assert pred2.input_fingerprint == pred1.input_fingerprint
    
    # Verify in DB there is exactly 1 row
    stmt = select(func.count()).select_from(FailurePredictionModel).where(FailurePredictionModel.payment_attempt_id == payment_id)
    count = (await db_session.execute(stmt)).scalar()
    assert count == 1
    
    # 4. Modify context (new capture event ingested before T) to trigger a new version
    db_session.add(PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test",
        source_event_id="ev_cap_new",
        payment_id="pay_cap_new",
        timestamp=T - timedelta(minutes=2),
        event_type="payment.captured",
        currency="INR",
        amount_minor_units=1000,
        payment_status="CAPTURED",
        payment_method="UPI",
        ingested_at=T - timedelta(minutes=1)
    ))
    await db_session.commit()
    
    # 5. Third prediction orchestration (context changed -> new version)
    pred3 = await svc.orchestrate_prediction(payment_id, T)
    
    assert pred3.prediction_version == 2
    assert pred3.input_fingerprint != pred1.input_fingerprint
    
    # Verify in DB there are exactly 2 rows now
    stmt = select(func.count()).select_from(FailurePredictionModel).where(FailurePredictionModel.payment_attempt_id == payment_id)
    count = (await db_session.execute(stmt)).scalar()
    assert count == 2
