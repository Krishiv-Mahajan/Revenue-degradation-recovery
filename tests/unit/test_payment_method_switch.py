"""
Customer-approved payment method switch (PROMPT_PAYMENT_METHOD_SWITCH), offline: the offer lifecycle rules,
Stage 6 eligibility, policy ranking against the circuit bypass, and the PayPal sandbox offer execution.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from src.core.domain.execution_models import ExecutionResultStatus
from src.core.domain.intervention_models import InterventionRouteKey
from src.core.domain.switch_offer_models import (
    COMPLETION_WINDOW,
    OFFER_TTL,
    OfferEventType,
    OfferSignal,
    OfferState,
    OfferStatus,
    evaluate_offer_signal,
    make_offer_id,
    offer_presentation,
)
from src.core.execution.simulator import SimulatorInterventionExecutor
from src.core.intervention.calculator import calculate_policy_utility
from src.core.intervention.gates import evaluate_route_eligibility
from src.core.intervention.policy import get_default_policy
from src.infrastructure.paypal_client import PayPalEnvironment
from src.infrastructure.paypal_recovery_executor import (
    EXECUTOR_NAME,
    SWITCH_OFFER_ACTION_TYPE,
    RecoveryRoutingExecutor,
)
from tests.unit.test_paypal_recovery_executor import FakePayPal, ORDER_ID, _command, _executor, _order

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
SWITCH = InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH
OFFERED = OfferState(status=OfferStatus.OFFERED, expires_at=T0 + OFFER_TTL, amount_minor_units=2000, currency="USD")


def _accepted(at=T0 + timedelta(minutes=5)):
    return evaluate_offer_signal(OFFERED, OfferSignal.APPROVAL, at).state


def _types(transition):
    return [step.event_type for step in transition.steps]


# --- lifecycle --------------------------------------------------------------------------------------

def test_approval_before_expiry_is_the_customer_acceptance():
    at = T0 + timedelta(minutes=5)
    transition = evaluate_offer_signal(OFFERED, OfferSignal.APPROVAL, at)
    assert transition.state.status is OfferStatus.ACCEPTED
    assert (transition.state.accepted_at, transition.state.completion_deadline) == (at, at + COMPLETION_WINDOW)
    assert _types(transition) == [OfferEventType.CUSTOMER_ACCEPTED]


def test_explicit_decline_is_recorded_separately():
    transition = evaluate_offer_signal(OFFERED, OfferSignal.DECLINE, T0 + timedelta(minutes=1))
    assert (transition.state.status, _types(transition)) == (OfferStatus.DECLINED, [OfferEventType.CUSTOMER_DECLINED])


def test_no_response_by_the_deadline_expires_the_offer():
    transition = evaluate_offer_signal(OFFERED, OfferSignal.CLOCK, T0 + OFFER_TTL)
    assert (transition.state.status, _types(transition)) == (OfferStatus.EXPIRED, [OfferEventType.OFFER_EXPIRED])
    assert evaluate_offer_signal(OFFERED, OfferSignal.CLOCK, T0 + OFFER_TTL - timedelta(seconds=1)).changed is False


@pytest.mark.parametrize("signal", [OfferSignal.APPROVAL, OfferSignal.DECLINE, OfferSignal.CAPTURE_COMPLETED])
def test_a_late_customer_action_cannot_revive_an_expired_offer(signal):
    transition = evaluate_offer_signal(OFFERED, signal, T0 + OFFER_TTL + timedelta(seconds=1), 2000, "USD")
    assert transition.state.status is OfferStatus.EXPIRED
    assert _types(transition) == [OfferEventType.OFFER_EXPIRED, OfferEventType.EVENT_IGNORED]


def test_an_accepted_offer_without_completion_by_the_deadline_is_abandoned():
    accepted = _accepted()
    transition = evaluate_offer_signal(accepted, OfferSignal.CLOCK, accepted.completion_deadline)
    assert (transition.state.status, _types(transition)) == (OfferStatus.ABANDONED, [OfferEventType.OFFER_ABANDONED])
    late = evaluate_offer_signal(accepted, OfferSignal.CAPTURE_COMPLETED, accepted.completion_deadline, 2000, "USD")
    assert late.state.status is OfferStatus.ABANDONED
    assert _types(late) == [OfferEventType.OFFER_ABANDONED, OfferEventType.EVENT_IGNORED]


def test_only_a_capture_of_the_offered_amount_completes_an_accepted_offer():
    accepted = _accepted()
    at = T0 + timedelta(minutes=6)
    completed = evaluate_offer_signal(accepted, OfferSignal.CAPTURE_COMPLETED, at, 2000, "USD")
    assert (completed.state.status, _types(completed)) == (OfferStatus.COMPLETED, [OfferEventType.PAYMENT_COMPLETED])
    for amount, currency in ((1999, "USD"), (2000, "EUR")):
        mismatch = evaluate_offer_signal(accepted, OfferSignal.CAPTURE_COMPLETED, at, amount, currency)
        assert (mismatch.state.status, _types(mismatch)) == (OfferStatus.ACCEPTED, [OfferEventType.EVENT_IGNORED])


def test_a_declined_capture_fails_the_payment_without_changing_the_customer_response():
    transition = evaluate_offer_signal(_accepted(), OfferSignal.CAPTURE_DECLINED, T0 + timedelta(minutes=6))
    assert (transition.state.status, _types(transition)) == (OfferStatus.PAYMENT_FAILED, [OfferEventType.PAYMENT_FAILED])
    assert transition.state.accepted_at is not None


@pytest.mark.parametrize("signal", [OfferSignal.CAPTURE_COMPLETED, OfferSignal.CAPTURE_DECLINED])
def test_a_payment_outcome_before_acceptance_is_held_not_applied(signal):
    transition = evaluate_offer_signal(OFFERED, signal, T0 + timedelta(minutes=1), 2000, "USD")
    assert (transition.state.status, _types(transition)) == (OfferStatus.OFFERED, [OfferEventType.PAYMENT_OUTCOME_HELD])
    assert transition.changed is False


@pytest.mark.parametrize("terminal", [
    OfferStatus.DECLINED, OfferStatus.EXPIRED, OfferStatus.ABANDONED, OfferStatus.COMPLETED, OfferStatus.PAYMENT_FAILED,
])
@pytest.mark.parametrize("signal", [
    OfferSignal.APPROVAL, OfferSignal.DECLINE, OfferSignal.CAPTURE_COMPLETED, OfferSignal.CAPTURE_DECLINED,
])
def test_terminal_offers_are_never_reused(terminal, signal):
    state = OfferState(
        status=terminal, expires_at=T0 + OFFER_TTL, amount_minor_units=2000, currency="USD",
        accepted_at=T0, completion_deadline=T0 + COMPLETION_WINDOW,
    )
    transition = evaluate_offer_signal(state, signal, T0 + timedelta(minutes=2), 2000, "USD")
    assert (transition.state.status, _types(transition), transition.changed) == (
        terminal, [OfferEventType.EVENT_IGNORED], False,
    )


@pytest.mark.parametrize("signal", [OfferSignal.APPROVAL, OfferSignal.DECLINE])
def test_after_acceptance_repeated_approvals_and_declines_change_nothing(signal):
    transition = evaluate_offer_signal(_accepted(), signal, T0 + timedelta(minutes=6))
    assert (transition.state.status, _types(transition)) == (OfferStatus.ACCEPTED, [OfferEventType.EVENT_IGNORED])


def test_offer_identity_is_one_per_command():
    import uuid
    command_id = uuid.uuid4()
    assert make_offer_id(command_id) == make_offer_id(command_id) != make_offer_id(uuid.uuid4())


def test_presentation_requires_explicit_customer_action_and_promises_no_silent_charge():
    presentation = offer_presentation(2000, "USD", "card")
    assert presentation["requires_customer_action"] is True
    assert "card" in presentation["message"]
    assert any("Nothing is charged unless you approve" in term for term in presentation["terms"])


# --- Stage 6 eligibility -----------------------------------------------------------------------------

def _eligibility(**overrides):
    kwargs = dict(
        route=get_default_policy().routes[SWITCH], rca_classification="SEGMENT_SPECIFIC",
        rca_evidence_strength="STRONG", rca_candidate_dimension="PAYMENT_METHOD", rca_candidate_value="card",
        rca_candidate_matches_payment_segment=True, payment_method="card", bank=None, is_on_cooldown=False,
        source_system="razorpay", currency="USD",
    )
    kwargs.update(overrides)
    return evaluate_route_eligibility(**kwargs)


def test_switch_is_eligible_for_a_diagnosed_payment_method_outage_on_another_provider():
    assert _eligibility() == (True, "Eligible")
    assert _eligibility(currency="EUR")[0] is True


@pytest.mark.parametrize("overrides, reason", [
    ({"source_system": "paypal"}, "already paying with paypal"),
    ({"source_system": None}, "already paying with paypal"),
    ({"currency": "XYZ"}, "payments in XYZ cannot be verified"),
    # PayPal accepts INR orders only for India in-country accounts, and PayPal INR webhooks are not ingested
    # here, so an INR offer's completion could never be verified.
    ({"currency": "INR"}, "payments in INR cannot be verified"),
    ({"rca_classification": "SYSTEMIC"}, "SYSTEMIC episode requires a GLOBAL route"),
    ({"rca_evidence_strength": "WEAK"}, "requires STRONG/MODERATE"),
    ({"rca_candidate_dimension": "BANK"}, "Diagnosed dimension BANK not supported"),
    ({"rca_candidate_matches_payment_segment": False}, "does not match diagnosed segment"),
    ({"is_on_cooldown": True}, "cooldown"),
    ({"rca_classification": "UNKNOWN"}, "UNKNOWN"),
])
def test_switch_is_ineligible_without_a_diagnosed_method_outage_and_a_real_alternative(overrides, reason):
    eligible, why = _eligibility(**overrides)
    assert eligible is False
    assert reason in why


def test_other_routes_ignore_the_provider_and_currency_rule():
    route = get_default_policy().routes[InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS]
    assert _eligibility(route=route, source_system="paypal", currency="XYZ") == (True, "Eligible")


def test_policy_1_2_0_changes_no_cost_rate_or_threshold():
    policy = get_default_policy()
    switch = policy.routes[SWITCH]
    assert (policy.policy_version, switch.alternative_payment_provider) == ("1.2.0", "paypal")
    assert (switch.policy_recovery_rate, switch.policy_cost_minor_units, switch.cooldown_seconds) == (Decimal("0.65"), 300, 600)
    assert [r.alternative_payment_provider for k, r in policy.routes.items() if k is not SWITCH] == [None] * 4
    assert (policy.act_risk_threshold, policy.monitor_risk_threshold) == (0.70, 0.35)


@pytest.mark.parametrize("amount, winner", [
    (50, SWITCH),  # USD: bypass 31 - 15 = 16 < switch 27 - 10 = 17
    (2000, InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS),  # bypass 1238 - 15 = 1223 > switch 1072 - 10 = 1062
])
def test_against_the_circuit_bypass_the_switch_wins_only_where_the_unchanged_policy_scores_it_higher(amount, winner):
    policy = get_default_policy()
    routes, costs = policy.routes, policy.cost_schedule("USD").route_costs_minor_units
    utilities = {
        key: calculate_policy_utility(amount, 0.825, routes[key].policy_recovery_rate, costs[key])[1]
        for key in (SWITCH, InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS)
    }
    assert max(utilities, key=utilities.get) is winner


# --- execution (PayPal is httpx.MockTransport; the real client and executor run) ------------------------

RAZORPAY_CONTEXT = {"amount_minor_units": 2000, "currency": "USD", "source_system": "razorpay"}


async def test_switch_offer_creates_a_sandbox_checkout_that_needs_customer_approval():
    fake = FakePayPal(httpx.Response(201, json=_order()))
    command = _command(SWITCH)

    result = await _executor(fake).execute(command, RAZORPAY_CONTEXT)

    assert (result.status, result.executor_name, result.is_simulation) == (ExecutionResultStatus.SUCCESS, EXECUTOR_NAME, False)
    assert result.provider_action_type == SWITCH_OFFER_ACTION_TYPE
    payload = result.provider_response_payload
    assert (payload["paypal_order_id"], payload["environment"], payload["amount"]) == (
        ORDER_ID, "sandbox", {"currency_code": "USD", "value": "20.00"},
    )
    assert payload["switch_offer"]["consent"].startswith("CUSTOMER_APPROVAL_REQUIRED")
    assert payload["switch_offer"]["capture"].startswith("NEVER_BY_THIS_ENGINE")
    [request] = fake.order_requests
    body = json.loads(request.content)
    assert (body["intent"], body["purchase_units"][0]["description"]) == ("CAPTURE", "Pay with PayPal instead")
    assert request.headers["PayPal-Request-Id"] == command.idempotency_key
    assert [r.url.path for r in fake.order_requests] == ["/v2/checkout/orders"]  # never a capture call


async def test_switch_offer_retries_reuse_the_same_paypal_request_id():
    fake = FakePayPal(httpx.Response(503), httpx.Response(201, json=_order()))
    command = _command(SWITCH)
    result = await _executor(fake).execute(command, RAZORPAY_CONTEXT)
    assert result.status is ExecutionResultStatus.SUCCESS
    assert {r.headers["PayPal-Request-Id"] for r in fake.order_requests} == {command.idempotency_key}
    assert len(fake.order_requests) == 2


@pytest.mark.parametrize("kwargs, context, code", [
    ({}, {**RAZORPAY_CONTEXT, "source_system": "paypal"}, "ALTERNATIVE_METHOD_UNAVAILABLE"),
    ({}, {"amount_minor_units": 2000, "currency": "USD"}, "ALTERNATIVE_METHOD_UNAVAILABLE"),
    ({"enabled": False}, RAZORPAY_CONTEXT, "KILL_SWITCH_ACTIVE"),
    ({"environment": PayPalEnvironment.LIVE}, RAZORPAY_CONTEXT, "LIVE_ENVIRONMENT_REFUSED"),
])
async def test_switch_offer_refusals_make_no_paypal_request(kwargs, context, code):
    fake = FakePayPal()
    result = await _executor(fake, **kwargs).execute(_command(SWITCH), context)
    assert (result.status, result.provider_response_code, result.is_simulation) == (
        ExecutionResultStatus.FAILURE, code, False,
    )
    assert (fake.order_requests, fake.token_requests) == ([], 0)


async def test_routing_sends_the_switch_to_paypal_and_keeps_gateway_retry_simulated():
    fake = FakePayPal(httpx.Response(201, json=_order()))
    router = RecoveryRoutingExecutor(_executor(fake), SimulatorInterventionExecutor())

    switch = await router.execute(_command(SWITCH), RAZORPAY_CONTEXT)
    retry = await router.execute(_command(InterventionRouteKey.RETRY_SECONDARY_GATEWAY), RAZORPAY_CONTEXT)

    assert (switch.executor_name, switch.is_simulation) == (EXECUTOR_NAME, False)
    assert (retry.executor_name, retry.is_simulation) == ("SIMULATOR_V1", True)
    assert len(fake.order_requests) == 1
