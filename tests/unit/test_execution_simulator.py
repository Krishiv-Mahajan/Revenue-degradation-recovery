"""
Unit tests for Stage 7 SimulatorInterventionExecutor.
"""
import uuid
from datetime import datetime, timezone
import pytest

from src.core.domain.execution_models import (
    CommandStatus,
    ExecutionResultStatus,
    InterventionCommand,
)
from src.core.domain.intervention_models import InterventionRouteKey
from src.core.execution.simulator import SimulatorInterventionExecutor, SimulatorMode


def _dummy_command(route_key: InterventionRouteKey = InterventionRouteKey.RETRY_SECONDARY_GATEWAY) -> InterventionCommand:
    now = datetime.now(timezone.utc)
    return InterventionCommand(
        command_id=uuid.uuid4(),
        decision_id=uuid.uuid4(),
        payment_attempt_id="pay_sim_test",
        decision_version=1,
        route_key=route_key,
        policy_id="test_policy",
        policy_version="1.0.0",
        command_status=CommandStatus.EXECUTING,
        status_reason=None,
        idempotency_key="cmd_sim",
        created_at=now,
        expires_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_simulator_always_succeed():
    sim = SimulatorInterventionExecutor(mode=SimulatorMode.ALWAYS_SUCCEED, simulated_duration_ms=10)
    cmd = _dummy_command()
    res = await sim.execute(cmd, {})

    assert res.status == ExecutionResultStatus.SUCCESS
    assert res.is_simulation is True
    assert res.executor_name == "SIMULATOR_V1"
    assert res.provider_action_type == "GATEWAY_REROUTE"
    assert res.duration_ms == 10
    assert res.error_message is None


@pytest.mark.asyncio
async def test_simulator_always_fail():
    sim = SimulatorInterventionExecutor(mode=SimulatorMode.ALWAYS_FAIL, simulated_duration_ms=15)
    cmd = _dummy_command()
    res = await sim.execute(cmd, {})

    assert res.status == ExecutionResultStatus.FAILURE
    assert res.is_simulation is True
    assert res.executor_name == "SIMULATOR_V1"
    assert res.provider_response_code == "SIMULATED_PROVIDER_REJECTION"
    assert res.duration_ms == 15
    assert res.error_message is not None


@pytest.mark.asyncio
async def test_simulator_timeout():
    sim = SimulatorInterventionExecutor(mode=SimulatorMode.SIMULATE_TIMEOUT, simulated_duration_ms=50)
    cmd = _dummy_command()
    res = await sim.execute(cmd, {})

    assert res.status == ExecutionResultStatus.TIMEOUT
    assert res.is_simulation is True
    assert res.executor_name == "SIMULATOR_V1"
    assert res.provider_response_code == "SIMULATED_TIMEOUT"
    assert res.duration_ms == 50
    assert "timeout" in res.error_message.lower()


@pytest.mark.asyncio
async def test_simulator_route_specific():
    route_map = {
        InterventionRouteKey.RETRY_SECONDARY_GATEWAY: ExecutionResultStatus.SUCCESS,
        InterventionRouteKey.FALLBACK_PAYMENT_LINK: ExecutionResultStatus.FAILURE,
    }
    sim = SimulatorInterventionExecutor(mode=SimulatorMode.ROUTE_SPECIFIC, route_status_map=route_map)

    cmd_retry = _dummy_command(InterventionRouteKey.RETRY_SECONDARY_GATEWAY)
    res_retry = await sim.execute(cmd_retry, {})
    assert res_retry.status == ExecutionResultStatus.SUCCESS

    cmd_link = _dummy_command(InterventionRouteKey.FALLBACK_PAYMENT_LINK)
    res_link = await sim.execute(cmd_link, {})
    assert res_link.status == ExecutionResultStatus.FAILURE
