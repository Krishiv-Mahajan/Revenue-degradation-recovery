"""
Database-backed write provenance: for each provider, one attempt runs through every Stage 5-8 write
(prediction, ACT decision, command, simulated execution, observation, attribution) and every row
stores the provider it is registered under. Writes for unregistered attempts leave no rows, and a
stored row without a provider is never read back with a guessed one.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.attribution.attribution_service import CounterfactualAttributionService
from src.core.domain.intervention_models import DecisionType
from src.core.domain.payment_attempt_exceptions import AttemptNotRegisteredError, ProviderIdentityMissingError
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
    CounterfactualAttributionModel,
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
STAGE_5_TO_8 = (
    FailurePredictionModel,
    InterventionDecisionModel,
    InterventionCommandModel,
    InterventionExecutionAttemptModel,
    PaymentOutcomeObservationModel,
    CounterfactualAttributionModel,
)


class SyntheticFixedProbabilityTestModel(StageFiveModel):
    """Test double. Its name contains "synthetic", so Stage 8 weighs it as SYNTHETIC_DEVELOPMENT."""

    model_name = "SyntheticFixedProbabilityTestModel"
    model_version = "fixed-0.9"

    def predict(self, feature_vector):
        return 0.9

    def get_feature_schema_version(self):
        return "v1.0.0"


def _event(source_system, event_type, payment_id, at, order_id=None):
    return PaymentEventModel(
        event_id=uuid.uuid4(), source_system=source_system, source_event_id=f"evt_{uuid.uuid4().hex}",
        payment_id=payment_id, order_id=order_id, timestamp=at, event_type=event_type, currency="INR",
        amount_minor_units=50000, payment_status=event_type.split(".")[-1], payment_method="card", ingested_at=at,
    )


def _seed_attempt(session, provider, key):
    """The attempt's trigger plus 50 earlier captures of other payments, enough Stage 5 volume to predict."""
    if provider == "paypal":
        order_id = key.removeprefix("paypal-order:")
        session.add(_event("paypal", "order.approved", order_id, T - timedelta(seconds=1)))
        for i in range(50):
            session.add(_event("paypal", "payment.captured", f"CAPVOL{i:04d}", T - timedelta(minutes=10), f"ORDVOL{i:04d}"))
    else:
        session.add(_event(provider, "payment.authorized", key, T - timedelta(seconds=1)))
        for i in range(50):
            session.add(_event(provider, "payment.captured", f"pay_vol_{provider}_{i:04d}", T - timedelta(minutes=10)))


def _terminal(provider, key, at):
    if provider == "paypal":
        return _event("paypal", "payment.captured", "CAPTUREPROV01", at, order_id=key.removeprefix("paypal-order:"))
    return _event(provider, "payment.captured", key, at)


@pytest.mark.parametrize("provider, key", [
    ("razorpay", "pay_Prov00000001"),
    ("synthetic", "pay_Prov00000002"),
    ("paypal", "paypal-order:ORDERPROV0001"),
])
async def test_every_stage_5_to_8_row_stores_the_registered_provider(db_session: AsyncSession, provider, key):
    _seed_attempt(db_session, provider, key)
    attempt = await register_attempt(db_session, key)
    assert attempt == AttemptRef.for_key(provider, key)

    feature_repo = FeatureReconstructionRepository(db_session)
    execution_repo = ExecutionRepository(db_session)
    prediction = await FailurePredictionService(
        db_session, FeatureReconstructionService(feature_repo), FailurePredictionRepository(db_session),
        SyntheticFixedProbabilityTestModel(),
    ).orchestrate_prediction(attempt, T)
    decision = await InterventionDecisionService(
        db_session, InterventionRepository(db_session), feature_repo
    ).decide(attempt, T)
    assert (prediction.failure_probability, decision.decision_type) == (0.9, DecisionType.ACT)

    execution = InterventionExecutionService(
        db_session, execution_repo, SimulatorInterventionExecutor(SimulatorMode.ALWAYS_SUCCEED)
    )
    command = await execution.create_command(decision, t_create=T)
    await execution.execute_command(command.command_id, t_claim=T)
    terminal_at = T + timedelta(seconds=30)
    db_session.add(_terminal(provider, key, terminal_at))
    await db_session.flush()
    observation = await PaymentOutcomeObservationService(db_session, execution_repo).observe(
        command.command_id, as_of_timestamp=terminal_at
    )
    attribution = await CounterfactualAttributionService(AttributionRepository(db_session)).attribute_payment_intervention(
        command.command_id, as_of_timestamp=terminal_at
    )

    assert observation.payment_outcome.value == "CAPTURED"
    assert {prediction.source_system, decision.source_system, command.source_system,
            observation.source_system, attribution.source_system} == {provider}
    registered = (await db_session.execute(
        select(PaymentAttemptModel.source_system).where(PaymentAttemptModel.payment_attempt_id == key)
    )).scalar_one()
    assert registered == provider
    for model in STAGE_5_TO_8:
        sources = (await db_session.execute(select(model.source_system))).scalars().all()
        assert sources == [provider], model.__tablename__


async def test_writes_for_an_unregistered_attempt_leave_no_rows(db_session: AsyncSession):
    _seed_attempt(db_session, "razorpay", "pay_Prov00000003")
    await db_session.flush()
    attempt = AttemptRef.for_key("razorpay", "pay_Prov00000003")
    feature_repo = FeatureReconstructionRepository(db_session)

    with pytest.raises(AttemptNotRegisteredError):
        await FailurePredictionService(
            db_session, FeatureReconstructionService(feature_repo), FailurePredictionRepository(db_session),
            SyntheticFixedProbabilityTestModel(),
        ).orchestrate_prediction(attempt, T)
    with pytest.raises(AttemptNotRegisteredError):
        await InterventionDecisionService(db_session, InterventionRepository(db_session), feature_repo).decide(attempt, T)

    for model in (FailurePredictionModel, InterventionDecisionModel):
        assert (await db_session.execute(select(func.count()).select_from(model))).scalar_one() == 0


async def test_a_stored_row_without_a_provider_is_never_read_back(db_session: AsyncSession):
    db_session.add(FailurePredictionModel(
        prediction_id=uuid.uuid4(), payment_attempt_id="pay_Prov00000004", source_system=None,
        prediction_version=1, predicted_at=T, prediction_horizon="30m", failure_probability=0.5,
        risk_band="ELEVATED", prediction_status="PREDICTED", model_name="m", model_version="1",
        feature_schema_version="1", feature_snapshot={}, input_fingerprint="sha256:x", created_at=T,
    ))
    await db_session.flush()

    with pytest.raises(ProviderIdentityMissingError, match="has no source_system"):
        await FailurePredictionRepository(db_session).get_latest_prediction("pay_Prov00000004")
