"""
Integration tests for Stage 5-8 provider-exact reads, composite identity joins,
and cross-provider conflict detection against PostgreSQL.
"""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.execution_models import (
    CommandStatus,
    ExecutionResultStatus,
    PaymentOutcome,
)
from src.core.domain.intervention_models import DecisionType
from src.core.domain.payment_attempt_exceptions import AttemptProviderConflictError
from src.core.domain.payment_attempts import AttemptRef
from src.core.execution.execution_service import InterventionExecutionService
from src.core.execution.observation_service import PaymentOutcomeObservationService
from src.core.execution.simulator import SimulatorInterventionExecutor, SimulatorMode
from src.core.intervention.service import InterventionDecisionService
from src.core.ml.model import FailurePredictionModel as StageFiveModel
from src.core.services.failure_prediction_service import FailurePredictionService
from src.core.services.feature_reconstruction_service import FeatureReconstructionService
from src.infrastructure.attribution_repository import AttributionRepository
from src.infrastructure.execution_repository import ExecutionRepository
from src.infrastructure.failure_prediction_repository import FailurePredictionRepository
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.infrastructure.intervention_repository import InterventionRepository
from src.infrastructure.models import (
    FailurePredictionModel,
    InterventionCommandModel,
    InterventionDecisionModel,
    InterventionExecutionAttemptModel,
    PaymentAttemptModel,
    PaymentEventModel,
    PaymentOutcomeObservationModel,
)
from tests.integration.attempt_registry import register_attempt

T = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


class FixedProbTestModel(StageFiveModel):
    model_name = "FixedProbTestModel"
    model_version = "v1"

    def predict(self, feature_vector):
        return 0.9

    def get_feature_schema_version(self):
        return "v1.0.0"


def _event(source_system, event_type, payment_id, at, order_id=None):
    return PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system=source_system,
        source_event_id=f"evt_{uuid.uuid4().hex}",
        payment_id=payment_id,
        order_id=order_id,
        timestamp=at,
        event_type=event_type,
        currency="INR",
        amount_minor_units=50000,
        payment_status=event_type.split(".")[-1],
        payment_method="card",
        ingested_at=at,
    )


def _seed_attempt(session, provider, key):
    if provider == "paypal":
        order_id = key.removeprefix("paypal-order:")
        session.add(_event("paypal", "order.approved", order_id, T - timedelta(seconds=1)))
        for i in range(50):
            session.add(_event("paypal", "payment.captured", f"CAPVOL_{i:04d}", T - timedelta(minutes=10), f"ORDVOL_{i:04d}"))
    else:
        session.add(_event(provider, "payment.authorized", key, T - timedelta(seconds=1)))
        for i in range(50):
            session.add(_event(provider, "payment.captured", f"pay_vol_{provider}_{i:04d}", T - timedelta(minutes=10)))


# ============================================================================
# 1. Identical reference under two providers resolves provider-exact
# ============================================================================

async def test_identical_reference_under_two_providers_resolves_provider_exact(db_session: AsyncSession):
    shared_ref = "ORD_SHARED_REF_001"
    razor_key = shared_ref
    paypal_key = f"paypal-order:{shared_ref}"

    # Seed and register Razorpay attempt
    _seed_attempt(db_session, "razorpay", razor_key)
    ref_razor = await register_attempt(db_session, razor_key)
    assert ref_razor.source_system == "razorpay"
    assert ref_razor.provider_reference == shared_ref

    # Seed and register PayPal attempt sharing the identical reference
    _seed_attempt(db_session, "paypal", paypal_key)
    ref_paypal = await register_attempt(db_session, paypal_key)
    assert ref_paypal.source_system == "paypal"
    assert ref_paypal.provider_reference == shared_ref

    feature_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    int_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)

    # Orchestrate pipeline for Razorpay
    pred_razor = await FailurePredictionService(
        db_session, FeatureReconstructionService(feature_repo), pred_repo, FixedProbTestModel()
    ).orchestrate_prediction(ref_razor, T)

    dec_razor = await InterventionDecisionService(
        db_session, int_repo, feature_repo
    ).decide(ref_razor, T)

    exec_service = InterventionExecutionService(
        db_session, exec_repo, SimulatorInterventionExecutor(SimulatorMode.ALWAYS_SUCCEED)
    )
    cmd_razor = await exec_service.create_command(dec_razor, t_create=T)

    # Orchestrate pipeline for PayPal
    pred_paypal = await FailurePredictionService(
        db_session, FeatureReconstructionService(feature_repo), pred_repo, FixedProbTestModel()
    ).orchestrate_prediction(ref_paypal, T)

    dec_paypal = await InterventionDecisionService(
        db_session, int_repo, feature_repo
    ).decide(ref_paypal, T)

    cmd_paypal = await exec_service.create_command(dec_paypal, t_create=T)

    # Verify provider-exact resolution for FailurePredictionRepository
    read_pred_razor = await pred_repo.get_latest_prediction(razor_key, source_system="razorpay")
    read_pred_paypal = await pred_repo.get_latest_prediction(paypal_key, source_system="paypal")
    assert read_pred_razor is not None and read_pred_razor.prediction_id == pred_razor.prediction_id
    assert read_pred_paypal is not None and read_pred_paypal.prediction_id == pred_paypal.prediction_id
    assert read_pred_razor.prediction_id != read_pred_paypal.prediction_id

    # Verify cross-querying with wrong provider returns None
    assert await pred_repo.get_latest_prediction(razor_key, source_system="paypal") is None
    assert await pred_repo.get_latest_prediction(paypal_key, source_system="razorpay") is None

    # Verify provider-exact resolution for InterventionRepository
    read_dec_razor = await int_repo.get_latest_decision(razor_key, source_system="razorpay")
    read_dec_paypal = await int_repo.get_latest_decision(paypal_key, source_system="paypal")
    assert read_dec_razor is not None and read_dec_razor.decision_id == dec_razor.decision_id
    assert read_dec_paypal is not None and read_dec_paypal.decision_id == dec_paypal.decision_id
    assert read_dec_razor.decision_id != read_dec_paypal.decision_id

    # Cross-querying returns None
    assert await int_repo.get_latest_decision(razor_key, source_system="paypal") is None
    assert await int_repo.get_latest_decision(paypal_key, source_system="razorpay") is None

    # Verify provider-exact resolution for ExecutionRepository
    read_cmd_razor = await exec_repo.get_command_by_attempt_and_version(razor_key, 1, source_system="razorpay")
    read_cmd_paypal = await exec_repo.get_command_by_attempt_and_version(paypal_key, 1, source_system="paypal")
    assert read_cmd_razor is not None and read_cmd_razor.command_id == cmd_razor.command_id
    assert read_cmd_paypal is not None and read_cmd_paypal.command_id == cmd_paypal.command_id
    assert read_cmd_razor.command_id != read_cmd_paypal.command_id

    # Cross-querying returns None
    assert await exec_repo.get_command_by_attempt_and_version(razor_key, 1, source_system="paypal") is None
    assert await exec_repo.get_command_by_attempt_and_version(paypal_key, 1, source_system="razorpay") is None


# ============================================================================
# 2. Reading under the wrong provider returns None (does not fall back)
# ============================================================================

async def test_reading_under_wrong_provider_returns_none(db_session: AsyncSession):
    key = "pay_WRONG_PROV_001"
    _seed_attempt(db_session, "razorpay", key)
    ref = await register_attempt(db_session, key)

    feature_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    int_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)

    pred = await FailurePredictionService(
        db_session, FeatureReconstructionService(feature_repo), pred_repo, FixedProbTestModel()
    ).orchestrate_prediction(ref, T)

    dec = await InterventionDecisionService(
        db_session, int_repo, feature_repo
    ).decide(ref, T)

    cmd = await InterventionExecutionService(
        db_session, exec_repo, SimulatorInterventionExecutor(SimulatorMode.ALWAYS_SUCCEED)
    ).create_command(dec, t_create=T)

    # Reading with wrong provider "synthetic" or "paypal" returns None
    assert await pred_repo.get_latest_prediction(key, source_system="synthetic") is None
    assert await pred_repo.get_latest_prediction(key, source_system="paypal") is None

    assert await int_repo.get_latest_decision(key, source_system="synthetic") is None
    assert await int_repo.get_latest_decision(key, source_system="paypal") is None

    assert await exec_repo.get_command_by_attempt_and_version(key, 1, source_system="synthetic") is None
    assert await exec_repo.get_command_by_attempt_and_version(key, 1, source_system="paypal") is None


# ============================================================================
# 3. Missing registry entries rejected by database FK constraints under Revision B
# (Preserved fail-closed repository read assertions run under Revision A in test_revision_a.py)
# ============================================================================

async def test_unregistered_attempt_records_rejected_at_the_database(db_session: AsyncSession):
    unreg_key = "pay_UNREG_READ_001"
    pred_model = FailurePredictionModel(
        prediction_id=uuid.uuid4(),
        payment_attempt_id=unreg_key,
        source_system="razorpay",
        prediction_version=1,
        predicted_at=T,
        prediction_horizon="15m",
        model_name="test",
        model_version="1",
        feature_schema_version="v1.0.0",
        failure_probability=0.8,
        prediction_status="PREDICTED",
        feature_snapshot={},
        input_fingerprint="fp1",
        created_at=T,
    )
    dec_model = InterventionDecisionModel(
        decision_id=uuid.uuid4(),
        payment_attempt_id=unreg_key,
        source_system="razorpay",
        decision_version=1,
        decided_at=T,
        decision_type="MONITOR",
        selected_route_id=None,
        failure_probability=0.8,
        stage5_prediction_id=pred_model.prediction_id,
        diagnosis_confidence="HIGH",
        decision_confidence=1.0,
        gate_verdict="PASSED",
        policy_id="pol1",
        policy_version="1",
        input_fingerprint="fp2",
        evaluation_audit_payload={},
        created_at=T,
    )
    cmd_model = InterventionCommandModel(
        command_id=uuid.uuid4(),
        decision_id=dec_model.decision_id,
        payment_attempt_id=unreg_key,
        source_system="razorpay",
        decision_version=1,
        route_key="RETRY_SECONDARY_GATEWAY",
        policy_id="pol1",
        policy_version="1",
        command_status="SUCCEEDED",
        status_reason=None,
        idempotency_key="idemp_unreg",
        created_at=T,
        expires_at=T,
        updated_at=T,
    )
    # Verify each Stage 5-7 model is rejected by its specific composite foreign key
    with pytest.raises(IntegrityError, match="fk_failure_predictions_payment_attempt"):
        async with db_session.begin_nested():
            db_session.add(pred_model)
            await db_session.flush()

    with pytest.raises(IntegrityError, match="fk_intervention_decisions_payment_attempt"):
        async with db_session.begin_nested():
            db_session.add(dec_model)
            await db_session.flush()

    with pytest.raises(IntegrityError, match="fk_intervention_commands_payment_attempt"):
        async with db_session.begin_nested():
            db_session.add(cmd_model)
            await db_session.flush()


# ============================================================================
# 4. Execution attempts with mismatched provider rejected by database FK constraints
# (Preserved repository conflict detection runs under Revision A in test_revision_a.py)
# ============================================================================

async def test_mismatched_execution_attempt_rejected_at_the_database(db_session: AsyncSession):
    key = "pay_EXEC_MISMATCH_001"
    _seed_attempt(db_session, "razorpay", key)
    ref = await register_attempt(db_session, key)

    feature_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    int_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)

    await FailurePredictionService(
        db_session, FeatureReconstructionService(feature_repo), pred_repo, FixedProbTestModel()
    ).orchestrate_prediction(ref, T)

    dec = await InterventionDecisionService(
        db_session, int_repo, feature_repo
    ).decide(ref, T)
    assert dec.decision_type == DecisionType.ACT

    cmd = await InterventionExecutionService(
        db_session, exec_repo, SimulatorInterventionExecutor(SimulatorMode.ALWAYS_SUCCEED)
    ).create_command(dec, t_create=T)

    # Directly insert execution attempt with conflicting source_system ("synthetic")
    mismatched_attempt = InterventionExecutionAttemptModel(
        attempt_id=uuid.uuid4(),
        command_id=cmd.command_id,
        source_system="synthetic",  # Conflicts with command's "razorpay"
        attempt_number=1,
        started_at=T,
        completed_at=T,
        duration_ms=100,
        execution_status=ExecutionResultStatus.SUCCESS.value,
        executor_name="sim",
        is_simulation=True,
        provider_action_type="RETRY",
        provider_response_code=None,
        provider_response_payload=None,
        error_message=None,
        created_at=T,
    )
    with pytest.raises(IntegrityError, match="fk_exec_attempt_command"):
        async with db_session.begin_nested():
            db_session.add(mismatched_attempt)
            await db_session.flush()


# ============================================================================
# 5. Outcome observations with mismatched provider rejected by database FK constraints
# (Preserved repository conflict detection runs under Revision A in test_revision_a.py)
# ============================================================================

async def test_mismatched_observation_rejected_at_the_database(db_session: AsyncSession):
    key = "pay_OBS_MISMATCH_001"
    _seed_attempt(db_session, "razorpay", key)
    ref = await register_attempt(db_session, key)

    feature_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    int_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)

    await FailurePredictionService(
        db_session, FeatureReconstructionService(feature_repo), pred_repo, FixedProbTestModel()
    ).orchestrate_prediction(ref, T)

    dec = await InterventionDecisionService(
        db_session, int_repo, feature_repo
    ).decide(ref, T)
    assert dec.decision_type == DecisionType.ACT

    cmd = await InterventionExecutionService(
        db_session, exec_repo, SimulatorInterventionExecutor(SimulatorMode.ALWAYS_SUCCEED)
    ).create_command(dec, t_create=T)

    # Directly insert observation with conflicting source_system ("synthetic")
    mismatched_obs = PaymentOutcomeObservationModel(
        observation_id=uuid.uuid4(),
        command_id=cmd.command_id,
        payment_attempt_id=key,
        source_system="synthetic",  # Conflicts with command's "razorpay"
        observation_version=1,
        observed_at=T,
        as_of_timestamp=T,
        payment_outcome=PaymentOutcome.CAPTURED.value,
        terminal_event_id=uuid.uuid4(),
        terminal_event_type="payment.captured",
        terminal_event_timestamp=T,
        time_to_outcome_ms=500,
        observation_audit_payload={},
        created_at=T,
    )
    with pytest.raises(IntegrityError, match="fk_payment_outcome_observations_payment_attempt"):
        async with db_session.begin_nested():
            db_session.add(mismatched_obs)
            await db_session.flush()


# ============================================================================
# 6. Correct reads across all 3 pipeline providers (razorpay, synthetic, paypal)
# ============================================================================

@pytest.mark.parametrize("provider, key", [
    ("razorpay", "pay_ReadTest000001"),
    ("synthetic", "pay_ReadTest000002"),
    ("paypal", "paypal-order:ORDERREAD0001"),
])
async def test_correct_reads_across_all_providers(db_session: AsyncSession, provider, key):
    _seed_attempt(db_session, provider, key)
    ref = await register_attempt(db_session, key)
    assert ref.source_system == provider

    feature_repo = FeatureReconstructionRepository(db_session)
    pred_repo = FailurePredictionRepository(db_session)
    int_repo = InterventionRepository(db_session)
    exec_repo = ExecutionRepository(db_session)

    pred = await FailurePredictionService(
        db_session, FeatureReconstructionService(feature_repo), pred_repo, FixedProbTestModel()
    ).orchestrate_prediction(ref, T)

    dec = await InterventionDecisionService(
        db_session, int_repo, feature_repo
    ).decide(ref, T)

    cmd = await InterventionExecutionService(
        db_session, exec_repo, SimulatorInterventionExecutor(SimulatorMode.ALWAYS_SUCCEED)
    ).create_command(dec, t_create=T)

    # Provider-exact read for each provider succeeds and returns the created record
    read_pred = await pred_repo.get_latest_prediction(key, source_system=provider)
    assert read_pred is not None
    assert read_pred.prediction_id == pred.prediction_id
    assert read_pred.source_system == provider

    read_dec = await int_repo.get_latest_decision(key, source_system=provider)
    assert read_dec is not None
    assert read_dec.decision_id == dec.decision_id
    assert read_dec.source_system == provider

    read_cmd = await exec_repo.get_command_by_attempt_and_version(key, 1, source_system=provider)
    assert read_cmd is not None
    assert read_cmd.command_id == cmd.command_id
    assert read_cmd.source_system == provider
