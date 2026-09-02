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
    assert policy.policy_version == "1.0.0"
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
        assert route_def.policy_cost_minor_units >= 0
        assert route_def.cooldown_seconds > 0
        assert len(route_def.target_dimensions) > 0
