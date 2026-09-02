"""
Stage 7 Executor abstraction.

Completely provider-agnostic interface for intervention dispatch.
Core execution service depends on InterventionExecutor protocol, not concrete SDKs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from src.core.domain.execution_models import ExecutionResultStatus, InterventionCommand


@dataclass(frozen=True)
class ExecutionResult:
    """
    Factual result of an intervention dispatch attempt returned by an executor.
    """
    status: ExecutionResultStatus
    executor_name: str
    is_simulation: bool
    provider_action_type: str
    provider_response_code: Optional[str]
    provider_response_payload: dict[str, Any]
    error_message: Optional[str]
    duration_ms: int


class InterventionExecutor(Protocol):
    """
    Protocol for intervention execution adapters.
    Must never raise unhandled transport/adapter exceptions; must return an ExecutionResult.
    """
    async def execute(
        self,
        command: InterventionCommand,
        context: dict[str, Any],
    ) -> ExecutionResult:
        ...
