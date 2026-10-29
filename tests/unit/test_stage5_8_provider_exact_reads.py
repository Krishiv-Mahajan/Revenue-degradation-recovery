"""
Unit tests for Stage 5-8 provider-exact reads, provider identity validation,
and cross-provider conflict detection.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from src.core.attribution.attribution_service import CounterfactualAttributionService
from src.core.domain.execution_models import (
    CommandStatus,
    ExecutionResultStatus,
    InterventionCommand,
    PaymentOutcome,
)
from src.core.domain.intervention_models import InterventionRouteKey
from src.core.domain.payment_attempt_exceptions import (
    AttemptProviderConflictError,
    ProviderIdentityMissingError,
    UnsupportedProviderError,
)
from src.core.domain.payment_attempts import (
    validate_provider,
)
from src.infrastructure.attribution_repository import AttributionRepository
from src.infrastructure.execution_repository import ExecutionRepository
from src.infrastructure.failure_prediction_repository import FailurePredictionRepository
from src.infrastructure.intervention_repository import InterventionRepository
from src.infrastructure.models import (
    CounterfactualAttributionModel,
    InterventionCommandModel,
    InterventionExecutionAttemptModel,
    PaymentOutcomeObservationModel,
)


T = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


# ============================================================================
# 1. validate_provider unit tests
# ============================================================================

def test_validate_provider_accepts_supported_providers():
    assert validate_provider("razorpay") == "razorpay"
    assert validate_provider("synthetic") == "synthetic"
    assert validate_provider("paypal") == "paypal"


def test_validate_provider_rejects_none_with_missing_error():
    with pytest.raises(ProviderIdentityMissingError):
        validate_provider(None)


def test_validate_provider_rejects_unsupported_providers():
    for unsupported in ["stripe", "adyen", "unknown", "", "RAZORPAY"]:
        with pytest.raises(UnsupportedProviderError):
            validate_provider(unsupported)


# ============================================================================
# 2. Repository provider validation unit tests
# ============================================================================

@pytest.mark.asyncio
async def test_failure_prediction_repo_rejects_invalid_provider():
    session = AsyncMock()
    repo = FailurePredictionRepository(session)
    with pytest.raises(UnsupportedProviderError):
        await repo.get_latest_prediction("pay_1", source_system="stripe")
    with pytest.raises(UnsupportedProviderError):
        await repo.get_max_prediction_version("pay_1", source_system="stripe")


@pytest.mark.asyncio
async def test_intervention_repo_rejects_invalid_provider():
    session = AsyncMock()
    repo = InterventionRepository(session)
    with pytest.raises(UnsupportedProviderError):
        await repo.get_latest_decision("pay_1", source_system="stripe")
    with pytest.raises(UnsupportedProviderError):
        await repo.get_max_decision_version("pay_1", source_system="stripe")
    with pytest.raises(UnsupportedProviderError):
        await repo.has_recent_route_decision("pay_1", "ROUTE_A", T, source_system="stripe")
    with pytest.raises(UnsupportedProviderError):
        await repo.check_terminal_event_exists("pay_1", T, source_system="stripe")
    with pytest.raises(UnsupportedProviderError):
        await repo.get_stage5_prediction_as_of("pay_1", T, source_system="stripe")


@pytest.mark.asyncio
async def test_execution_repo_rejects_invalid_provider():
    session = AsyncMock()
    repo = ExecutionRepository(session)
    with pytest.raises(UnsupportedProviderError):
        await repo.get_command_by_attempt_and_version("pay_1", 1, source_system="stripe")
    with pytest.raises(UnsupportedProviderError):
        await repo.check_terminal_event_exists_before("pay_1", T, source_system="stripe")
    with pytest.raises(UnsupportedProviderError):
        await repo.get_canonical_terminal_events("pay_1", T, source_system="stripe")


@pytest.mark.asyncio
async def test_attribution_repo_rejects_invalid_provider():
    session = AsyncMock()
    repo = AttributionRepository(session)
    with pytest.raises(UnsupportedProviderError):
        await repo.get_canonical_payment_amount("pay_1", T, source_system="stripe")
    with pytest.raises(UnsupportedProviderError):
        await repo.get_latest_attribution(uuid.uuid4(), source_system="stripe")


# ============================================================================
# 3. ExecutionRepository provider mismatch detection
# ============================================================================

@pytest.mark.asyncio
async def test_execution_repo_detects_mismatched_execution_attempt_provider():
    cmd_id = uuid.uuid4()
    cmd = InterventionCommand(
        command_id=cmd_id,
        decision_id=uuid.uuid4(),
        payment_attempt_id="pay_1",
        source_system="razorpay",
        decision_version=1,
        route_key=InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
        policy_id="pol_1",
        policy_version="1.0",
        command_status=CommandStatus.SUCCEEDED,
        status_reason=None,
        idempotency_key="idemp_1",
        created_at=T,
        expires_at=T,
        updated_at=T,
    )
    attempt_model = InterventionExecutionAttemptModel(
        attempt_id=uuid.uuid4(),
        command_id=cmd_id,
        source_system="synthetic",  # Mismatch: synthetic vs razorpay
        attempt_number=1,
        started_at=T,
        completed_at=T,
        duration_ms=100,
        execution_status=ExecutionResultStatus.SUCCESS.value,
        executor_name="sim",
        is_simulation=True,
        provider_action_type=None,
        provider_response_code=None,
        provider_response_payload=None,
        error_message=None,
        created_at=T,
    )

    session = AsyncMock()
    repo = ExecutionRepository(session)
    repo.get_command_by_id = AsyncMock(return_value=cmd)

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [attempt_model]
    exec_result_mock = MagicMock()
    exec_result_mock.scalars.return_value = scalars_mock
    session.execute.return_value = exec_result_mock

    with pytest.raises(AttemptProviderConflictError, match="conflicts with command"):
        await repo.get_execution_attempts(cmd_id)


@pytest.mark.asyncio
async def test_execution_repo_detects_mismatched_observation_provider():
    cmd_id = uuid.uuid4()
    cmd = InterventionCommand(
        command_id=cmd_id,
        decision_id=uuid.uuid4(),
        payment_attempt_id="pay_1",
        source_system="razorpay",
        decision_version=1,
        route_key=InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
        policy_id="pol_1",
        policy_version="1.0",
        command_status=CommandStatus.SUCCEEDED,
        status_reason=None,
        idempotency_key="idemp_1",
        created_at=T,
        expires_at=T,
        updated_at=T,
    )
    obs_model = PaymentOutcomeObservationModel(
        observation_id=uuid.uuid4(),
        command_id=cmd_id,
        payment_attempt_id="pay_1",
        source_system="paypal",  # Mismatch: paypal vs razorpay
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

    session = AsyncMock()
    repo = ExecutionRepository(session)
    repo.get_command_by_id = AsyncMock(return_value=cmd)

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = obs_model
    session.execute.return_value = result_mock

    with pytest.raises(AttemptProviderConflictError, match="conflicts with command"):
        await repo.get_latest_observation(cmd_id)


# ============================================================================
# 4. AttributionRepository provider mismatch detection
# ============================================================================

@pytest.mark.asyncio
async def test_attribution_repo_detects_mismatched_attribution_provider():
    cmd_id = uuid.uuid4()
    cmd_model = InterventionCommandModel(
        command_id=cmd_id,
        decision_id=uuid.uuid4(),
        payment_attempt_id="pay_1",
        source_system="razorpay",
        decision_version=1,
        route_key="RETRY_SECONDARY_GATEWAY",
        policy_id="pol_1",
        policy_version="1.0",
        command_status="SUCCEEDED",
        status_reason=None,
        idempotency_key="idemp_1",
        created_at=T,
        expires_at=T,
        updated_at=T,
    )
    attr_model = CounterfactualAttributionModel(
        attribution_id=uuid.uuid4(),
        command_id=cmd_id,
        payment_attempt_id="pay_1",
        source_system="synthetic",  # Mismatch: synthetic vs razorpay
        decision_id=cmd_model.decision_id,
        attribution_version=1,
        attributed_at=T,
        as_of_timestamp=T,
        attribution_status="ATTRIBUTED",
        methodology_name="synth_diff_in_diff",
        methodology_version="1.0.0",
        treatment_status="TREATED",
        observed_payment_outcome="CAPTURED",
        payment_amount_minor_units=50000,
        counterfactual_failure_probability=0.85,
        counterfactual_loss_exposure_minor_units=42500,
        counterfactual_natural_success_gmv_minor_units=7500,
        attributed_protected_gmv_minor_units=25500,
        attribution_confidence=0.6,
        is_synthetic_baseline=True,
        is_simulated_execution=True,
        attribution_audit_payload={},
        created_at=T,
    )

    session = AsyncMock()
    repo = AttributionRepository(session)
    repo.get_command = AsyncMock(return_value=cmd_model)

    res_mock = MagicMock()
    res_mock.scalar_one_or_none.return_value = attr_model
    session.execute.return_value = res_mock

    with pytest.raises(AttemptProviderConflictError, match="conflicts with command"):
        await repo.get_latest_attribution(cmd_id)


# ============================================================================
# 5. AttributionService cross-provider boundary validations
# ============================================================================

@pytest.mark.asyncio
async def test_attribution_service_rejects_cross_provider_execution_attempt():
    cmd_id = uuid.uuid4()
    dec_id = uuid.uuid4()
    command = MagicMock(
        command_id=cmd_id,
        decision_id=dec_id,
        payment_attempt_id="pay_1",
        source_system="razorpay",
        route_key="RETRY_SECONDARY_GATEWAY",
        command_status="SUCCEEDED",
        created_at=T,
    )
    decision = MagicMock(
        decision_id=dec_id,
        payment_attempt_id="pay_1",
        source_system="razorpay",
        decision_type="ACT",
        stage5_prediction_id=uuid.uuid4(),
        diagnosis_confidence="STRONG",
        decided_at=T,
    )
    prediction = MagicMock(
        prediction_id=decision.stage5_prediction_id,
        source_system="razorpay",
        model_name="m",
        model_version="1",
        failure_probability=0.85,
        prediction_status="PREDICTED",
    )
    mismatched_attempt = MagicMock(
        attempt_id=uuid.uuid4(),
        source_system="synthetic",  # Mismatched provider
        execution_status="SUCCESS",
        is_simulation=True,
        started_at=T,
        completed_at=T,
    )

    repo = AsyncMock()
    repo.get_attribution_by_as_of.return_value = None
    repo.get_latest_attribution.return_value = None
    repo.get_command.return_value = command
    repo.get_decision.return_value = decision
    repo.get_prediction.return_value = prediction
    repo.get_latest_execution_attempt.return_value = mismatched_attempt
    repo.get_latest_outcome_observation.return_value = None

    service = CounterfactualAttributionService(repo, attempt_repository=AsyncMock())
    with pytest.raises(AttemptProviderConflictError, match="execution attempt"):
        await service.attribute_payment_intervention(cmd_id, as_of_timestamp=T)


@pytest.mark.asyncio
async def test_attribution_service_rejects_cross_provider_outcome_observation():
    cmd_id = uuid.uuid4()
    dec_id = uuid.uuid4()
    command = MagicMock(
        command_id=cmd_id,
        decision_id=dec_id,
        payment_attempt_id="pay_1",
        source_system="razorpay",
        route_key="RETRY_SECONDARY_GATEWAY",
        command_status="SUCCEEDED",
        created_at=T,
    )
    decision = MagicMock(
        decision_id=dec_id,
        payment_attempt_id="pay_1",
        source_system="razorpay",
        decision_type="ACT",
        stage5_prediction_id=uuid.uuid4(),
        diagnosis_confidence="STRONG",
        decided_at=T,
    )
    prediction = MagicMock(
        prediction_id=decision.stage5_prediction_id,
        source_system="razorpay",
        model_name="m",
        model_version="1",
        failure_probability=0.85,
        prediction_status="PREDICTED",
    )
    mismatched_obs = MagicMock(
        observation_id=uuid.uuid4(),
        payment_attempt_id="pay_1",
        source_system="paypal",  # Mismatched provider
        terminal_event_id=uuid.uuid4(),
        terminal_event_type="payment.captured",
        terminal_event_timestamp=T,
        payment_outcome="CAPTURED",
        observation_audit_payload={},
    )

    repo = AsyncMock()
    repo.get_attribution_by_as_of.return_value = None
    repo.get_latest_attribution.return_value = None
    repo.get_command.return_value = command
    repo.get_decision.return_value = decision
    repo.get_prediction.return_value = prediction
    repo.get_latest_execution_attempt.return_value = None
    repo.get_latest_outcome_observation.return_value = mismatched_obs

    service = CounterfactualAttributionService(repo, attempt_repository=AsyncMock())
    with pytest.raises(AttemptProviderConflictError, match="outcome observation"):
        await service.attribute_payment_intervention(cmd_id, as_of_timestamp=T)


@pytest.mark.asyncio
async def test_attribution_service_rejects_decision_with_differing_payment_attempt_id():
    cmd_id = uuid.uuid4()
    dec_id = uuid.uuid4()
    command = MagicMock(
        command_id=cmd_id,
        decision_id=dec_id,
        payment_attempt_id="pay_1",
        source_system="razorpay",
        route_key="RETRY_SECONDARY_GATEWAY",
        command_status="SUCCEEDED",
        created_at=T,
    )
    decision = MagicMock(
        decision_id=dec_id,
        payment_attempt_id="pay_DIFFERENT",  # Differing attempt id
        source_system="razorpay",
        decision_type="ACT",
        stage5_prediction_id=uuid.uuid4(),
        diagnosis_confidence="STRONG",
        decided_at=T,
    )

    repo = AsyncMock()
    repo.get_attribution_by_as_of.return_value = None
    repo.get_latest_attribution.return_value = None
    repo.get_command.return_value = command
    repo.get_decision.return_value = decision
    repo.get_prediction.return_value = None
    repo.get_latest_execution_attempt.return_value = None
    repo.get_latest_outcome_observation.return_value = None

    service = CounterfactualAttributionService(repo, attempt_repository=AsyncMock())
    with pytest.raises(AttemptProviderConflictError, match="payment_attempt_id"):
        await service.attribute_payment_intervention(cmd_id, as_of_timestamp=T)
