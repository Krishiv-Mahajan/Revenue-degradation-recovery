"""
PayPal recovery checkout execution, offline. PayPal is replaced by httpx.MockTransport for isolated
testing only; the real client, retry loop, response validation and executor run.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.core.domain.execution_models import CommandStatus, ExecutionResultStatus, InterventionCommand
from src.core.domain.intervention_models import InterventionRouteKey
from src.core.execution.simulator import SimulatorInterventionExecutor
from src.infrastructure.paypal_client import (
    PayPalClient,
    PayPalConfig,
    PayPalEnvironment,
    PayPalRequestRejectedError,
    format_order_amount,
)
from src.infrastructure.paypal_recovery_executor import (
    ACTION_TYPE,
    DELIVERY_NOTE,
    EXECUTOR_NAME,
    PayPalRecoveryCheckoutExecutor,
    RecoveryRoutingExecutor,
    build_intervention_executor,
    recovery_execution_enabled,
)

T = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
ORDER_ID = "5O190127TN364715T"
APPROVE_URL = f"https://www.sandbox.paypal.com/checkoutnow?token={ORDER_ID}"
CONTEXT = {"amount_minor_units": 12345, "currency": "USD", "source_system": "paypal"}


def _command(route=InterventionRouteKey.FALLBACK_PAYMENT_LINK):
    decision_id = uuid.uuid4()
    return InterventionCommand(
        command_id=uuid.uuid4(), decision_id=decision_id, payment_attempt_id="paypal-order:ORIG123",
        source_system="paypal", decision_version=1, route_key=route, policy_id="default-recovery-v1",
        policy_version="1.1.0", command_status=CommandStatus.EXECUTING, status_reason=None,
        idempotency_key=f"cmd_{decision_id}", created_at=T, expires_at=T + timedelta(minutes=5), updated_at=T,
    )


def _order(status="PAYER_ACTION_REQUIRED", approve=APPROVE_URL, order_id=ORDER_ID):
    links = [{"href": f"https://api.sandbox.paypal.com/v2/checkout/orders/{order_id}", "rel": "self"}]
    if approve:
        links.append({"href": approve, "rel": "payer-action" if status == "PAYER_ACTION_REQUIRED" else "approve"})
    return {"id": order_id, "status": status, "links": links}


class FakePayPal:
    """Answers OAuth and queued Orders responses; records every Orders request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.order_requests = []
        self.token_requests = 0

    def __call__(self, request):
        if request.url.path == "/v1/oauth2/token":
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.token_requests}", "token_type": "Bearer", "expires_in": 3600})
        self.order_requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(fake, environment=PayPalEnvironment.SANDBOX):
    config = PayPalConfig(client_id="sandbox-client", client_secret="sandbox-secret", environment=environment)
    return PayPalClient(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))


async def _no_sleep(seconds):
    _no_sleep.calls.append(seconds)


_no_sleep.calls = []


def _executor(fake, enabled=True, environment=PayPalEnvironment.SANDBOX):
    _no_sleep.calls = []
    return PayPalRecoveryCheckoutExecutor(_client(fake, environment), enabled=lambda: enabled, sleep=_no_sleep)


# --- amounts ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("amount, currency, expected", [
    (12345, "USD", "123.45"), (5, "USD", "0.05"), (100, "EUR", "1.00"), (1000, "JPY", "1000"), (50000, "INR", "500.00"),
])
def test_order_amounts_use_the_currency_exponent(amount, currency, expected):
    assert format_order_amount(amount, currency) == expected


@pytest.mark.parametrize("amount, currency", [(0, "USD"), (-5, "USD"), (True, "USD"), (12.5, "USD"), (100, "XYZ"), (100, "usd")])
def test_invalid_amounts_and_currencies_are_refused(amount, currency):
    with pytest.raises(ValueError):
        format_order_amount(amount, currency)


# --- successful execution --------------------------------------------------------------------------

async def test_creates_a_sandbox_checkout_and_records_real_provenance():
    fake = FakePayPal(httpx.Response(201, json=_order(), headers={"paypal-debug-id": "f00dcafe1234"}))
    command = _command()

    result = await _executor(fake).execute(command, CONTEXT)

    assert (result.status, result.executor_name, result.is_simulation) == (ExecutionResultStatus.SUCCESS, EXECUTOR_NAME, False)
    assert (result.provider_action_type, result.provider_response_code, result.error_message) == (
        ACTION_TYPE, "PAYER_ACTION_REQUIRED", None,
    )
    assert result.provider_response_payload == {
        "environment": "sandbox", "paypal_request_id": command.idempotency_key, "route_key": "FALLBACK_PAYMENT_LINK",
        "payment_attempt_id": "paypal-order:ORIG123", "amount": {"currency_code": "USD", "value": "123.45"},
        "paypal_order_id": ORDER_ID, "order_status": "PAYER_ACTION_REQUIRED", "approve_url": APPROVE_URL,
        "paypal_debug_id": "f00dcafe1234", "idempotent_replay": False, "http_attempts": 1, "delivery": DELIVERY_NOTE,
    }

    [request] = fake.order_requests
    assert (request.method, str(request.url)) == ("POST", "https://api-m.sandbox.paypal.com/v2/checkout/orders")
    assert request.headers["PayPal-Request-Id"] == command.idempotency_key
    assert request.headers["Authorization"] == "Bearer tok-1"
    assert json.loads(request.content) == {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": "paypal-order:ORIG123", "custom_id": str(command.command_id),
            "description": "Payment recovery checkout", "amount": {"currency_code": "USD", "value": "123.45"},
        }],
    }


async def test_a_replayed_request_returns_the_same_order_and_says_so():
    fake = FakePayPal(httpx.Response(200, json=_order(status="CREATED")))
    result = await _executor(fake).execute(_command(), CONTEXT)
    assert (result.status, result.provider_response_payload["idempotent_replay"]) == (ExecutionResultStatus.SUCCESS, True)


async def test_retryable_failures_are_retried_with_the_same_request_id():
    fake = FakePayPal(
        httpx.ReadTimeout("slow"), httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}), httpx.Response(201, json=_order()),
    )
    command = _command()

    result = await _executor(fake).execute(command, CONTEXT)

    assert result.status == ExecutionResultStatus.SUCCESS
    assert result.provider_response_payload["http_attempts"] == 3
    assert {r.headers["PayPal-Request-Id"] for r in fake.order_requests} == {command.idempotency_key}
    assert _no_sleep.calls == [0.5, 1.0]


async def test_an_expired_token_is_refreshed_once():
    fake = FakePayPal(httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"}), httpx.Response(201, json=_order()))
    result = await _executor(fake).execute(_command(), CONTEXT)
    assert result.status == ExecutionResultStatus.SUCCESS
    assert [r.headers["Authorization"] for r in fake.order_requests] == ["Bearer tok-1", "Bearer tok-2"]


# --- failures are classified, never raised ----------------------------------------------------------

async def test_exhausted_timeouts_are_reported_as_timeout():
    fake = FakePayPal(httpx.ReadTimeout("a"), httpx.ReadTimeout("b"), httpx.ReadTimeout("c"))
    result = await _executor(fake).execute(_command(), CONTEXT)
    assert (result.status, result.provider_response_code, result.is_simulation) == (
        ExecutionResultStatus.TIMEOUT, "PAYPAL_TIMEOUT", False,
    )
    assert len(fake.order_requests) == 3


async def test_exhausted_server_errors_are_reported_as_unavailable():
    fake = FakePayPal(*[httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"}) for _ in range(3)])
    result = await _executor(fake).execute(_command(), CONTEXT)
    assert (result.status, result.provider_response_code) == (ExecutionResultStatus.FAILURE, "PAYPAL_UNAVAILABLE")


async def test_a_validation_rejection_is_not_retried_and_keeps_paypals_issue_code():
    body = {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "CURRENCY_NOT_SUPPORTED"}]}
    fake = FakePayPal(httpx.Response(422, json=body, headers={"paypal-debug-id": "abc123"}))
    result = await _executor(fake).execute(_command(), CONTEXT)
    assert (result.status, result.provider_response_code) == (ExecutionResultStatus.FAILURE, "CURRENCY_NOT_SUPPORTED")
    assert "debug_id=abc123" in result.error_message
    assert len(fake.order_requests) == 1


async def test_rejected_credentials_are_reported_without_secrets():
    fake = FakePayPal(httpx.Response(401, json={}), httpx.Response(403, json={}))
    result = await _executor(fake).execute(_command(), CONTEXT)
    assert (result.status, result.provider_response_code) == (ExecutionResultStatus.FAILURE, "PAYPAL_AUTH_REJECTED")
    assert "sandbox-secret" not in result.error_message and "tok-" not in result.error_message


async def test_an_order_without_a_paypal_approval_link_is_not_success():
    fake = FakePayPal(httpx.Response(201, json=_order(approve="https://evil.example.com/approve")))
    result = await _executor(fake).execute(_command(), CONTEXT)
    assert (result.status, result.provider_response_code) == (ExecutionResultStatus.FAILURE, "ORDER_PAYER_ACTION_REQUIRED")
    assert result.provider_response_payload["approve_url"] is None


async def test_a_malformed_order_response_is_unavailable():
    fake = FakePayPal(*[httpx.Response(201, json={"id": "bad id!", "status": "CREATED"}) for _ in range(1)])
    result = await _executor(fake).execute(_command(), CONTEXT)
    assert (result.status, result.provider_response_code) == (ExecutionResultStatus.FAILURE, "PAYPAL_UNAVAILABLE")


# --- refusals happen before any request ------------------------------------------------------------

@pytest.mark.parametrize("kwargs, context, route, code", [
    ({"enabled": False}, CONTEXT, InterventionRouteKey.FALLBACK_PAYMENT_LINK, "KILL_SWITCH_ACTIVE"),
    ({"environment": PayPalEnvironment.LIVE}, CONTEXT, InterventionRouteKey.FALLBACK_PAYMENT_LINK, "LIVE_ENVIRONMENT_REFUSED"),
    ({}, {**CONTEXT, "amount_minor_units": None}, InterventionRouteKey.FALLBACK_PAYMENT_LINK, "INVALID_PAYMENT_FACTS"),
    ({}, {**CONTEXT, "currency": "XYZ"}, InterventionRouteKey.FALLBACK_PAYMENT_LINK, "INVALID_PAYMENT_FACTS"),
    ({}, CONTEXT, InterventionRouteKey.RETRY_SECONDARY_GATEWAY, "UNSUPPORTED_ROUTE"),
])
async def test_refusals_make_no_paypal_request(kwargs, context, route, code):
    fake = FakePayPal()
    result = await _executor(fake, **kwargs).execute(_command(route), context)
    assert (result.status, result.provider_response_code, result.is_simulation) == (
        ExecutionResultStatus.FAILURE, code, False,
    )
    assert (fake.order_requests, fake.token_requests) == ([], 0)


async def test_get_order_reads_status_without_changing_it():
    fake = FakePayPal(httpx.Response(200, json=_order(status="APPROVED")))
    order = await _client(fake).get_order(ORDER_ID)
    assert (order.order_id, order.status) == (ORDER_ID, "APPROVED")
    [request] = fake.order_requests
    assert (request.method, request.url.path) == ("GET", f"/v2/checkout/orders/{ORDER_ID}")


async def test_create_order_refuses_an_invalid_request_id():
    with pytest.raises(ValueError):
        await _client(FakePayPal()).create_order(
            amount_minor_units=100, currency="USD", request_id="has spaces", custom_id="c", reference_id="r", description="d",
        )


async def test_create_order_rejection_raises_with_issue():
    fake = FakePayPal(httpx.Response(400, json={"name": "INVALID_REQUEST", "details": [{"issue": "MISSING_REQUIRED_PARAMETER"}]}))
    with pytest.raises(PayPalRequestRejectedError) as excinfo:
        await _client(fake).create_order(
            amount_minor_units=100, currency="USD", request_id="cmd_1", custom_id="c", reference_id="r", description="d",
        )
    assert excinfo.value.issue == "MISSING_REQUIRED_PARAMETER"


# --- routing and construction ----------------------------------------------------------------------

async def test_routing_sends_only_supported_routes_to_paypal():
    fake = FakePayPal(httpx.Response(201, json=_order()))
    router = RecoveryRoutingExecutor(_executor(fake), SimulatorInterventionExecutor())

    real = await router.execute(_command(), CONTEXT)
    simulated = await router.execute(_command(InterventionRouteKey.DYNAMIC_RETRY_BACKOFF), CONTEXT)

    assert (real.executor_name, real.is_simulation) == (EXECUTOR_NAME, False)
    assert (simulated.executor_name, simulated.is_simulation) == ("SIMULATOR_V1", True)
    assert len(fake.order_requests) == 1


SANDBOX_ENV = {
    "PAYPAL_RECOVERY_EXECUTION_ENABLED": "true", "PAYPAL_CLIENT_ID": "sandbox-client",
    "PAYPAL_CLIENT_SECRET": "sandbox-secret", "PAYPAL_ENVIRONMENT": "sandbox",
}


@pytest.mark.parametrize("env", [
    {},
    {**SANDBOX_ENV, "PAYPAL_RECOVERY_EXECUTION_ENABLED": "false"},
    {**SANDBOX_ENV, "PAYPAL_CLIENT_SECRET": ""},
    {**SANDBOX_ENV, "PAYPAL_ENVIRONMENT": "live"},
])
def test_the_simulator_is_used_unless_sandbox_recovery_is_enabled_and_configured(env):
    simulator = SimulatorInterventionExecutor()
    assert build_intervention_executor(simulator, env=env) is simulator


def test_enabled_sandbox_configuration_routes_supported_routes_to_paypal():
    simulator = SimulatorInterventionExecutor()
    executor = build_intervention_executor(simulator, env=SANDBOX_ENV)
    assert isinstance(executor, RecoveryRoutingExecutor)
    assert executor.simulator is simulator
    assert executor.real.client.environment is PayPalEnvironment.SANDBOX


@pytest.mark.parametrize("value, expected", [("true", True), ("TRUE", True), (" true ", True), ("1", False), ("", False)])
def test_kill_switch_requires_an_explicit_true(value, expected):
    assert recovery_execution_enabled({"PAYPAL_RECOVERY_EXECUTION_ENABLED": value}) is expected
