"""
Unit tests for PayPal pipeline attempts: identity, event roles, and provider-scoped Stage 5 features.
Events come from the real PayPal and Razorpay parsers/normalizers; no database is used.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.domain.exceptions import NormalizationError
from src.core.domain.payment_attempts import (
    PAYPAL_ATTEMPT_PREFIX,
    PipelineAttempt,
    PipelineRole,
    in_paypal_attempt_namespace,
    paypal_attempt_id,
    paypal_order_id,
    resolve_pipeline_attempt,
)
from src.core.intervention.policy import get_default_policy
from src.core.ml.model import DeterministicBaselineModel
from src.core.normalizers.paypal import normalize_paypal_event
from src.core.normalizers.razorpay import normalize_razorpay_event
from src.core.parsers.paypal import parse_paypal_event
from src.core.parsers.razorpay import parse_razorpay_event
from src.core.services.feature_reconstruction_service import FeatureReconstructionService
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from tests.unit import paypal_event_samples as samples

T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
PAYPAL_ATTEMPT_ID = f"{PAYPAL_ATTEMPT_PREFIX}{samples.ORDER_ID}"


def _paypal(builder):
    return normalize_paypal_event(parse_paypal_event(builder()))


def _razorpay(event, status, payment_id="pay_AttemptTest0001", source_system=None):
    entity = {
        "id": payment_id, "entity": "payment", "amount": 50000, "currency": "INR", "status": status,
        "order_id": "order_AttemptTest01", "method": "card", "created_at": int(T.timestamp()),
    }
    body = {
        "entity": "event", "account_id": "acc_AttemptTest01", "event": event, "contains": ["payment"],
        "payload": {"payment": {"entity": entity}}, "created_at": int(T.timestamp()),
    }
    normalized = normalize_razorpay_event(parse_razorpay_event(json.dumps(body).encode()), f"evt_{status}")
    return normalized if source_system is None else normalized.model_copy(update={"source_system": source_system})


# --- attempt identity ------------------------------------------------------------------------------

def test_paypal_attempt_ids_round_trip_and_never_equal_the_raw_id():
    attempt_id = paypal_attempt_id(samples.ORDER_ID)
    assert attempt_id == PAYPAL_ATTEMPT_ID != samples.ORDER_ID
    assert paypal_order_id(attempt_id) == samples.ORDER_ID


@pytest.mark.parametrize("attempt_id", ["pay_AttemptTest0001", samples.ORDER_ID, PAYPAL_ATTEMPT_PREFIX, "", None])
def test_non_paypal_attempt_ids_have_no_paypal_order(attempt_id):
    assert paypal_order_id(attempt_id) is None


# --- event roles -----------------------------------------------------------------------------------

def test_paypal_order_approval_starts_the_order_attempt():
    event = _paypal(samples.order_approved)
    assert event.payment_id == samples.ORDER_ID
    assert resolve_pipeline_attempt(event) == PipelineAttempt(PipelineRole.TRIGGER, PAYPAL_ATTEMPT_ID)


@pytest.mark.parametrize("builder", [samples.capture_completed, samples.capture_declined])
def test_paypal_capture_outcomes_resolve_their_order_attempt_by_order_id(builder):
    event = _paypal(builder)
    assert (event.payment_id, event.order_id) == (samples.CAPTURE_ID, samples.ORDER_ID)
    assert resolve_pipeline_attempt(event) == PipelineAttempt(PipelineRole.TERMINAL, PAYPAL_ATTEMPT_ID)


@pytest.mark.parametrize("builder", [samples.capture_pending, samples.capture_refunded, samples.order_completed])
def test_other_paypal_events_have_no_pipeline_role(builder):
    assert resolve_pipeline_attempt(_paypal(builder)) is None


def test_paypal_capture_without_a_related_order_cannot_be_correlated():
    event = _paypal(samples.capture_completed).model_copy(update={"order_id": None})
    assert resolve_pipeline_attempt(event) is None


def test_paypal_events_never_use_the_razorpay_trigger():
    synthetic = _paypal(samples.order_approved).model_copy(update={"event_type": "payment.authorized"})
    assert resolve_pipeline_attempt(synthetic) is None


@pytest.mark.parametrize("source_system", [None, "synthetic"], ids=["razorpay", "synthetic"])
@pytest.mark.parametrize("event, status", [
    ("payment.authorized", "authorized"), ("payment.captured", "captured"), ("payment.failed", "failed"),
])
def test_non_paypal_payment_id_resembling_a_paypal_attempt_id_gets_no_pipeline_role(event, status, source_system):
    # Webhook ingestion rejects such ids, but rows written directly (scripts, synthetic data) can still
    # carry one; it must neither start nor resolve the PayPal attempt it resembles.
    lookalike = _razorpay(event, status, source_system=source_system).model_copy(
        update={"payment_id": PAYPAL_ATTEMPT_ID}
    )
    assert in_paypal_attempt_namespace(lookalike)
    assert resolve_pipeline_attempt(lookalike) is None


@pytest.mark.parametrize("payment_id", [PAYPAL_ATTEMPT_ID, f"{PAYPAL_ATTEMPT_PREFIX}AnyOtherOrder"])
@pytest.mark.parametrize("event, status", [
    ("payment.authorized", "authorized"), ("payment.captured", "captured"), ("payment.failed", "failed"),
])
def test_razorpay_normalization_rejects_payment_ids_in_the_paypal_attempt_namespace(event, status, payment_id):
    with pytest.raises(NormalizationError, match="reserved PayPal attempt namespace"):
        _razorpay(event, status, payment_id=payment_id)


@pytest.mark.parametrize("payment_id", ["pay_AttemptTest0001", samples.ORDER_ID, PAYPAL_ATTEMPT_PREFIX, "paypal-order"])
def test_razorpay_normalization_accepts_payment_ids_outside_the_namespace(payment_id):
    assert _razorpay("payment.authorized", "authorized", payment_id=payment_id).payment_id == payment_id


@pytest.mark.parametrize("builder", list(samples.ALL_SUPPORTED.values()))
def test_paypal_events_are_never_refused_as_namespace_lookalikes(builder):
    assert not in_paypal_attempt_namespace(_paypal(builder))


@pytest.mark.parametrize("source_system", [None, "synthetic"], ids=["razorpay", "synthetic"])
def test_razorpay_and_synthetic_roles_are_unchanged(source_system):
    authorized = _razorpay("payment.authorized", "authorized", source_system=source_system)
    captured = _razorpay("payment.captured", "captured", source_system=source_system)
    failed = _razorpay("payment.failed", "failed", source_system=source_system)
    assert resolve_pipeline_attempt(authorized) == PipelineAttempt(PipelineRole.TRIGGER, "pay_AttemptTest0001")
    assert resolve_pipeline_attempt(captured) == PipelineAttempt(PipelineRole.TERMINAL, "pay_AttemptTest0001")
    assert resolve_pipeline_attempt(failed) == PipelineAttempt(PipelineRole.TERMINAL, "pay_AttemptTest0001")
    assert resolve_pipeline_attempt(authorized.model_copy(update={"event_type": "payment.refunded"})) is None


# --- provider-scoped Stage 5 features --------------------------------------------------------------

def _feature_repository(trigger_event):
    repository = AsyncMock()
    repository.get_payment_event_by_id.return_value = trigger_event
    repository.get_historical_failure_rate.return_value = (100, 10)
    repository.get_most_severe_active_episode.return_value = None
    return repository


async def test_paypal_attempt_features_count_only_paypal_history():
    approval = _paypal(samples.order_approved)
    repository = _feature_repository(approval)

    snapshot = await FeatureReconstructionService(repository).reconstruct_features(PAYPAL_ATTEMPT_ID, T)

    repository.get_payment_event_by_id.assert_awaited_once_with(PAYPAL_ATTEMPT_ID, T)
    calls = repository.get_historical_failure_rate.await_args_list
    # PayPal events carry no method, bank or wallet: global and currency rates, 30 m and 24 h each.
    assert len(calls) == 4
    assert all(call.kwargs == {"paypal": True} for call in calls)
    assert {call.args[1] for call in calls} == {"paypal", "USD"}
    assert (snapshot.currency, snapshot.amount_minor_units) == ("USD", 50000)
    assert (snapshot.global_30m_failure_rate, snapshot.currency_24h_failure_rate) == (0.1, 0.1)
    assert (snapshot.is_in_active_degradation, snapshot.degradation_severity) == (False, None)


async def test_razorpay_attempt_features_keep_their_original_queries():
    authorized = _razorpay("payment.authorized", "authorized")
    repository = _feature_repository(authorized)

    await FeatureReconstructionService(repository).reconstruct_features("pay_AttemptTest0001", T)

    calls = repository.get_historical_failure_rate.await_args_list
    assert len(calls) == 6  # global, payment_method and currency; no bank or wallet on this card payment
    assert all(len(call.args) == 4 and call.kwargs == {} for call in calls)


@pytest.mark.parametrize("rate", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_without_stage_3_4_context_the_heuristic_cannot_reach_the_act_threshold(rate):
    # Characterization (PayPal today): no PayPal episodes or RCA exist, so the heuristic is
    # 0.5 x 0.05 + 0.5 x rate, at most 0.525 even when every recent PayPal capture failed.
    features = {
        "global_30m_failure_rate": rate, "global_24h_failure_rate": rate,
        "is_in_active_degradation": False, "rca_candidate_matches_payment_segment": None,
    }
    probability = DeterministicBaselineModel().predict(features)
    assert probability == pytest.approx(0.025 + 0.5 * rate)
    assert probability < get_default_policy().act_risk_threshold


async def test_paypal_events_never_receive_razorpay_episode_context():
    session = AsyncMock()
    approval = SimpleNamespace(**_paypal(samples.order_approved).model_dump())

    assert await FeatureReconstructionRepository(session).get_most_severe_active_episode(approval, T) is None
    session.execute.assert_not_awaited()


async def test_razorpay_events_still_look_up_episode_context():
    session = AsyncMock()
    session.execute.return_value.scalar_one_or_none = lambda: None
    authorized = SimpleNamespace(**_razorpay("payment.authorized", "authorized").model_dump())

    assert await FeatureReconstructionRepository(session).get_most_severe_active_episode(authorized, T) is None
    session.execute.assert_awaited()
