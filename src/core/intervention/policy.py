"""
Stage 6 — Intervention Decisioning Policy & Route Catalogue.

All policy recovery rates, cost scores, and thresholds are explicit, versioned
business policy assumptions. They must NOT be tuned against synthetic Stage 5 metrics.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Optional, Tuple

from src.core.domain.intervention_models import InterventionRouteKey


@dataclass(frozen=True)
class RoutePolicyDefinition:
    """
    Static policy specification for a single permitted intervention route.
    """
    route_key: InterventionRouteKey
    target_dimensions: Tuple[str, ...]
    policy_recovery_rate: Decimal
    policy_cost_minor_units: int
    cooldown_seconds: int
    requires_diagnosis: bool
    requires_strong_rca: bool
    enabled: bool = True
    # A route that offers the customer another provider's payment method names that provider; it is never
    # eligible for an attempt already paying with it, or in a currency whose payments with that provider
    # this engine cannot verify.
    alternative_payment_provider: Optional[str] = None


@dataclass(frozen=True)
class CurrencyCostSchedule:
    """
    Route costs and the minimum policy utility for one currency, in that currency's minor units.
    Each schedule is an explicit business assumption; schedules are never derived by exchange rates.
    """
    route_costs_minor_units: Mapping[InterventionRouteKey, int]
    min_policy_utility_threshold: Decimal


@dataclass(frozen=True)
class InterventionPolicy:
    """
    Versioned decision policy configuration.
    The routes' policy_cost_minor_units and min_policy_utility_threshold are minor units of cost_currency.
    additional_cost_schedules declare costs for further currencies. A payment is cost-scored only in a
    currency that has a schedule, and only against that schedule (no exchange rates).
    """
    policy_id: str
    policy_version: str
    kill_switch_enabled: bool
    act_risk_threshold: float
    monitor_risk_threshold: float
    min_policy_utility_threshold: Decimal
    global_rate_limit_per_minute: int
    routes: Dict[InterventionRouteKey, RoutePolicyDefinition]
    cost_currency: str = "INR"
    additional_cost_schedules: Mapping[str, CurrencyCostSchedule] = field(default_factory=dict)

    def cost_schedule(self, currency: str) -> Optional[CurrencyCostSchedule]:
        """The cost schedule for a payment currency, or None when the policy defines no costs for it."""
        if currency == self.cost_currency:
            return CurrencyCostSchedule(
                route_costs_minor_units={key: route.policy_cost_minor_units for key, route in self.routes.items()},
                min_policy_utility_threshold=self.min_policy_utility_threshold,
            )
        return self.additional_cost_schedules.get(currency)

    @property
    def supported_cost_currencies(self) -> Tuple[str, ...]:
        return tuple(sorted({self.cost_currency, *self.additional_cost_schedules}))


def get_default_policy() -> InterventionPolicy:
    """
    Factory providing the default v1 intervention policy.
    Explicit policy assumptions:
    - ACT risk threshold: 0.70
    - MONITOR risk threshold: 0.35
    - Min policy utility: 50 minor units (0.50 INR); 2 minor units (0.02 USD)
    - Route costs: an INR schedule (route definitions) and an explicit USD schedule; payments in any
      other currency are not cost-scored
    - Global rate limit: 100 ACT decisions per minute
    Version 1.1.0 added the USD schedule; INR costs and every threshold are unchanged from 1.0.0.
    Version 1.2.0 names PayPal as the alternative payment method PROMPT_PAYMENT_METHOD_SWITCH offers; costs,
    recovery rates, thresholds and every other route are unchanged from 1.1.0.
    """
    routes: Dict[InterventionRouteKey, RoutePolicyDefinition] = {
        InterventionRouteKey.RETRY_SECONDARY_GATEWAY: RoutePolicyDefinition(
            route_key=InterventionRouteKey.RETRY_SECONDARY_GATEWAY,
            target_dimensions=("BANK", "GLOBAL"),
            policy_recovery_rate=Decimal("0.55"),
            policy_cost_minor_units=150,
            cooldown_seconds=300,
            requires_diagnosis=True,
            requires_strong_rca=False,
            enabled=True,
        ),
        InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH: RoutePolicyDefinition(
            route_key=InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH,
            target_dimensions=("PAYMENT_METHOD",),
            policy_recovery_rate=Decimal("0.65"),
            policy_cost_minor_units=300,
            cooldown_seconds=600,
            requires_diagnosis=True,
            requires_strong_rca=True,
            enabled=True,
            alternative_payment_provider="paypal",
        ),
        InterventionRouteKey.DYNAMIC_RETRY_BACKOFF: RoutePolicyDefinition(
            route_key=InterventionRouteKey.DYNAMIC_RETRY_BACKOFF,
            target_dimensions=("GLOBAL", "BANK", "PAYMENT_METHOD"),
            policy_recovery_rate=Decimal("0.35"),
            policy_cost_minor_units=50,
            cooldown_seconds=180,
            requires_diagnosis=False,
            requires_strong_rca=False,
            enabled=True,
        ),
        InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS: RoutePolicyDefinition(
            route_key=InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS,
            target_dimensions=("BANK", "PAYMENT_METHOD"),
            policy_recovery_rate=Decimal("0.75"),
            policy_cost_minor_units=500,
            cooldown_seconds=900,
            requires_diagnosis=True,
            requires_strong_rca=True,
            enabled=True,
        ),
        InterventionRouteKey.FALLBACK_PAYMENT_LINK: RoutePolicyDefinition(
            route_key=InterventionRouteKey.FALLBACK_PAYMENT_LINK,
            target_dimensions=("GLOBAL",),
            policy_recovery_rate=Decimal("0.45"),
            policy_cost_minor_units=200,
            cooldown_seconds=1800,
            requires_diagnosis=False,
            requires_strong_rca=False,
            enabled=True,
        ),
    }

    usd_schedule = CurrencyCostSchedule(
        route_costs_minor_units={
            InterventionRouteKey.RETRY_SECONDARY_GATEWAY: 5,
            InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH: 10,
            InterventionRouteKey.DYNAMIC_RETRY_BACKOFF: 2,
            InterventionRouteKey.DEGRADATION_CIRCUIT_BYPASS: 15,
            InterventionRouteKey.FALLBACK_PAYMENT_LINK: 10,
        },
        min_policy_utility_threshold=Decimal("2"),
    )

    return InterventionPolicy(
        policy_id="default-recovery-v1",
        policy_version="1.2.0",
        kill_switch_enabled=False,
        act_risk_threshold=0.70,
        monitor_risk_threshold=0.35,
        min_policy_utility_threshold=Decimal("50"),
        global_rate_limit_per_minute=100,
        routes=routes,
        cost_currency="INR",
        additional_cost_schedules={"USD": usd_schedule},
    )


# A merchant may switch off routes its stack cannot perform (for example a circuit bypass). Disabling only
# removes candidate routes: thresholds, costs, gates and every other route are unchanged.
DISABLED_ROUTES_ENV = "RECOVERY_DISABLED_ROUTES"


def with_disabled_routes(policy: InterventionPolicy, route_keys: Iterable[str]) -> InterventionPolicy:
    """
    The policy with these routes disabled, under a distinct, deterministic policy id so every decision
    records which variant it used. Unknown route keys are refused.
    """
    keys = sorted({key.strip() for key in route_keys if key.strip()})
    if not keys:
        return policy
    unknown = [key for key in keys if key not in {route.value for route in policy.routes}]
    if unknown:
        raise ValueError(f"Unknown intervention routes in {DISABLED_ROUTES_ENV}: {', '.join(unknown)}")
    routes = {
        key: replace(route, enabled=False) if key.value in keys else route for key, route in policy.routes.items()
    }
    digest = hashlib.sha256(",".join(keys).encode()).hexdigest()[:8]
    return replace(policy, policy_id=f"{policy.policy_id}~disabled-{digest}", routes=routes)


def apply_route_configuration(policy: InterventionPolicy, env: Optional[Mapping[str, str]] = None) -> InterventionPolicy:
    """The live policy: the given policy with the routes named in RECOVERY_DISABLED_ROUTES disabled."""
    env = os.environ if env is None else env
    return with_disabled_routes(policy, (env.get(DISABLED_ROUTES_ENV) or "").split(","))
