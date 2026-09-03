import pytest
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.infrastructure.models import (
    Base, PaymentEventModel, EpisodeStateHistoryModel, 
    RCAEvaluationModel, CandidateCauseModel, FailurePredictionModel
)
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.core.services.feature_reconstruction_service import FeatureReconstructionService
from src.core.intervention.service import InterventionDecisionService
from src.core.intervention.policy import get_default_policy

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
def feature_repo(session: AsyncSession):
    return FeatureReconstructionRepository(session)

@pytest.fixture
def feature_service(feature_repo):
    return FeatureReconstructionService(feature_repo)

class MockInterventionRepository:
    def __init__(self, pred_map=None):
        self.pred_map = pred_map or {}

    async def get_latest_decision(self, payment_attempt_id):
        return None
    async def get_max_decision_version(self, payment_attempt_id):
        return 0
    async def acquire_version_allocation_lock(self, payment_attempt_id):
        return True
    async def has_recent_route_decision(self, payment_attempt_id, route_key, since_timestamp):
        return False
    async def save_decision(self, decision):
        return decision
    async def check_terminal_event_exists(self, payment_attempt_id, t_decide):
        return False
    async def get_stage5_prediction_as_of(self, payment_attempt_id, t_decide):
        return self.pred_map.get(payment_attempt_id)
    async def acquire_global_rate_limit_lock(self):
        return True
    async def check_global_rate_limit(self, since_timestamp, max_allowed):
        return True
    async def count_recent_act_decisions(self, since_timestamp):
        return 0
    async def append_decision(self, decision):
        return decision

@pytest.fixture
def intervention_service(session: AsyncSession, feature_repo):
    # We will instantiate it per test with the specific pred_map
    pass

T0 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

def make_event(payment_id, bank=None, wallet=None, currency="INR", pm=None, ingested_at=T0):
    return PaymentEventModel(
        event_id=uuid.uuid4(), source_system="TEST", source_event_id=str(uuid.uuid4()),
        payment_id=payment_id, timestamp=ingested_at, event_type="payment.authorized",
        bank=bank, wallet=wallet, currency=currency, payment_method=pm,
        amount_minor_units=1000, payment_status="AUTHORIZED", ingested_at=ingested_at
    )

def make_episode(dim, val, severity, start_time, eval_time):
    return EpisodeStateHistoryModel(
        reconciliation_run_id=uuid.uuid4(), episode_id=uuid.uuid4(),
        segment_dimension=dim, segment_value=val, status="ACTIVE",
        severity=severity, effective_start_window=start_time, evaluation_timestamp=eval_time
    )

def make_rca(episode_id, gen_time, cand_dim, cand_val, classification="SEGMENT_SPECIFIC", traffic_mix_shift=False):
    eval_id = uuid.uuid4()
    rca = RCAEvaluationModel(
        evaluation_id=eval_id, episode_id=episode_id, generated_at=gen_time, evaluation_version=1,
        analysis_window_start=gen_time - timedelta(minutes=5), analysis_window_end=gen_time,
        input_fingerprint="sha256", evidence_audit_payload={"traffic_mix_shift": traffic_mix_shift}, classification=classification
    )
    if classification == "SEGMENT_SPECIFIC":
        cand1 = CandidateCauseModel(
            candidate_id=uuid.uuid4(), evaluation_id=eval_id, candidate_dimension=cand_dim,
            candidate_value=cand_val, rank=1, evidence_strength="STRONG",
            actual_segment_failures=100, expected_segment_failures=0.0, excess_segment_failures=100, excess_failure_contribution=1.0
        )
        return rca, cand1
    return rca, None

@pytest.mark.asyncio
async def test_stage5_stage6_boundary_episode_resolution(session: AsyncSession, feature_repo: FeatureReconstructionRepository, feature_service: FeatureReconstructionService):
    t_eval = T0 - timedelta(minutes=1)
    
    # Insert multiple active episodes
    ep_wallet = make_episode("WALLET", "PAYTM", "CRITICAL", T0 - timedelta(minutes=5), t_eval)
    ep_global = make_episode("GLOBAL", "ALL", "HIGH", T0 - timedelta(minutes=10), t_eval)
    
    rca_w, cand_w = make_rca(ep_wallet.episode_id, t_eval, "WALLET", "PAYTM", traffic_mix_shift=True)
    rca_g, _ = make_rca(ep_global.episode_id, t_eval, None, None, classification="SYSTEMIC")
    
    session.add_all([ep_wallet, ep_global, rca_w, cand_w, rca_g])
    
    payment_id = "PAY_123"
    auth_event = make_event(payment_id, wallet="PAYTM", ingested_at=T0)
    session.add(auth_event)
    await session.commit()
    
    # 1. Verify Stage 5 resolves CRITICAL WALLET episode
    features = await feature_service.reconstruct_features(payment_id, T0)
    assert features.degradation_severity == "CRITICAL"
    assert features.rca_candidate_dimension == "WALLET"
    assert features.rca_candidate_matches_payment_segment is True
    
    # 2. Verify Stage 6 resolves exactly the SAME episode without duplicating logic
    stage5_pred = FailurePredictionModel(
        prediction_id=uuid.uuid4(), payment_attempt_id=payment_id, prediction_version=1, predicted_at=T0,
        failure_probability=0.85, prediction_status="PREDICTED",
        feature_snapshot={
            "rca_candidate_matches_payment_segment": True
        }
    )
    
    intervention_service = InterventionDecisionService(
        session=session,
        intervention_repository=MockInterventionRepository({payment_id: stage5_pred}),
        feature_reconstruction_repository=feature_repo,
        policy=get_default_policy()
    )
    
    decision = await intervention_service.decide(
        payment_attempt_id=payment_id, t_decide=T0
    )
    
    # Decision should fail due to FAILED_UNRELIABLE_DIAGNOSIS (traffic_mix_shift=True) 
    # which proves it fetched the correct RCA audit payload for the WALLET episode.
    assert decision.gate_verdict.name == "FAILED_UNRELIABLE_DIAGNOSIS"
    assert decision.evaluation_audit_payload["gates"]["episode_and_traffic_shift"]["passed"] is False

@pytest.mark.asyncio
async def test_stage5_stage6_boundary_currency_episode(session: AsyncSession, feature_repo: FeatureReconstructionRepository, feature_service: FeatureReconstructionService):
    t_eval = T0 - timedelta(minutes=1)
    
    # Insert multiple active episodes
    ep_currency = make_episode("CURRENCY", "USD", "HIGH", T0 - timedelta(minutes=8), t_eval)
    ep_bank = make_episode("BANK", "CHASE", "HIGH", T0 - timedelta(minutes=2), t_eval) # Newer
    
    # Older episode wins ties (CURRENCY)
    rca_c, cand_c = make_rca(ep_currency.episode_id, t_eval, "CURRENCY", "USD")
    rca_b, cand_b = make_rca(ep_bank.episode_id, t_eval, "BANK", "CHASE")
    
    session.add_all([ep_currency, ep_bank, rca_c, cand_c, rca_b, cand_b])
    
    payment_id = "PAY_456"
    auth_event = make_event(payment_id, currency="USD", bank="CHASE", ingested_at=T0)
    session.add(auth_event)
    await session.commit()
    
    # 1. Verify Stage 5 resolves HIGH CURRENCY episode (older start window)
    features = await feature_service.reconstruct_features(payment_id, T0)
    assert features.rca_candidate_dimension == "CURRENCY"
    assert features.degradation_severity == "HIGH"
    
    stage5_pred = FailurePredictionModel(
        prediction_id=uuid.uuid4(), payment_attempt_id=payment_id, prediction_version=1, predicted_at=T0,
        failure_probability=0.85, prediction_status="PREDICTED",
        feature_snapshot={
            "rca_candidate_matches_payment_segment": True
        }
    )
    
    intervention_service = InterventionDecisionService(
        session=session,
        intervention_repository=MockInterventionRepository({payment_id: stage5_pred}),
        feature_reconstruction_repository=feature_repo,
        policy=get_default_policy()
    )
    
    # 2. Verify Stage 6 resolves exactly the SAME episode (CURRENCY)
    decision = await intervention_service.decide(
        payment_attempt_id=payment_id, t_decide=T0
    )
    
    # Verify fingerprint contains CURRENCY
    assert decision.evaluation_audit_payload["upstream_context"]["rca_candidate_dimension"] == "CURRENCY"
