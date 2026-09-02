"""
Integration tests for Stage 6 persistence, immutability, and idempotency.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.domain.failure_prediction_models import FailurePrediction
from src.core.intervention.service import InterventionDecisionService
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.infrastructure.failure_prediction_repository import FailurePredictionRepository
from src.infrastructure.intervention_repository import InterventionRepository
from src.infrastructure.models import Base, InterventionDecisionModel, PaymentEventModel

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
async def test_idempotency_identical_inputs(db_session: AsyncSession):
    """
    Identical inputs + identical T_decide -> returns existing decision without creating a new version.
    """
    feat_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    inter_repo = InterventionRepository(db_session)
    service = InterventionDecisionService(db_session, inter_repo, feat_repo)

    payment_id = "pay_idem_1"
    T = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    # Ingest auth event
    auth = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=T,
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=10000,
        payment_status="processed",
        ingested_at=T,
    )
    db_session.add(auth)
    await db_session.flush()

    # Ingest Stage 5 prediction
    pred = FailurePrediction(
        prediction_id=uuid.uuid4(),
        payment_attempt_id=payment_id,
        prediction_version=1,
        predicted_at=T,
        prediction_horizon="30m",
        failure_probability=0.80,
        risk_band="HIGH",
        prediction_status="PREDICTED",
        model_name="test",
        model_version="1.0",
        feature_schema_version="1.0",
        feature_snapshot={},
        input_fingerprint="fp1",
        created_at=T,
    )
    await pred_repo.append_prediction(pred)

    # First call -> creates version 1
    d1 = await service.decide(payment_id, T)
    assert d1.decision_version == 1

    # Second call at same T_decide -> idempotent, returns version 1
    d2 = await service.decide(payment_id, T)
    assert d2.decision_version == 1
    assert d2.decision_id == d1.decision_id
    assert d2.input_fingerprint == d1.input_fingerprint

    # Verify only 1 row exists in the database
    stmt = select(func.count()).where(InterventionDecisionModel.payment_attempt_id == payment_id)
    count = (await db_session.execute(stmt)).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_version_increment_on_later_re_evaluation(db_session: AsyncSession):
    """
    Later evaluation with different T_decide increments decision version monotonically.
    """
    feat_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    inter_repo = InterventionRepository(db_session)
    service = InterventionDecisionService(db_session, inter_repo, feat_repo)

    payment_id = "pay_reval_1"
    T1 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    T2 = datetime(2026, 9, 2, 10, 5, tzinfo=timezone.utc)

    auth = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=T1,
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=10000,
        payment_status="processed",
        ingested_at=T1,
    )
    db_session.add(auth)
    await db_session.flush()

    pred = FailurePrediction(
        prediction_id=uuid.uuid4(),
        payment_attempt_id=payment_id,
        prediction_version=1,
        predicted_at=T1,
        prediction_horizon="30m",
        failure_probability=0.80,
        risk_band="HIGH",
        prediction_status="PREDICTED",
        model_name="test",
        model_version="1.0",
        feature_schema_version="1.0",
        feature_snapshot={},
        input_fingerprint="fp1",
        created_at=T1,
    )
    await pred_repo.append_prediction(pred)

    d1 = await service.decide(payment_id, T1)
    assert d1.decision_version == 1

    # Re-evaluate at T2
    d2 = await service.decide(payment_id, T2)
    assert d2.decision_version == 2
    assert d2.decision_id != d1.decision_id

    # Verify both version 1 and version 2 exist immutably in the DB
    stmt = select(func.count()).where(InterventionDecisionModel.payment_attempt_id == payment_id)
    count = (await db_session.execute(stmt)).scalar()
    assert count == 2
