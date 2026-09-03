import pytest
from datetime import datetime, timedelta, timezone
import uuid
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from src.infrastructure.models import Base, PaymentEventModel, EpisodeStateHistoryModel, RCAEvaluationModel, CandidateCauseModel
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.core.services.feature_reconstruction_service import FeatureReconstructionService

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

@pytest.fixture
def repo(session: AsyncSession):
    return FeatureReconstructionRepository(session)

@pytest.fixture
def service(repo):
    return FeatureReconstructionService(repo)

T0 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

def make_event(payment_id, bank=None, wallet=None, currency=None, pm=None, ingested_at=T0):
    return PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="TEST",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=ingested_at,
        event_type="payment.authorized",
        bank=bank,
        wallet=wallet,
        currency=currency,
        payment_method=pm,
        amount_minor_units=1000,
        payment_status="AUTHORIZED",
        ingested_at=ingested_at
    )

def make_episode(dim, val, severity, start_time, eval_time):
    return EpisodeStateHistoryModel(
        reconciliation_run_id=uuid.uuid4(),
        episode_id=uuid.uuid4(),
        segment_dimension=dim,
        segment_value=val,
        status="ACTIVE",
        severity=severity,
        effective_start_window=start_time,
        evaluation_timestamp=eval_time
    )

def make_rca(episode_id, gen_time, cand_dim, cand_val, strength, version=1):
    eval_id = uuid.uuid4()
    rca = RCAEvaluationModel(
        evaluation_id=eval_id,
        episode_id=episode_id,
        generated_at=gen_time,
        evaluation_version=version,
        analysis_window_start=gen_time - timedelta(minutes=5),
        analysis_window_end=gen_time,
        input_fingerprint=f"sha256:{version}",
        evidence_audit_payload={},
        classification="SEGMENT_SPECIFIC"
    )
    cand = CandidateCauseModel(
        candidate_id=uuid.uuid4(),
        evaluation_id=eval_id,
        candidate_dimension=cand_dim,
        candidate_value=cand_val,
        rank=1,
        evidence_strength=strength,
        actual_segment_failures=100,
        expected_segment_failures=0.0,
        excess_segment_failures=100,
        excess_failure_contribution=1.0
    )
    return rca, cand

@pytest.mark.asyncio
async def test_boundary_active_episode_resolution(session: AsyncSession, repo: FeatureReconstructionRepository, service: FeatureReconstructionService):
    t_eval = T0 - timedelta(minutes=1)
    
    # Insert multiple active episodes
    ep_bank = make_episode("BANK", "HDFC", "CRITICAL", T0 - timedelta(minutes=10), t_eval)
    ep_wallet = make_episode("WALLET", "PAYTM", "HIGH", T0 - timedelta(minutes=9), t_eval)
    ep_global = make_episode("GLOBAL", "ALL", "MODERATE", T0 - timedelta(minutes=8), t_eval)
    
    ep_curr = make_episode("CURRENCY", "INR", "CRITICAL", T0 - timedelta(minutes=15), t_eval)
    
    session.add_all([ep_bank, ep_wallet, ep_global, ep_curr])
    await session.commit()
    
    # 1. BANK-only active degradation is discovered
    event1 = make_event("p1", bank="HDFC")
    res1 = await repo.get_most_severe_active_episode(event1, T0)
    assert res1.episode_id == ep_bank.episode_id
    
    # 2. WALLET-only active degradation is discovered
    event2 = make_event("p2", wallet="PAYTM")
    res2 = await repo.get_most_severe_active_episode(event2, T0)
    assert res2.episode_id == ep_wallet.episode_id
    
    # 3. CURRENCY-only
    event3 = make_event("p3", currency="INR")
    res3 = await repo.get_most_severe_active_episode(event3, T0)
    assert res3.episode_id == ep_curr.episode_id
    
    # 4. Overlapping: GLOBAL + BANK + WALLET. CRITICAL Bank should win over HIGH wallet and MODERATE global
    event4 = make_event("p4", bank="HDFC", wallet="PAYTM")
    res4 = await repo.get_most_severe_active_episode(event4, T0)
    assert res4.episode_id == ep_bank.episode_id
    
    # 5. Equal-severity tie-breaker: CURRENCY (older, CRITICAL) vs BANK (newer, CRITICAL)
    event5 = make_event("p5", currency="INR", bank="HDFC")
    res5 = await repo.get_most_severe_active_episode(event5, T0)
    assert res5.episode_id == ep_curr.episode_id
    
@pytest.mark.asyncio
async def test_boundary_rca_matching_and_temporal(session: AsyncSession, service: FeatureReconstructionService):
    t_eval = T0 - timedelta(minutes=5)
    
    # GLOBAL episode
    ep = make_episode("GLOBAL", "ALL", "MODERATE", t_eval, t_eval)
    session.add(ep)
    
    # 9. generated_at == T excluded
    rca_eq, cand_eq = make_rca(ep.episode_id, T0, "CURRENCY", "INR", "STRONG", version=1)
    # 10. generated_at > T excluded
    rca_fut, cand_fut = make_rca(ep.episode_id, T0 + timedelta(minutes=1), "CURRENCY", "INR", "STRONG", version=2)
    
    # Valid RCA < T
    rca_valid, cand_valid = make_rca(ep.episode_id, T0 - timedelta(minutes=1), "CURRENCY", "INR", "STRONG", version=3)
    
    session.add_all([rca_eq, cand_eq, rca_fut, cand_fut, rca_valid, cand_valid])
    await session.commit()
    
    # Reconstruct features for payment at T0 with currency INR
    event = make_event("p1", currency="INR", ingested_at=T0 - timedelta(seconds=1))
    session.add(event)
    await session.commit()
    
    snapshot = await service.reconstruct_features("p1", T0)
    
    # 6. CURRENCY RCA candidate matches a payment with the same currency
    assert snapshot.rca_candidate_matches_payment_segment is True
    assert snapshot.rca_candidate_dimension == "CURRENCY"
    assert snapshot.rca_candidate_value == "INR"
    assert snapshot.rca_evidence_strength == "STRONG"
    
    # 7. Non-matching CURRENCY candidate remains false
    event2 = make_event("p2", currency="USD", ingested_at=T0 - timedelta(seconds=1))
    session.add(event2)
    await session.commit()
    snapshot2 = await service.reconstruct_features("p2", T0)
    assert snapshot2.rca_candidate_matches_payment_segment is False
    
    # 8. Existing BANK/WALLET matching is unchanged
    assert service._matches_segment(event, "BANK", "HDFC") is False
    assert service._matches_segment(make_event("p3", bank="HDFC"), "BANK", "HDFC") is True
    
    # 11. No future episode state becomes visible
    ep_fut = make_episode("GLOBAL", "ALL", "CRITICAL", T0 + timedelta(minutes=1), T0 + timedelta(minutes=1))
    session.add(ep_fut)
    await session.commit()
    
    snapshot3 = await service.reconstruct_features("p1", T0)
    # Still picks up the MODERATE one, not the CRITICAL future one
    assert snapshot3.degradation_severity == "MODERATE"
