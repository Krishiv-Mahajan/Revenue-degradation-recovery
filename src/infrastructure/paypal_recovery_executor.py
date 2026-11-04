"""
Real PayPal recovery execution (sandbox only).

Recovery actions: for an ACT decision on FALLBACK_PAYMENT_LINK or PROMPT_PAYMENT_METHOD_SWITCH, create a
PayPal Orders v2 checkout for the at-risk amount (the amount and currency Stage 6 recorded) and record its
approval link. FALLBACK_PAYMENT_LINK is a fallback payment link; PROMPT_PAYMENT_METHOD_SWITCH offers the
same kind of checkout as an alternative payment method the customer may accept or decline (see
src/core/domain/switch_offer_models.py), and is refused for an attempt already paying with PayPal.
In both cases:
- creating the order moves no money; the payer must approve it at PayPal;
- capturing an approved order is a separate merchant step this engine never performs;
- the link is NOT delivered to the payer by this engine; delivery needs a merchant channel (email, SMS,
  checkout UI), so the recorded payload says so.

Safety:
- live PayPal is refused outright, before any request;
- PAYPAL_RECOVERY_EXECUTION_ENABLED=true is required at execution time (kill switch);
- PayPal-Request-Id is the command's idempotency key, so retries and re-executions return the same order;
- every result is is_simulation=False and names this executor, so it can never be confused with
  SIMULATOR_V1; routes this executor does not implement go to the simulator, which labels itself.
"""
import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from src.core.domain.execution_models import ExecutionResultStatus, InterventionCommand
from src.core.domain.intervention_models import InterventionRouteKey
from src.core.execution.executor import ExecutionResult, InterventionExecutor
from src.infrastructure.execution_repository import RECOVERY_ORDER_ID_KEY
from src.infrastructure.paypal_client import (
    DEFAULT_ORDER_RETRY_DELAYS,
    PayPalAuthError,
    PayPalClient,
    PayPalConfig,
    PayPalConfigurationError,
    PayPalEnvironment,
    PayPalRequestRejectedError,
    PayPalTimeoutError,
    PayPalUnavailableError,
    format_order_amount,
)

logger = logging.getLogger(__name__)

EXECUTOR_NAME = "PAYPAL_ORDERS_V2_SANDBOX"
ACTION_TYPE = "PAYPAL_RECOVERY_CHECKOUT"
SWITCH_OFFER_ACTION_TYPE = "PAYPAL_PAYMENT_METHOD_SWITCH_OFFER"
RECOVERY_EXECUTION_ENABLED_ENV = "PAYPAL_RECOVERY_EXECUTION_ENABLED"
SUPPORTED_ROUTES = frozenset({
    InterventionRouteKey.FALLBACK_PAYMENT_LINK, InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH,
})
ACTION_TYPES = {
    InterventionRouteKey.FALLBACK_PAYMENT_LINK: ACTION_TYPE,
    InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH: SWITCH_OFFER_ACTION_TYPE,
}
ORDER_DESCRIPTIONS = {
    InterventionRouteKey.FALLBACK_PAYMENT_LINK: "Payment recovery checkout",
    InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH: "Pay with PayPal instead",
}
SUCCESS_ORDER_STATUSES = frozenset({"CREATED", "PAYER_ACTION_REQUIRED", "APPROVED", "COMPLETED"})
DELIVERY_NOTE = "NOT_DELIVERED: approve_url must reach the payer through a merchant channel"
SWITCH_OFFER_TERMS = {
    "consent": "CUSTOMER_APPROVAL_REQUIRED: only the customer's approval at PayPal accepts the offer",
    "capture": "NEVER_BY_THIS_ENGINE: capturing an approved order is the merchant's step",
}


def recovery_execution_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return (env.get(RECOVERY_EXECUTION_ENABLED_ENV) or "").strip().lower() == "true"


class PayPalRecoveryCheckoutExecutor(InterventionExecutor):
    """Creates a sandbox PayPal checkout order as a recovery payment link. Never raises."""

    def __init__(
        self,
        client: PayPalClient,
        enabled: Callable[[], bool] = recovery_execution_enabled,
        clock: Callable[[], float] = time.monotonic,
        retry_delays: Tuple[float, ...] = DEFAULT_ORDER_RETRY_DELAYS,
        sleep=asyncio.sleep,
    ) -> None:
        self.client = client
        self.enabled = enabled
        self.clock = clock
        self.retry_delays = retry_delays
        self.sleep = sleep

    async def execute(self, command: InterventionCommand, context: Dict[str, Any]) -> ExecutionResult:
        started = self.clock()
        environment = self.client.environment
        payload: Dict[str, Any] = {
            "environment": environment.value,
            "paypal_request_id": command.idempotency_key,
            "route_key": command.route_key.value,
            "payment_attempt_id": command.payment_attempt_id,
        }
        switch_offer = command.route_key is InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH
        if switch_offer:
            payload["switch_offer"] = dict(SWITCH_OFFER_TERMS)

        def result(status, code, error=None, **extra):
            return ExecutionResult(
                status=status,
                executor_name=EXECUTOR_NAME,
                is_simulation=False,
                provider_action_type=ACTION_TYPES.get(command.route_key, ACTION_TYPE),
                provider_response_code=code,
                provider_response_payload={**payload, **extra},
                error_message=error,
                duration_ms=max(0, int((self.clock() - started) * 1000)),
            )

        if command.route_key not in SUPPORTED_ROUTES:
            return result(ExecutionResultStatus.FAILURE, "UNSUPPORTED_ROUTE", "Route has no PayPal recovery action")
        if environment is not PayPalEnvironment.SANDBOX:
            return result(ExecutionResultStatus.FAILURE, "LIVE_ENVIRONMENT_REFUSED", "Live PayPal execution is refused")
        if not self.enabled():
            return result(ExecutionResultStatus.FAILURE, "KILL_SWITCH_ACTIVE", "PayPal recovery execution is disabled")
        if switch_offer and context.get("source_system") in (None, "paypal"):
            return result(
                ExecutionResultStatus.FAILURE, "ALTERNATIVE_METHOD_UNAVAILABLE",
                "A PayPal switch offer needs an original attempt with another provider",
            )

        amount, currency = context.get("amount_minor_units"), context.get("currency")
        try:
            value = format_order_amount(amount, currency)
        except ValueError as exc:
            return result(ExecutionResultStatus.FAILURE, "INVALID_PAYMENT_FACTS", str(exc))
        payload["amount"] = {"currency_code": currency, "value": value}

        try:
            order = await self.client.create_order(
                amount_minor_units=amount,
                currency=currency,
                request_id=command.idempotency_key,
                custom_id=str(command.command_id),
                reference_id=command.payment_attempt_id,
                description=ORDER_DESCRIPTIONS[command.route_key],
                retry_delays=self.retry_delays,
                sleep=self.sleep,
            )
        except PayPalTimeoutError as exc:
            return result(ExecutionResultStatus.TIMEOUT, "PAYPAL_TIMEOUT", str(exc))
        except PayPalAuthError as exc:
            return result(ExecutionResultStatus.FAILURE, "PAYPAL_AUTH_REJECTED", str(exc))
        except PayPalRequestRejectedError as exc:
            return result(ExecutionResultStatus.FAILURE, exc.issue or "PAYPAL_REQUEST_REJECTED", str(exc))
        except PayPalUnavailableError as exc:
            return result(ExecutionResultStatus.FAILURE, "PAYPAL_UNAVAILABLE", str(exc))
        except (PayPalConfigurationError, ValueError) as exc:
            return result(ExecutionResultStatus.FAILURE, "PAYPAL_CONFIGURATION", str(exc))

        order_facts = {
            RECOVERY_ORDER_ID_KEY: order.order_id,
            "order_status": order.status,
            "approve_url": order.approve_url,
            "paypal_debug_id": order.debug_id,
            "idempotent_replay": order.replayed,
            "http_attempts": order.http_attempts,
            "delivery": DELIVERY_NOTE,
        }
        usable = order.status in SUCCESS_ORDER_STATUSES and (
            order.approve_url is not None or order.status in ("APPROVED", "COMPLETED")
        )
        if not usable:
            return result(
                ExecutionResultStatus.FAILURE, f"ORDER_{order.status}", "PayPal order has no usable approval link",
                **order_facts,
            )
        return result(ExecutionResultStatus.SUCCESS, order.status, None, **order_facts)


class RecoveryRoutingExecutor(InterventionExecutor):
    """Real PayPal execution for the routes it implements; every other route goes to the simulator."""

    def __init__(self, real: InterventionExecutor, simulator: InterventionExecutor, routes=SUPPORTED_ROUTES) -> None:
        self.real = real
        self.simulator = simulator
        self.routes = routes

    async def execute(self, command: InterventionCommand, context: Dict[str, Any]) -> ExecutionResult:
        executor = self.real if command.route_key in self.routes else self.simulator
        return await executor.execute(command, context)


_recovery_clients: Dict[Tuple[str, str], PayPalClient] = {}


def build_intervention_executor(
    simulator: InterventionExecutor, env: Optional[Mapping[str, str]] = None
) -> InterventionExecutor:
    """
    The executor the live pipeline uses: the simulator unless real PayPal recovery is enabled and a
    sandbox configuration is present. Misconfiguration falls back to the simulator with a warning;
    live configuration is refused.
    """
    env = os.environ if env is None else env
    if not recovery_execution_enabled(env):
        return simulator
    try:
        config = PayPalConfig.from_env(env)
    except PayPalConfigurationError as exc:
        logger.warning(f"[PayPalRecovery] enabled but not configured ({exc}); using the simulator")
        return simulator
    if config.environment is not PayPalEnvironment.SANDBOX:
        logger.error("[PayPalRecovery] refusing live PayPal recovery execution; using the simulator")
        return simulator
    key = (config.client_id, config.environment.value)
    client = _recovery_clients.get(key)
    if client is None:
        client = _recovery_clients[key] = PayPalClient(config)
    return RecoveryRoutingExecutor(
        PayPalRecoveryCheckoutExecutor(client, enabled=lambda: recovery_execution_enabled(env)), simulator
    )


async def close_recovery_clients() -> None:
    clients = list(_recovery_clients.values())
    _recovery_clients.clear()
    for client in clients:
        await client.aclose()
