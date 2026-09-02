"""
Deterministic Simulator Executor for Stage 7 development and testing.

Explicitly flags all execution attempts with:
- is_simulation = True
- executor_name = "SIMULATOR_V1"
Never pretends to be a production gateway integration.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional

from src.core.domain.execution_models import ExecutionResultStatus, InterventionCommand
from src.core.domain.intervention_models import InterventionRouteKey
from src.core.execution.executor import ExecutionResult, InterventionExecutor

_ROUTE_ACTION_TYPE_MAP: Dict[InterventionRouteKey, str] = {
    InterventionRouteKey.RETRY_SECONDARY_GATEWAY: "GATEWAY_REROUTE",
    InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH: "METHOD_SWITCH_PROMPT",
    InterventionRouteKey.DYNAMIC_RETRY_BACKOFF: "BACKOFF_RETRY_SCHEDULE",
    InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS: "CIRCUIT_BYPASS",
    InterventionRouteKey.FALLBACK_PAYMENT_LINK: "PAYMENT_LINK_DISPATCH",
}


class SimulatorMode(str, Enum):
    ALWAYS_SUCCEED = "ALWAYS_SUCCEED"
    ALWAYS_FAIL = "ALWAYS_FAIL"
    SIMULATE_TIMEOUT = "SIMULATE_TIMEOUT"
    ROUTE_SPECIFIC = "ROUTE_SPECIFIC"


class SimulatorInterventionExecutor(InterventionExecutor):
    """
    Deterministic simulator for intervention execution.
    Transparently marks all executions as simulation.
    """

    def __init__(
        self,
        mode: SimulatorMode = SimulatorMode.ALWAYS_SUCCEED,
        simulated_duration_ms: int = 15,
        route_status_map: Optional[Dict[InterventionRouteKey, ExecutionResultStatus]] = None,
    ) -> None:
        self.mode = mode
        self.simulated_duration_ms = simulated_duration_ms
        self.route_status_map = route_status_map or {}

    async def execute(
        self,
        command: InterventionCommand,
        context: Dict[str, Any],
    ) -> ExecutionResult:
        action_type = _ROUTE_ACTION_TYPE_MAP.get(command.route_key, "GENERIC_INTERVENTION")

        if self.mode == SimulatorMode.SIMULATE_TIMEOUT:
            return ExecutionResult(
                status=ExecutionResultStatus.TIMEOUT,
                executor_name="SIMULATOR_V1",
                is_simulation=True,
                provider_action_type=action_type,
                provider_response_code="SIMULATED_TIMEOUT",
                provider_response_payload={
                    "simulator_mode": self.mode.value,
                    "command_id": str(command.command_id),
                    "route_key": command.route_key.value,
                    "timeout_ms": self.simulated_duration_ms,
                },
                error_message="Simulated executor transport timeout: no confirmed result from provider.",
                duration_ms=self.simulated_duration_ms,
            )

        if self.mode == SimulatorMode.ALWAYS_FAIL:
            return ExecutionResult(
                status=ExecutionResultStatus.FAILURE,
                executor_name="SIMULATOR_V1",
                is_simulation=True,
                provider_action_type=action_type,
                provider_response_code="SIMULATED_PROVIDER_REJECTION",
                provider_response_payload={
                    "simulator_mode": self.mode.value,
                    "command_id": str(command.command_id),
                    "route_key": command.route_key.value,
                    "error_code": "GATEWAY_SWITCH_UNAVAILABLE",
                },
                error_message="Simulated provider rejected intervention route instruction.",
                duration_ms=self.simulated_duration_ms,
            )

        if self.mode == SimulatorMode.ROUTE_SPECIFIC:
            status = self.route_status_map.get(command.route_key, ExecutionResultStatus.SUCCESS)
            if status == ExecutionResultStatus.SUCCESS:
                return self._build_success_result(command, action_type)
            elif status == ExecutionResultStatus.TIMEOUT:
                return ExecutionResult(
                    status=ExecutionResultStatus.TIMEOUT,
                    executor_name="SIMULATOR_V1",
                    is_simulation=True,
                    provider_action_type=action_type,
                    provider_response_code="SIMULATED_TIMEOUT",
                    provider_response_payload={"route_key": command.route_key.value},
                    error_message="Simulated route-specific timeout.",
                    duration_ms=self.simulated_duration_ms,
                )
            else:
                return ExecutionResult(
                    status=ExecutionResultStatus.FAILURE,
                    executor_name="SIMULATOR_V1",
                    is_simulation=True,
                    provider_action_type=action_type,
                    provider_response_code="SIMULATED_ROUTE_FAILURE",
                    provider_response_payload={"route_key": command.route_key.value},
                    error_message="Simulated route-specific execution failure.",
                    duration_ms=self.simulated_duration_ms,
                )

        # Default ALWAYS_SUCCEED
        return self._build_success_result(command, action_type)

    def _build_success_result(
        self, command: InterventionCommand, action_type: str
    ) -> ExecutionResult:
        return ExecutionResult(
            status=ExecutionResultStatus.SUCCESS,
            executor_name="SIMULATOR_V1",
            is_simulation=True,
            provider_action_type=action_type,
            provider_response_code="SIMULATED_ACK",
            provider_response_payload={
                "simulator_mode": self.mode.value,
                "command_id": str(command.command_id),
                "route_key": command.route_key.value,
                "acknowledged": True,
            },
            error_message=None,
            duration_ms=self.simulated_duration_ms,
        )
