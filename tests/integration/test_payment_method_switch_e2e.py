"""
End-to-end tests of the customer-approved payment method switch (PROMPT_PAYMENT_METHOD_SWITCH).

Signed Razorpay and PayPal webhooks go through the real routes and the real live pipeline, against the
disposable test database. The degradation context is the live-context generator's card outage (real Stage 2-4
services on synthetic traffic, STRONG RCA on PAYMENT_METHOD=card), and recent Razorpay card failures give the
real heuristic its 30-minute failure rate. Only PayPal's HTTP API is replaced, in-process, by a MockTransport
that honours PayPal-Request-Id idempotency.

The default policy scores DEGRADATION_CIRCUIT_BYPASS above the switch for these payments, so the switch tests
use a merchant policy whose circuit bypass is disabled (every cost, rate and threshold unchanged); a test pins
that the default policy still chooses the bypass. The customer's acceptance is PayPal's approval of the offer's
checkout; the merchant's capture of it is represented by PayPal's capture webhook (this engine never captures).
"""
import dataclasses
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.core.domain.intervention_models import InterventionRouteKey
from src.core.domain.payment_attempts import paypal_attempt_id
from src.core.execution.switch_offer_service import SwitchOfferService
from src.core.intervention.policy import get_default_policy
from src.core.services import live_pipeline
from src.infrastructure.models import (
    CounterfactualAttributionModel,
    FailurePredictionModel,
    InterventionCommandModel,
    InterventionDecisionModel,
    InterventionExecutionAttemptModel,
    PaymentAttemptModel,
    PaymentEventModel,
    PaymentMethodSwitchOfferEventModel,
    PaymentMethodSwitchOfferModel,
    PaymentOutcomeObservationModel,
)
from tests.integration.test_live_pipeline_e2e import (  # noqa: F401  (e2e is a fixture)
    _assert_runs_succeeded,
    _domain_event,
    _for_payment,
    _one,
    _payment_id,
    _paypal_order_id,
    _post_paypal,
    _post_razorpay,
    _razorpay_webhook,
    _rows,
    _seed_degraded_card_context,
    e2e,
)
from tests.unit import paypal_event_samples as samples

AMOUNT = 2000  # USD 20.00
VALUE = "20.00"
SWITCH = InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH.value
BYPASS = InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS
AUTH = ("e2e-dashboard-user", "e2e-dashboard-password")


# --- setup ------------------------------------------------------------------------------------------

async def _seed_recent_razorpay_card_failures(Session, now, failed=36, captured=24):
    """Recent Razorpay card outcomes (60% failed): the real heuristic's 30-minute Razorpay failure rate."""
    at = now - timedelta(minutes=25)
    async with Session() as session:
        session.add_all([
            PaymentEventModel(
                event_id=uuid.uuid4(), source_system="razorpay", source_event_id=f"evt_switch_recent_{i:03d}",
                payment_id=f"pay_SwitchRecent{i:03d}", timestamp=at + timedelta(seconds=i),
                event_type="payment.failed" if i < failed else "payment.captured", currency="INR",
                amount_minor_units=50000, payment_status="failed" if i < failed else "captured",
                payment_method="card", ingested_at=at + timedelta(seconds=i),
            )
            for i in range(failed + captured)
        ])
        await session.commit()


async def _card_outage(e2e, now):
    await _seed_degraded_card_context(e2e.Session, now)
    await _seed_recent_razorpay_card_failures(e2e.Session, now)


def _merchant_policy_without_circuit_bypass(monkeypatch):
    """The default policy with DEGRADATION_CIRCUIT_BYPASS disabled; nothing else differs."""
    policy = get_default_policy()
    routes = {**policy.routes, BYPASS: dataclasses.replace(policy.routes[BYPASS], enabled=False)}
    merchant = dataclasses.replace(policy, policy_id="merchant-without-circuit-bypass", routes=routes)
    monkeypatch.setattr(live_pipeline, "get_default_policy", lambda: merchant)


def _install_paypal_sandbox(monkeypatch):
    """
    The real PayPal executor and client, with PayPal's Orders API served in-process. Each new
    PayPal-Request-Id gets a new order; a repeated one returns the same order, as PayPal does.
    Returns every Orders request.
    """
    from src.core.execution.simulator import SimulatorInterventionExecutor
    from src.infrastructure.paypal_client import PayPalClient, PayPalConfig
    from src.infrastructure.paypal_recovery_executor import PayPalRecoveryCheckoutExecutor, RecoveryRoutingExecutor

    requests, orders = [], {}

    def paypal_api(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "sandbox-token", "token_type": "Bearer", "expires_in": 3600})
        requests.append(request)
        request_id = request.headers["PayPal-Request-Id"]
        replay = request_id in orders
        order_id = orders.setdefault(request_id, _paypal_order_id())
        return httpx.Response(200 if replay else 201, json={
            "id": order_id, "status": "PAYER_ACTION_REQUIRED",
            "links": [{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"}],
        })

    client = PayPalClient(
        PayPalConfig(client_id="sandbox-client", client_secret="sandbox-secret"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(paypal_api)),
    )
    executor = RecoveryRoutingExecutor(
        PayPalRecoveryCheckoutExecutor(client, enabled=lambda: True), SimulatorInterventionExecutor()
    )
    monkeypatch.setattr(live_pipeline, "build_intervention_executor", lambda simulator: executor)
    return requests


def _dashboard(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", AUTH[0])
    monkeypatch.setenv("DASHBOARD_PASSWORD", AUTH[1])


# --- webhooks ---------------------------------------------------------------------------------------

async def _razorpay(e2e, event, payment_id, at):
    source_event_id = f"evt_{event.split('.')[1]}_{payment_id}"
    body = _razorpay_webhook(event, payment_id, int(at.timestamp()), amount=AMOUNT, method="card", currency="USD")
    response = await _post_razorpay(e2e, body, source_event_id)
    assert response.status_code == 200, response.text
    return source_event_id


async def _paypal_approval(e2e, order_id, at, webhook_id=None):
    body = samples.order_approved(value=VALUE, currency="USD")
    body["id"] = webhook_id or f"WH-SWITCH-APPROVED-{order_id}"
    body["create_time"] = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    body["resource"]["id"] = order_id
    response = await _post_paypal(e2e, json.dumps(body).encode())
    assert response.status_code == 200, response.text
    return body["id"], response.json()["status"]


async def _paypal_capture(e2e, order_id, at, *, declined=False, value=VALUE, webhook_id=None):
    body = samples.capture_declined() if declined else samples.capture_completed()
    body["id"] = webhook_id or f"WH-SWITCH-CAPTURE-{order_id}-{value.replace('.', '_')}"
    body["create_time"] = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    body["resource"]["id"] = f"SWCAP{uuid.uuid4().hex[:12].upper()}"
    body["resource"]["amount"] = {"value": value, "currency_code": "USD"}
    body["resource"]["supplementary_data"]["related_ids"]["order_id"] = order_id
    response = await _post_paypal(e2e, json.dumps(body).encode())
    assert (response.status_code, response.json()["status"]) == (200, "stored"), response.text
    return body["id"]


async def _offer_for(e2e, payment_id):
    return await _one(e2e.Session, PaymentMethodSwitchOfferModel, PaymentMethodSwitchOfferModel.payment_attempt_id == payment_id)


async def _offer_events(e2e, offer):
    rows = await _rows(e2e.Session, PaymentMethodSwitchOfferEventModel, PaymentMethodSwitchOfferEventModel.offer_id == offer.offer_id)
    return [(e.event_type, e.to_status) for e in sorted(rows, key=lambda e: e.sequence)]


async def _attributions(e2e, command_id):
    rows = await _rows(e2e.Session, CounterfactualAttributionModel, CounterfactualAttributionModel.command_id == command_id)
    return sorted(rows, key=lambda a: a.attribution_version)


async def _offered_switch(e2e, monkeypatch, now):
    """A card outage, then a USD card authorization that is offered a PayPal switch."""
    await _card_outage(e2e, now)
    _merchant_policy_without_circuit_bypass(monkeypatch)
    requests = _install_paypal_sandbox(monkeypatch)
    payment_id = _payment_id()
    auth_id = await _razorpay(e2e, "payment.authorized", payment_id, now)
    offer = await _offer_for(e2e, payment_id)
    return payment_id, auth_id, offer, requests


# --- the full flow ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_outage_diagnosis_offer_consent_checkout_verified_payment_and_attribution(e2e, monkeypatch):
    _dashboard(monkeypatch)
    now = datetime.now(timezone.utc)
    payment_id, auth_id, offer, requests = await _offered_switch(e2e, monkeypatch, now)

    # Diagnosis -> decision: the real heuristic and the switch, eligible on the STRONG card diagnosis.
    prediction = await _for_payment(e2e.Session, FailurePredictionModel, payment_id)
    assert (prediction.model_name, prediction.failure_probability) == ("DeterministicBaselineModel", pytest.approx(0.825))
    decision = await _for_payment(e2e.Session, InterventionDecisionModel, payment_id)
    assert (decision.decision_type, decision.gate_verdict, decision.selected_route_id) == ("ACT", "PASSED", SWITCH)
    context = decision.evaluation_audit_payload["upstream_context"]
    assert (context["rca_candidate_dimension"], context["rca_candidate_value"], context["rca_evidence_strength"]) == (
        "PAYMENT_METHOD", "card", "STRONG",
    )

    # Offer: a real sandbox checkout for the decided amount, awaiting the customer. One PayPal call, no capture.
    command = await _for_payment(e2e.Session, InterventionCommandModel, payment_id)
    execution = await _one(e2e.Session, InterventionExecutionAttemptModel, InterventionExecutionAttemptModel.command_id == command.command_id)
    assert (execution.executor_name, execution.is_simulation, execution.provider_action_type) == (
        "PAYPAL_ORDERS_V2_SANDBOX", False, "PAYPAL_PAYMENT_METHOD_SWITCH_OFFER",
    )
    assert [(r.method, r.url.path) for r in requests] == [("POST", "/v2/checkout/orders")]
    order_id = offer.checkout_order_id
    assert (offer.status, offer.source_system, offer.alternative_provider, offer.environment) == ("OFFERED", "razorpay", "paypal", "sandbox")
    assert (offer.amount_minor_units, offer.currency, offer.command_id, offer.decision_id) == (AMOUNT, "USD", command.command_id, decision.decision_id)
    assert (offer.diagnosis_dimension, offer.diagnosis_value, str(offer.episode_id)) == ("PAYMENT_METHOD", "card", context["episode_id"])
    assert offer.presentation["requires_customer_action"] is True
    view = (await e2e.client.get(f"/api/v1/offers/{offer.offer_id}", auth=AUTH)).json()
    assert view["checkout"] == {"order_id": order_id, "approve_url": offer.approve_url, "actionable": True}

    # The card payment fails. Nothing is attributed: the customer has not taken up the offer.
    failed_id = await _razorpay(e2e, "payment.failed", payment_id, now + timedelta(seconds=30))
    [first] = await _attributions(e2e, command.command_id)
    assert (first.attribution_status, first.attributed_protected_gmv_minor_units) == ("UNATTRIBUTED_UNTREATED", 0)
    assert first.attribution_audit_payload["switch_offer"]["treatment_reason"] == "CUSTOMER_HAS_NOT_ACCEPTED_THE_OFFER"

    # Consent: the customer approves the checkout at PayPal. Still nothing is attributed.
    approval_id, _ = await _paypal_approval(e2e, order_id, now + timedelta(seconds=60))
    assert (await _offer_for(e2e, payment_id)).status == "ACCEPTED"
    assert len(await _attributions(e2e, command.command_id)) == 1

    # Verified payment: the merchant captures; PayPal's capture webhook completes the offer.
    capture_id = await _paypal_capture(e2e, order_id, now + timedelta(seconds=90))
    _assert_runs_succeeded(e2e.caplog, [auth_id, failed_id, approval_id, capture_id])
    completed = await _offer_for(e2e, payment_id)
    capture_event = await _one(e2e.Session, PaymentEventModel, PaymentEventModel.source_event_id == capture_id)
    assert (completed.status, completed.completion_event_id) == ("COMPLETED", capture_event.event_id)
    assert await _offer_events(e2e, completed) == [
        ("OFFER_CREATED", "OFFERED"), ("CUSTOMER_ACCEPTED", "ACCEPTED"), ("PAYMENT_COMPLETED", "COMPLETED"),
    ]

    # Attribution only now, to the original Razorpay attempt, as real (not simulated) execution.
    observation = sorted(
        await _rows(e2e.Session, PaymentOutcomeObservationModel, PaymentOutcomeObservationModel.command_id == command.command_id),
        key=lambda o: o.observation_version,
    )[-1]
    assert (observation.payment_outcome, observation.terminal_event_id, observation.source_system) == (
        "CAPTURED", capture_event.event_id, "razorpay",
    )
    attributions = await _attributions(e2e, command.command_id)
    assert [(a.attribution_status, a.source_system, a.is_simulated_execution) for a in attributions] == [
        ("UNATTRIBUTED_UNTREATED", "razorpay", False), ("ATTRIBUTED", "razorpay", False),
    ]
    attributed = attributions[-1]
    assert attributed.payment_amount_minor_units == AMOUNT
    assert attributed.attribution_audit_payload["switch_offer"]["treatment_reason"] == "PAID_THROUGH_THE_ACCEPTED_OFFER"
    # p0 0.825; confidence 0.6 (synthetic model) x 1.0 (STRONG diagnosis) x 0.85 (paid 60-300 s after the offer).
    assert (attributed.counterfactual_loss_exposure_minor_units, str(attributed.attribution_confidence)) == (1650, "0.5100")
    assert attributed.attributed_protected_gmv_minor_units == 842

    # The recovery order is never a payment attempt of its own; PayPal was called once and never to capture.
    assert {a.payment_attempt_id for a in await _rows(e2e.Session, PaymentAttemptModel)} == {payment_id}
    assert await _rows(e2e.Session, FailurePredictionModel, FailurePredictionModel.payment_attempt_id == paypal_attempt_id(order_id)) == []
    assert len(requests) == 1

    # The audit trail links attempt, diagnosis, decision, offer, response, checkout and outcome; the link is withdrawn.
    view = (await e2e.client.get(f"/api/v1/offers/{offer.offer_id}", auth=AUTH)).json()
    assert view["status"] == "COMPLETED"
    assert view["checkout"] == {"order_id": order_id, "approve_url": None, "actionable": False}
    assert view["presentation"] is None
    assert (view["original_attempt"]["payment_attempt_id"], view["diagnosis"]["value"]) == (payment_id, "card")
    assert (view["decision"]["selected_route_id"], view["execution"]["is_simulation"]) == (SWITCH, False)
    assert view["observation"] == {
        "payment_outcome": "CAPTURED", "terminal_event_id": str(capture_event.event_id), "verified_switch_completion": True,
    }
    assert view["attribution"]["status"] == "ATTRIBUTED"
    assert [e["event_type"] for e in view["events"]] == ["OFFER_CREATED", "CUSTOMER_ACCEPTED", "PAYMENT_COMPLETED"]
    listing = (await e2e.client.get("/api/v1/offers", auth=AUTH)).json()
    assert listing["counts_by_status"]["COMPLETED"] == 1
    timeline = (await e2e.client.get(f"/api/v1/timeline/{payment_id}", auth=AUTH)).json()
    assert timeline["execution"]["switch_offer"]["status"] == "COMPLETED"
    assert timeline["execution"]["recovery_checkout"]["approve_url"] is None

    # The overview separates verified recovered money from the estimate, and reports provider health.
    summary = (await e2e.client.get("/api/v1/summary", auth=AUTH)).json()
    assert summary["verified_recovered_gmv_by_currency"] == [{"currency": "USD", "amount_minor_units": AMOUNT, "payment_count": 1}]
    assert [(g["currency"], g["amount_minor_units"]) for g in summary["total_protected_gmv_by_currency"]] == [("USD", 842)]
    assert summary["switch_offers_by_status"]["COMPLETED"] == 1
    health = {h["provider_scope"]: h for h in summary["provider_health"]}
    assert (health["razorpay_pipeline"]["status"], health["razorpay_pipeline"]["worst_severity"]) == ("DEGRADED", "CRITICAL")
    assert health["paypal"]["status"] == "HEALTHY"
    paypal_only = (await e2e.client.get("/api/v1/summary?provider=paypal", auth=AUTH)).json()
    assert (paypal_only["verified_recovered_gmv_by_currency"], paypal_only["switch_offers_by_status"]["COMPLETED"]) == ([], 0)
    [row] = [r for r in (await e2e.client.get("/api/v1/recovery", auth=AUTH)).json()["ledger"] if r["payment_attempt_id"] == payment_id]
    assert row["switch_offer"] == {"offer_id": str(offer.offer_id), "status": "COMPLETED"}


# --- unchanged behaviour ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_default_policy_still_chooses_the_circuit_bypass_and_makes_no_offer(e2e, monkeypatch):
    now = datetime.now(timezone.utc)
    await _card_outage(e2e, now)
    requests = _install_paypal_sandbox(monkeypatch)
    payment_id = _payment_id()
    auth_id = await _razorpay(e2e, "payment.authorized", payment_id, now)

    _assert_runs_succeeded(e2e.caplog, [auth_id])
    decision = await _for_payment(e2e.Session, InterventionDecisionModel, payment_id)
    assert (decision.decision_type, decision.selected_route_id, decision.policy_version) == ("ACT", BYPASS.value, "1.2.0")
    candidates = {c["route_key"]: c for c in decision.evaluation_audit_payload["candidates"]}
    assert candidates[SWITCH]["is_eligible"] is True
    assert candidates[BYPASS.value]["policy_utility_minor_units"] > candidates[SWITCH]["policy_utility_minor_units"]
    execution = await _one(e2e.Session, InterventionExecutionAttemptModel)
    assert (execution.executor_name, execution.is_simulation) == ("SIMULATOR_V1", True)
    assert (requests, await _rows(e2e.Session, PaymentMethodSwitchOfferModel)) == ([], [])


@pytest.mark.asyncio
async def test_a_simulated_switch_execution_is_labelled_and_never_becomes_an_offer(e2e, monkeypatch):
    now = datetime.now(timezone.utc)
    await _card_outage(e2e, now)
    _merchant_policy_without_circuit_bypass(monkeypatch)
    payment_id = _payment_id()
    await _razorpay(e2e, "payment.authorized", payment_id, now)

    decision = await _for_payment(e2e.Session, InterventionDecisionModel, payment_id)
    assert decision.selected_route_id == SWITCH
    execution = await _one(e2e.Session, InterventionExecutionAttemptModel)
    assert (execution.executor_name, execution.is_simulation, execution.provider_response_code) == (
        "SIMULATOR_V1", True, "SIMULATED_ACK",
    )
    assert await _rows(e2e.Session, PaymentMethodSwitchOfferModel) == []


# --- customer responses ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_declined_offer_is_never_reused_or_attributed(e2e, monkeypatch):
    _dashboard(monkeypatch)
    now = datetime.now(timezone.utc)
    payment_id, _, offer, _ = await _offered_switch(e2e, monkeypatch, now)

    # A cross-site form cannot submit a decline: only a JSON body is accepted.
    for content_type in ("text/plain", "application/x-www-form-urlencoded"):
        forged = await e2e.client.post(
            f"/api/v1/offers/{offer.offer_id}/decline", content=b'{"reason": "x"}',
            headers={"Content-Type": content_type}, auth=AUTH,
        )
        assert forged.status_code == 422
    assert (await _offer_for(e2e, payment_id)).status == "OFFERED"

    first = await e2e.client.post(f"/api/v1/offers/{offer.offer_id}/decline", json={"reason": "prefers card"}, auth=AUTH)
    again = await e2e.client.post(f"/api/v1/offers/{offer.offer_id}/decline", json={}, auth=AUTH)
    assert (first.status_code, first.json()["status"], first.json()["applied"]) == (200, "DECLINED", True)
    assert (again.status_code, again.json()["applied"]) == (200, False)

    # A later approval and capture of the same checkout are recorded and ignored.
    await _paypal_approval(e2e, offer.checkout_order_id, now + timedelta(seconds=60))
    await _paypal_capture(e2e, offer.checkout_order_id, now + timedelta(seconds=90))
    declined = await _offer_for(e2e, payment_id)
    assert declined.status == "DECLINED" and declined.completion_event_id is None
    assert await _offer_events(e2e, declined) == [
        ("OFFER_CREATED", "OFFERED"), ("CUSTOMER_DECLINED", "DECLINED"),
        ("EVENT_IGNORED", "DECLINED"), ("EVENT_IGNORED", "DECLINED"),
    ]
    command = await _for_payment(e2e.Session, InterventionCommandModel, payment_id)
    assert all(a.attribution_status != "ATTRIBUTED" for a in await _attributions(e2e, command.command_id))
    view = (await e2e.client.get(f"/api/v1/offers/{offer.offer_id}", auth=AUTH)).json()
    assert view["checkout"]["approve_url"] is None
    assert (await e2e.client.post(f"/api/v1/offers/{uuid.uuid4()}/decline", json={}, auth=AUTH)).status_code == 404
    assert (await e2e.client.post(f"/api/v1/offers/{offer.offer_id}/decline", json={})).status_code == 401


@pytest.mark.asyncio
async def test_a_decline_after_acceptance_is_refused(e2e, monkeypatch):
    _dashboard(monkeypatch)
    now = datetime.now(timezone.utc)
    payment_id, _, offer, _ = await _offered_switch(e2e, monkeypatch, now)
    await _paypal_approval(e2e, offer.checkout_order_id, now + timedelta(seconds=60))

    response = await e2e.client.post(f"/api/v1/offers/{offer.offer_id}/decline", json={}, auth=AUTH)

    assert response.status_code == 409
    accepted = await _offer_for(e2e, payment_id)
    assert accepted.status == "ACCEPTED"
    assert (await _offer_events(e2e, accepted))[-1] == ("EVENT_IGNORED", "ACCEPTED")


@pytest.mark.asyncio
async def test_an_unanswered_offer_expires_and_a_late_approval_cannot_revive_it(e2e, monkeypatch):
    now = datetime.now(timezone.utc)
    payment_id, _, offer, _ = await _offered_switch(e2e, monkeypatch, now)

    async with e2e.Session() as session:
        assert await SwitchOfferService(session).expire_due(now + timedelta(minutes=29)) == []
        [expired] = await SwitchOfferService(session).expire_due(now + timedelta(minutes=31))
        await session.commit()
    assert expired.status == "EXPIRED"

    await _paypal_approval(e2e, offer.checkout_order_id, now + timedelta(minutes=32))
    await _paypal_capture(e2e, offer.checkout_order_id, now + timedelta(minutes=33))
    final = await _offer_for(e2e, payment_id)
    assert (final.status, final.accepted_at, final.completion_event_id) == ("EXPIRED", None, None)
    assert final.resolved_at == final.expires_at
    assert [t for t, _ in await _offer_events(e2e, final)] == [
        "OFFER_CREATED", "OFFER_EXPIRED", "EVENT_IGNORED", "EVENT_IGNORED",
    ]
    command = await _for_payment(e2e.Session, InterventionCommandModel, payment_id)
    assert all(a.attribution_status != "ATTRIBUTED" for a in await _attributions(e2e, command.command_id))
    observations = await _rows(e2e.Session, PaymentOutcomeObservationModel, PaymentOutcomeObservationModel.command_id == command.command_id)
    assert all(o.payment_outcome != "CAPTURED" for o in observations)


@pytest.mark.asyncio
async def test_an_accepted_offer_not_paid_in_time_is_abandoned_and_a_late_capture_is_not_attributed(e2e, monkeypatch):
    now = datetime.now(timezone.utc)
    payment_id, _, offer, _ = await _offered_switch(e2e, monkeypatch, now)
    await _paypal_approval(e2e, offer.checkout_order_id, now + timedelta(seconds=60))

    async with e2e.Session() as session:
        [abandoned] = await SwitchOfferService(session).expire_due(now + timedelta(minutes=62))
        await session.commit()
    assert (abandoned.status, abandoned.resolved_at) == ("ABANDONED", abandoned.completion_deadline)

    await _paypal_capture(e2e, offer.checkout_order_id, now + timedelta(minutes=63))
    final = await _offer_for(e2e, payment_id)
    assert (final.status, final.completion_event_id) == ("ABANDONED", None)
    command = await _for_payment(e2e.Session, InterventionCommandModel, payment_id)
    assert all(a.attribution_status != "ATTRIBUTED" for a in await _attributions(e2e, command.command_id))


@pytest.mark.asyncio
async def test_a_declined_paypal_capture_fails_the_offer_without_attribution(e2e, monkeypatch):
    now = datetime.now(timezone.utc)
    payment_id, _, offer, _ = await _offered_switch(e2e, monkeypatch, now)
    await _paypal_approval(e2e, offer.checkout_order_id, now + timedelta(seconds=60))
    await _paypal_capture(e2e, offer.checkout_order_id, now + timedelta(seconds=90), declined=True)

    final = await _offer_for(e2e, payment_id)
    assert (final.status, final.accepted_at is not None) == ("PAYMENT_FAILED", True)
    # The customer took up the offer (treated), but neither the offer nor the original card attempt has
    # completed, so the payment is still unresolved and nothing is attributed.
    command = await _for_payment(e2e.Session, InterventionCommandModel, payment_id)
    [attribution] = await _attributions(e2e, command.command_id)
    assert (attribution.attribution_status, attribution.treatment_status) == ("UNRESOLVED_IN_FLIGHT", "TREATED")
    assert attribution.attributed_protected_gmv_minor_units == 0


# --- duplicates, replays and ordering ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_webhooks_and_pipeline_replays_are_idempotent(e2e, monkeypatch):
    now = datetime.now(timezone.utc)
    payment_id, _, offer, requests = await _offered_switch(e2e, monkeypatch, now)
    order_id = offer.checkout_order_id

    approval_id, first = await _paypal_approval(e2e, order_id, now + timedelta(seconds=60), webhook_id="WH-SWITCH-DUP-APPROVAL")
    _, second = await _paypal_approval(e2e, order_id, now + timedelta(seconds=60), webhook_id="WH-SWITCH-DUP-APPROVAL")
    capture_id = await _paypal_capture(e2e, order_id, now + timedelta(seconds=90))
    assert (first, second) == ("stored", "duplicate")
    before = await _offer_events(e2e, await _offer_for(e2e, payment_id))

    # Replaying every stored event re-runs the pipeline without a new order, offer or offer event.
    stored = await _rows(e2e.Session, PaymentEventModel, PaymentEventModel.source_event_id.in_(
        [f"evt_authorized_{payment_id}", approval_id, capture_id]
    ))
    for row in sorted(stored, key=lambda r: r.ingested_at):
        async with e2e.Session() as session:
            assert (await live_pipeline.run_live_pipeline(_domain_event(row), session=session))["status"] == "success"

    assert len(requests) == 1
    assert len(await _rows(e2e.Session, PaymentMethodSwitchOfferModel)) == 1
    final = await _offer_for(e2e, payment_id)
    assert (final.offer_id, final.status) == (offer.offer_id, "COMPLETED")
    assert await _offer_events(e2e, final) == before == [
        ("OFFER_CREATED", "OFFERED"), ("CUSTOMER_ACCEPTED", "ACCEPTED"), ("PAYMENT_COMPLETED", "COMPLETED"),
    ]
    command = await _for_payment(e2e.Session, InterventionCommandModel, payment_id)
    assert [a.attribution_status for a in await _attributions(e2e, command.command_id)] == ["ATTRIBUTED"]


@pytest.mark.asyncio
async def test_a_capture_delivered_before_the_approval_is_held_until_consent_is_recorded(e2e, monkeypatch):
    now = datetime.now(timezone.utc)
    payment_id, _, offer, _ = await _offered_switch(e2e, monkeypatch, now)
    order_id = offer.checkout_order_id

    capture_id = await _paypal_capture(e2e, order_id, now + timedelta(seconds=90))
    held = await _offer_for(e2e, payment_id)
    assert held.status == "OFFERED"
    command = await _for_payment(e2e.Session, InterventionCommandModel, payment_id)
    assert all(a.attribution_status != "ATTRIBUTED" for a in await _attributions(e2e, command.command_id))

    await _paypal_approval(e2e, order_id, now + timedelta(seconds=60))
    final = await _offer_for(e2e, payment_id)
    capture_event = await _one(e2e.Session, PaymentEventModel, PaymentEventModel.source_event_id == capture_id)
    assert (final.status, final.completion_event_id) == ("COMPLETED", capture_event.event_id)
    assert await _offer_events(e2e, final) == [
        ("OFFER_CREATED", "OFFERED"), ("PAYMENT_OUTCOME_HELD", "OFFERED"),
        ("CUSTOMER_ACCEPTED", "ACCEPTED"), ("PAYMENT_COMPLETED", "COMPLETED"),
    ]
    # The approval resolves the held payment: observed through the offer and attributed only now.
    observations = sorted(
        await _rows(e2e.Session, PaymentOutcomeObservationModel, PaymentOutcomeObservationModel.command_id == command.command_id),
        key=lambda o: o.observation_version,
    )
    assert [o.payment_outcome for o in observations][-1] == "CAPTURED"
    assert observations[-1].terminal_event_id == capture_event.event_id
    attributions = await _attributions(e2e, command.command_id)
    assert [a.attribution_status for a in attributions][-1] == "ATTRIBUTED"
    assert all(a.attribution_status != "ATTRIBUTED" for a in attributions[:-1])


# --- isolation --------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offers_are_isolated_per_attempt_and_from_unrelated_paypal_orders(e2e, monkeypatch):
    now = datetime.now(timezone.utc)
    first_id, _, first_offer, requests = await _offered_switch(e2e, monkeypatch, now)
    second_id = _payment_id()
    await _razorpay(e2e, "payment.authorized", second_id, now + timedelta(seconds=5))
    second_offer = await _offer_for(e2e, second_id)
    assert first_offer.checkout_order_id != second_offer.checkout_order_id
    assert len(requests) == 2

    # The first customer accepts and pays; a capture of the wrong amount on the second order is not a completion.
    await _paypal_approval(e2e, first_offer.checkout_order_id, now + timedelta(seconds=60))
    await _paypal_capture(e2e, first_offer.checkout_order_id, now + timedelta(seconds=90))
    await _paypal_approval(e2e, second_offer.checkout_order_id, now + timedelta(seconds=60))
    await _paypal_capture(e2e, second_offer.checkout_order_id, now + timedelta(seconds=95), value="19.99")

    assert ((await _offer_for(e2e, first_id)).status, (await _offer_for(e2e, second_id)).status) == ("COMPLETED", "ACCEPTED")
    second_command = await _for_payment(e2e.Session, InterventionCommandModel, second_id)
    assert all(a.attribution_status != "ATTRIBUTED" for a in await _attributions(e2e, second_command.command_id))

    # An unrelated PayPal order is its own PayPal attempt and touches no offer.
    unrelated = _paypal_order_id()
    await _paypal_approval(e2e, unrelated, now + timedelta(seconds=100))
    assert paypal_attempt_id(unrelated) in {a.payment_attempt_id for a in await _rows(e2e.Session, PaymentAttemptModel)}
    unrelated_decision = await _for_payment(e2e.Session, InterventionDecisionModel, paypal_attempt_id(unrelated))
    assert unrelated_decision.selected_route_id != SWITCH
    assert len(await _rows(e2e.Session, PaymentMethodSwitchOfferModel)) == 2
