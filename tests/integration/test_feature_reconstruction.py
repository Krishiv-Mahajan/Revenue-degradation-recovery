import pytest
import pytest_asyncio
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.dialects.postgresql import insert

from src.infrastructure.models import (
    PaymentEventModel,
    EpisodeStateHistoryModel,
    RCAEvaluationModel,
    CandidateCauseModel,
    Base
)
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

@pytest.mark.asyncio
async def test_reconstruct_features_strict_temporal(db_session: AsyncSession):
    repo = FeatureReconstructionRepository(db_session)
    svc = FeatureReconstructionService(repo)
    
    T = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    payment_id = "pay_reconstruct_1"
    
    # 1. Insert authorized event ingested exactly at T
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
    
    # 2. Insert older payment failures for failure rate calculation (in 30m window)
    for i in range(50):
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
    
    # 3. Insert future failure (SHOULD NOT BE INCLUDED)
    db_session.add(PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test",
        source_event_id="ev_future_fail",
        payment_id="pay_future",
        timestamp=T + timedelta(minutes=5),
        event_type="payment.failed",
        currency="INR",
        amount_minor_units=1000,
        payment_status="FAILED",
        payment_method="UPI",
        ingested_at=T + timedelta(minutes=5)
    ))
    
    # 4. Insert EpisodeStateHistory (Run 1: before T)
    run_1_id = uuid.uuid4()
    ep_id = uuid.uuid4()
    db_session.add(EpisodeStateHistoryModel(
        history_id=uuid.uuid4(),
        reconciliation_run_id=run_1_id,
        episode_id=ep_id,
        segment_dimension="GLOBAL",
        segment_value="ALL",
        status="ACTIVE",
        severity="HIGH",
        effective_start_window=T - timedelta(minutes=60),
        evaluation_timestamp=T - timedelta(minutes=10)
    ))
    
    # 5. Insert RCA Evaluation & Candidate Cause (Generated before T)
    eval_id = uuid.uuid4()
    db_session.add(RCAEvaluationModel(
        evaluation_id=eval_id,
        episode_id=ep_id,
        evaluation_version=1,
        classification="SEGMENT_SPECIFIC",
        analysis_window_start=T - timedelta(minutes=60),
        analysis_window_end=T - timedelta(minutes=10),
        input_fingerprint="sha256:dummy",
        generated_at=T - timedelta(minutes=9),
        evidence_audit_payload={}
    ))
    db_session.add(CandidateCauseModel(
        candidate_id=uuid.uuid4(),
        evaluation_id=eval_id,
        candidate_dimension="PAYMENT_METHOD",
        candidate_value="UPI",
        evidence_strength="STRONG",
        excess_failure_contribution=0.8,
        actual_segment_failures=100,
        expected_segment_failures=20.0,
        excess_segment_failures=80.0,
        rank=1
    ))
    
    # 6. Insert Future EpisodeStateHistory (Run 2: after T, INVALIDATES episode)
    # This must NOT be visible at T
    run_2_id = uuid.uuid4()
    db_session.add(EpisodeStateHistoryModel(
        history_id=uuid.uuid4(),
        reconciliation_run_id=run_2_id,
        episode_id=ep_id,
        segment_dimension="GLOBAL",
        segment_value="ALL",
        status="INVALIDATED",
        severity="HIGH",
        effective_start_window=T - timedelta(minutes=60),
        evaluation_timestamp=T + timedelta(minutes=10)
    ))
    
    await db_session.commit()
    
    # Reconstruct
    snapshot = await svc.reconstruct_features(payment_id, T)
    
    # Assertions
    assert snapshot is not None
    assert snapshot.payment_method == "UPI"
    assert snapshot.insufficient_global_volume is False
    
    # 50 failures / 50 total = 1.0. Future failure is excluded.
    assert snapshot.global_30m_failure_rate == 1.0
    
    # Degradation context is from Run 1 (ACTIVE), Run 2 (INVALIDATED) is ignored
    assert snapshot.is_in_active_degradation is True
    assert snapshot.degradation_severity == "HIGH"
    
    # RCA Context
    assert snapshot.rca_classification == "SEGMENT_SPECIFIC"
    assert snapshot.rca_candidate_dimension == "PAYMENT_METHOD"
    assert snapshot.rca_candidate_value == "UPI"
    assert snapshot.rca_candidate_rank == 1
    assert snapshot.rca_candidate_matches_payment_segment is True
