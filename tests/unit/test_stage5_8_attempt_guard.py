"""
Every Stage 5-8 write requires the attempt to be registered under its provider, and refuses before any
repository write otherwise. Offline: repositories are mocked; the real services and live pipeline
routing run.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.attribution.attribution_service import CounterfactualAttributionService
from src.core.domain.execution_models import CommandStatus, InterventionCommand
from src.core.domain.intervention_models import DecisionType, GateVerdict, InterventionRouteKey
from src.core.domain.models import PaymentEvent
from src.core.domain.payment_attempt_exceptions import (
    AttemptNotRegisteredError,
    AttemptProviderConflictError,
    ProviderIdentityMissingError,
)
from src.core.domain.payment_attempts import AttemptRef
from src.core.execution.execution_service import InterventionExecutionService
from src.core.execution.observation_service import PaymentOutcomeObservationService
from src.core.intervention.policy import get_default_policy
from src.core.intervention.service import InterventionDecisionService
from src.core.services import live_pipeline
from src.core.services.failure_prediction_service import FailurePredictionService

T = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
KEY = "pay_Guard00000001"
REF = AttemptRef("razorpay", KEY, KEY)


def _unregistered():
    attempts = AsyncMock()
    attempts.require.side_effect = AttemptNotRegisteredError(f"payment attempt {KEY!r} is not registered")
    return attempts


def _command(source_system="razorpay"):
    return InterventionCommand(
        command_id=uuid.uuid4(), decision_id=uuid.uuid4(), payment_attempt_id=KEY, source_system=source_system,
        decision_version=1, route_key=InterventionRouteKey.DYNAMIC_RETRY_BACKOFF, policy_id="p", policy_version="1",
        command_status=CommandStatus.PENDING, status_reason=None, idempotency_key="cmd_x", created_at=T,
        expires_at=T + timedelta(minutes=5), updated_at=T,
    )


async def test_prediction_is_refused_before_any_write():
    repository = AsyncMock()
    service = FailurePredictionService(AsyncMock(), AsyncMock(), repository, MagicMock(), attempt_repository=_unregistered())
    with pytest.raises(AttemptNotRegisteredError):
        await service.orchestrate_prediction(REF, T)
    repository.acquire_version_allocation_lock.assert_not_awaited()
    repository.append_prediction.assert_not_awaited()


async def test_decision_is_refused_before_any_write():
    intervention_repo = AsyncMock()
    service = InterventionDecisionService(
        AsyncMock(), intervention_repo, AsyncMock(), get_default_policy(), attempt_repository=_unregistered()
    )
    with pytest.raises(AttemptNotRegisteredError):
        await service.decide(REF, T)
    intervention_repo.acquire_version_allocation_lock.assert_not_awaited()
    intervention_repo.acquire_global_rate_limit_lock.assert_not_awaited()
    intervention_repo.append_decision.assert_not_awaited()


async def test_command_creation_is_refused_before_any_write():
    execution_repo = AsyncMock()
    decision = SimpleNamespace(
        decision_id=uuid.uuid4(), payment_attempt_id=KEY, source_system="razorpay", decision_version=1,
        decision_type=DecisionType.ACT, selected_route_id=InterventionRouteKey.DYNAMIC_RETRY_BACKOFF,
        gate_verdict=GateVerdict.PASSED, policy_id="p", policy_version="1",
    )
    service = InterventionExecutionService(AsyncMock(), execution_repo, AsyncMock(), attempt_repository=_unregistered())
    with pytest.raises(AttemptNotRegisteredError):
        await service.create_command(decision, t_create=T)
    execution_repo.insert_command.assert_not_awaited()


async def test_execution_is_refused_before_any_write_or_dispatch():
    execution_repo = AsyncMock()
    execution_repo.get_command_by_id.return_value = _command()
    executor = AsyncMock()
    service = InterventionExecutionService(AsyncMock(), execution_repo, executor, attempt_repository=_unregistered())
    with pytest.raises(AttemptNotRegisteredError):
        await service.execute_command(uuid.uuid4(), t_claim=T)
    execution_repo.update_command_status_atomic.assert_not_awaited()
    execution_repo.insert_execution_attempt.assert_not_awaited()
    executor.execute.assert_not_awaited()


async def test_observation_is_refused_before_any_write():
    execution_repo = AsyncMock()
    execution_repo.get_command_by_id.return_value = _command()
    service = PaymentOutcomeObservationService(AsyncMock(), execution_repo, attempt_repository=_unregistered())
    with pytest.raises(AttemptNotRegisteredError):
        await service.observe(uuid.uuid4(), as_of_timestamp=T)
    execution_repo.insert_payment_outcome_observation.assert_not_awaited()


def _attribution_repo(command_source="razorpay", decision_source="razorpay", prediction_source="razorpay"):
    command = MagicMock(
        command_id=uuid.uuid4(), decision_id=uuid.uuid4(), payment_attempt_id=KEY, source_system=command_source,
        route_key="DYNAMIC_RETRY_BACKOFF", command_status="SUCCEEDED", created_at=T,
    )
    decision = MagicMock(
        decision_id=command.decision_id, source_system=decision_source, decision_type="ACT",
        stage5_prediction_id=uuid.uuid4(), diagnosis_confidence=None, decided_at=T,
    )
    prediction = MagicMock(
        prediction_id=decision.stage5_prediction_id, source_system=prediction_source, model_name="m",
        model_version="1", failure_probability=0.9, prediction_status="PREDICTED",
    )
    repo = AsyncMock()
    repo.get_attribution_by_as_of.return_value = None
    repo.get_latest_attribution.return_value = None
    repo.get_command.return_value = command
    repo.get_latest_execution_attempt.return_value = None
    repo.get_latest_outcome_observation.return_value = None
    repo.get_decision.return_value = decision
    repo.get_prediction.return_value = prediction
    repo.get_canonical_payment_amount.return_value = 50000
    repo.append_attribution.side_effect = lambda attribution: attribution
    return command, repo


async def test_attribution_is_refused_before_any_write():
    command, repo = _attribution_repo()
    service = CounterfactualAttributionService(repo, attempt_repository=_unregistered())
    with pytest.raises(AttemptNotRegisteredError):
        await service.attribute_payment_intervention(command.command_id, as_of_timestamp=T)
    repo.append_attribution.assert_not_awaited()


async def test_attribution_records_the_commands_provider():
    command, repo = _attribution_repo()
    attempts = AsyncMock()
    attribution = await CounterfactualAttributionService(repo, attempt_repository=attempts).attribute_payment_intervention(
        command.command_id, as_of_timestamp=T
    )
    assert attribution.source_system == "razorpay"
    attempts.require.assert_awaited_once_with(REF)


@pytest.mark.parametrize("sources, record", [
    (("razorpay", "synthetic", "razorpay"), "intervention decision"),
    (("razorpay", "razorpay", "synthetic"), "failure prediction"),
])
async def test_attribution_refuses_upstream_records_of_another_provider(sources, record):
    command, repo = _attribution_repo(*sources)
    service = CounterfactualAttributionService(repo, attempt_repository=AsyncMock())
    with pytest.raises(AttemptProviderConflictError, match=record):
        await service.attribute_payment_intervention(command.command_id, as_of_timestamp=T)
    repo.append_attribution.assert_not_awaited()


@pytest.mark.parametrize("sources", [(None, "razorpay", "razorpay"), ("razorpay", None, "razorpay")])
async def test_attribution_refuses_upstream_records_without_a_provider(sources):
    command, repo = _attribution_repo(*sources)
    service = CounterfactualAttributionService(repo, attempt_repository=AsyncMock())
    with pytest.raises(ProviderIdentityMissingError):
        await service.attribute_payment_intervention(command.command_id, as_of_timestamp=T)
    repo.append_attribution.assert_not_awaited()


# --- live pipeline -------------------------------------------------------------------------------

def _stub_pipeline(monkeypatch, register=None):
    """Replaces the services run_live_pipeline constructs; its own event routing stays real."""
    prediction_service = AsyncMock()
    prediction_service.orchestrate_prediction.return_value = MagicMock(failure_probability=0.1)
    for name in ("PaymentHealthAnalyticsService", "DegradationService", "RCAService", "FeatureReconstructionService",
                 "InterventionDecisionService", "InterventionExecutionService", "PaymentOutcomeObservationService",
                 "CounterfactualAttributionService"):
        monkeypatch.setattr(live_pipeline, name, MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr(live_pipeline, "FailurePredictionService", MagicMock(return_value=prediction_service))
    monkeypatch.setattr(live_pipeline, "get_development_model", MagicMock())
    monkeypatch.setattr(live_pipeline, "get_default_policy", MagicMock())
    monkeypatch.setattr(live_pipeline, "SimulatorInterventionExecutor", MagicMock())
    attempts = AsyncMock()
    if register is not None:
        attempts.register.side_effect = register
    monkeypatch.setattr(live_pipeline, "PaymentAttemptRepository", MagicMock(return_value=attempts))

    session = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = []
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    return session, prediction_service, attempts


def _event(source_system, event_type):
    return PaymentEvent(
        event_id=uuid.uuid4(), source_system=source_system, source_event_id="evt_guard_1", payment_id=KEY,
        timestamp=T, event_type=event_type, currency="INR", amount_minor_units=50000,
        payment_status=event_type.split(".")[-1], ingested_at=T,
    )


async def test_a_provider_conflict_at_registration_skips_stages_5_to_8(monkeypatch, caplog):
    conflict = AttemptProviderConflictError(f"payment attempt {KEY!r} is registered as razorpay:{KEY}")
    session, prediction_service, attempts = _stub_pipeline(monkeypatch, register=conflict)
    caplog.set_level(logging.WARNING, logger="live_pipeline")

    outcome = await live_pipeline.run_live_pipeline(_event("synthetic", "payment.authorized"), session=session)

    assert (outcome["status"], outcome["payment_attempt_id"], outcome["prediction"]) == ("success", None, None)
    prediction_service.orchestrate_prediction.assert_not_awaited()
    assert "[LivePipelineAttemptRefused] source_event_id=evt_guard_1 source_system=synthetic" in caplog.text
    assert "is registered as razorpay" in caplog.text


@pytest.mark.parametrize("event_type", ["payment.authorized", "payment.captured"])
async def test_unsupported_providers_run_no_stage_5_to_8(monkeypatch, caplog, event_type):
    session, prediction_service, attempts = _stub_pipeline(monkeypatch)
    caplog.set_level(logging.WARNING, logger="live_pipeline")

    outcome = await live_pipeline.run_live_pipeline(_event("test_system", event_type), session=session)

    assert (outcome["status"], outcome["payment_attempt_id"]) == ("success", None)
    attempts.register.assert_not_awaited()
    prediction_service.orchestrate_prediction.assert_not_awaited()
    assert "source_system='test_system' cannot own a Stage 5-8 payment attempt; Stages 5-8 skipped" in caplog.text


async def test_terminal_events_never_register_an_attempt(monkeypatch):
    session, prediction_service, attempts = _stub_pipeline(monkeypatch)

    outcome = await live_pipeline.run_live_pipeline(_event("razorpay", "payment.captured"), session=session)

    assert (outcome["status"], outcome["payment_attempt_id"]) == ("success", KEY)
    attempts.register.assert_not_awaited()
