"""
Unit tests for Stage 6 Policy Configuration and Route Catalogue.
"""
from decimal import Decimal

from src.core.domain.intervention_models import InterventionRouteKey
from src.core.intervention.policy import (
    InterventionPolicy,
    RoutePolicyDefinition,
    get_default_policy,
)


def test_default_policy_catalogue_integrity():
    policy = get_default_policy()

    assert policy.policy_id == "default-recovery-v1"
    assert policy.policy_version == "1.2.0"
    assert policy.kill_switch_enabled is False
    assert 0.0 < policy.act_risk_threshold <= 1.0
    assert 0.0 < policy.monitor_risk_threshold < policy.act_risk_threshold
    assert policy.min_policy_utility_threshold >= Decimal("0")
    assert policy.global_rate_limit_per_minute > 0

    # Ensure all 5 standard routes exist in the catalogue
    expected_routes = {
        InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
        InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH,
        InterventionRouteKey.DYNAMIC_RETRY_BACKOFF,
        InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS,
        InterventionRouteKey.FALLBACK_PAYMENT_LINK,
    }
    assert set(policy.routes.keys()) == expected_routes

    for key, route_def in policy.routes.items():
        assert route_def.route_key == key
        assert isinstance(route_def.policy_recovery_rate, Decimal)
        assert 0 < route_def.policy_recovery_rate <= Decimal("1.0")
        assert isinstance(route_def.policy_cost_minor_units, int)

    # Every additional cost schedule prices every route explicitly, in whole minor units
    assert policy.supported_cost_currencies == ("INR", "USD")
    for currency in policy.supported_cost_currencies:
        schedule = policy.cost_schedule(currency)
        assert set(schedule.route_costs_minor_units) == expected_routes
        assert all(isinstance(cost, int) and cost >= 0 for cost in schedule.route_costs_minor_units.values())
        assert schedule.min_policy_utility_threshold >= Decimal("0")
        assert route_def.policy_cost_minor_units >= 0
        assert route_def.cooldown_seconds > 0
        assert len(route_def.target_dimensions) > 0


def test_route_configuration_is_the_default_policy_when_nothing_is_disabled():
    from src.core.intervention.policy import apply_route_configuration
    policy = get_default_policy()
    assert apply_route_configuration(policy, env={}) is policy
    assert apply_route_configuration(policy, env={"RECOVERY_DISABLED_ROUTES": " , "}) is policy


def test_disabling_a_route_changes_only_that_route_and_names_the_variant():
    import dataclasses
    import pytest
    from src.core.intervention.policy import apply_route_configuration

    policy = get_default_policy()
    variant = apply_route_configuration(policy, env={"RECOVERY_DISABLED_ROUTES": "DEGRADATION_CIRCUIT_BYPASS"})
    bypass = InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS

    assert variant.routes[bypass].enabled is False
    assert dataclasses.replace(variant.routes[bypass], enabled=True) == policy.routes[bypass]
    assert {k: r for k, r in variant.routes.items() if k is not bypass} == {
        k: r for k, r in policy.routes.items() if k is not bypass
    }
    assert variant.policy_id.startswith("default-recovery-v1~disabled-") and len(variant.policy_id) <= 50
    assert dataclasses.replace(variant, policy_id=policy.policy_id, routes=policy.routes) == policy
    same = apply_route_configuration(policy, env={"RECOVERY_DISABLED_ROUTES": " DEGRADATION_CIRCUIT_BYPASS,"})
    assert same.policy_id == variant.policy_id

    with pytest.raises(ValueError, match="Unknown intervention routes"):
        apply_route_configuration(policy, env={"RECOVERY_DISABLED_ROUTES": "CAPTURE_EVERYTHING"})
