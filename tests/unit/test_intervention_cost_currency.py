"""
Stage 6 cost currency: route costs and the minimum utility are minor units of one currency's cost
schedule and are never weighed against a payment in another currency. The default policy declares INR
and USD schedules; every other currency fails closed. Offline: repositories are mocked; the real
decision service, gates and calculator run.
"""
import dataclasses
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.domain.intervention_models import DecisionType, GateVerdict, InterventionRouteKey
from src.core.domain.payment_attempts import AttemptRef
from src.core.intervention.gates import evaluate_policy_cost_currency
from src.core.intervention.policy import get_default_policy
from src.core.intervention.service import InterventionDecisionService

T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
AMOUNT = 50000
NO_DIAGNOSIS_ROUTES = {InterventionRouteKey.DYNAMIC_RETRY_BACKOFF, InterventionRouteKey.FALLBACK_PAYMENT_LINK}


def test_default_policy_base_costs_are_inr():
    assert get_default_policy().cost_currency == "INR"


def test_default_policy_inr_costs_are_unchanged_and_the_version_records_the_usd_schedule():
    policy = get_default_policy()
    assert (policy.policy_id, policy.policy_version, policy.min_policy_utility_threshold) == (
        "default-recovery-v1", "1.1.0", 50,
    )
    assert {key.value: route.policy_cost_minor_units for key, route in policy.routes.items()} == {
        "RETRY_SECONDARY_GATEWAY": 150, "PROMPT_PAYMENT_METHOD_SWITCH": 300, "DYNAMIC_RETRY_BACKOFF": 50,
        "DEGRADATION_CIRCUIT_BYPASS": 500, "FALLBACK_PAYMENT_LINK": 200,
    }


def test_default_policy_usd_schedule_is_explicit():
    usd = get_default_policy().cost_schedule("USD")
    assert {key.value: cost for key, cost in usd.route_costs_minor_units.items()} == {
        "RETRY_SECONDARY_GATEWAY": 5, "PROMPT_PAYMENT_METHOD_SWITCH": 10, "DYNAMIC_RETRY_BACKOFF": 2,
        "DEGRADATION_CIRCUIT_BYPASS": 15, "FALLBACK_PAYMENT_LINK": 10,
    }
    assert usd.min_policy_utility_threshold == 2


@pytest.mark.parametrize("currency", ["INR", "USD"])
def test_cost_currency_gate_passes_for_currencies_with_a_schedule(currency):
    result = evaluate_policy_cost_currency(get_default_policy(), currency)
    assert (result.passed, result.verdict) == (True, GateVerdict.PASSED)
    assert result.details == {
        "policy_cost_currency": "INR", "cost_schedule_currency": currency, "supported_cost_currencies": ["INR", "USD"],
    }


@pytest.mark.parametrize("currency", ["JPY", "EUR", "inr", "usd", ""])
def test_cost_currency_gate_refuses_every_other_currency(currency):
    result = evaluate_policy_cost_currency(get_default_policy(), currency)
    assert (result.passed, result.verdict) == (False, GateVerdict.FAILED_UNSUPPORTED_COST_CURRENCY)
    assert result.details == {
        "currency": currency,
        "policy_cost_currency": "INR",
        "supported_cost_currencies": ["INR", "USD"],
        "reason": f"Policy route costs are defined in INR, USD; none exist for {currency}",
    }


def _service(currency, probability, policy=None):
    feature_repo = AsyncMock()
    feature_repo.get_payment_event_by_id.return_value = SimpleNamespace(
        amount_minor_units=AMOUNT, currency=currency, payment_method=None, bank=None, wallet=None,
    )
    feature_repo.get_most_severe_active_episode.return_value = None
    intervention_repo = AsyncMock()
    intervention_repo.check_terminal_event_exists.return_value = False
    intervention_repo.get_stage5_prediction_as_of.return_value = SimpleNamespace(
        prediction_id=uuid.uuid4(), prediction_status="PREDICTED", failure_probability=probability,
        feature_snapshot={}, model_name="DeterministicBaselineModel", model_version="untrained-heuristic-v2",
    )
    intervention_repo.get_latest_decision.return_value = None
    intervention_repo.has_recent_route_decision.return_value = False
    intervention_repo.count_recent_act_decisions.return_value = 0
    intervention_repo.get_max_decision_version.return_value = 0
    service = InterventionDecisionService(
        AsyncMock(), intervention_repo, feature_repo, policy, attempt_repository=AsyncMock()
    )
    return service, intervention_repo


def _ref(payment_attempt_id):
    return AttemptRef.for_key("razorpay", payment_attempt_id)


async def test_inr_payment_is_still_scored_and_acted_on():
    service, repo = _service("INR", 0.9)

    decision = await service.decide(_ref("pay_cost_inr"), t_decide=T)

    assert (decision.decision_type, decision.gate_verdict) == (DecisionType.ACT, GateVerdict.PASSED)
    assert InterventionRouteKey(decision.selected_route_id) in NO_DIAGNOSIS_ROUTES
    assert decision.evaluation_audit_payload["gates"]["policy_cost_currency"]["passed"] is True
    assert decision.evaluation_audit_payload["policy"]["cost_currency"] == "INR"
    assert decision.evaluation_audit_payload["policy"]["cost_schedule_currency"] == "INR"
    assert decision.evaluation_audit_payload["policy"]["min_policy_utility_threshold_minor_units"] == 50
    assert {c["route_key"] for c in decision.evaluation_audit_payload["candidates"]} == {
        r.value for r in NO_DIAGNOSIS_ROUTES
    }
    repo.append_decision.assert_awaited_once()


async def test_usd_payment_is_scored_against_the_usd_schedule_only():
    service, repo = _service("USD", 0.9)

    decision = await service.decide(_ref("pay_cost_usd"), t_decide=T)

    assert (decision.decision_type, decision.gate_verdict) == (DecisionType.ACT, GateVerdict.PASSED)
    audit = decision.evaluation_audit_payload
    assert audit["policy"]["cost_schedule_currency"] == "USD"
    assert audit["policy"]["min_policy_utility_threshold_minor_units"] == 2
    usd_costs = get_default_policy().cost_schedule("USD").route_costs_minor_units
    assert {c["route_key"]: c["policy_cost_minor_units"] for c in audit["candidates"]} == {
        r.value: usd_costs[r] for r in NO_DIAGNOSIS_ROUTES
    }
    # 0.9 x 50000 x recovery rate, minus the USD cost in cents
    by_route = {c["route_key"]: c for c in audit["candidates"]}
    assert by_route["FALLBACK_PAYMENT_LINK"]["policy_utility_minor_units"] == 20250 - 10
    assert by_route["DYNAMIC_RETRY_BACKOFF"]["policy_utility_minor_units"] == 15750 - 2
    assert decision.selected_route_id == InterventionRouteKey.FALLBACK_PAYMENT_LINK


async def test_decision_audit_names_the_stage5_model_behind_its_probability():
    service, _ = _service("INR", 0.9)

    decision = await service.decide(_ref("pay_cost_model"), t_decide=T)

    context = decision.evaluation_audit_payload["upstream_context"]
    assert (context["stage5_model_name"], context["stage5_model_version"]) == (
        "DeterministicBaselineModel", "untrained-heuristic-v2",
    )


@pytest.mark.parametrize("currency", ["JPY", "EUR"])
async def test_high_risk_payment_in_another_currency_is_not_acted_on(currency):
    service, repo = _service(currency, 0.9)

    decision = await service.decide(_ref("pay_cost_foreign"), t_decide=T)

    assert (decision.decision_type, decision.gate_verdict) == (
        DecisionType.NO_ACTION, GateVerdict.FAILED_UNSUPPORTED_COST_CURRENCY,
    )
    assert decision.selected_route_id is None
    gate = decision.evaluation_audit_payload["gates"]["policy_cost_currency"]
    assert gate["passed"] is False and gate["details"]["currency"] == currency
    # No route was scored, so no INR cost was compared with the foreign amount.
    repo.has_recent_route_decision.assert_not_awaited()
    repo.count_recent_act_decisions.assert_not_awaited()


async def test_unsupported_cost_currency_audit_explains_why_no_route_was_selected():
    service, repo = _service("JPY", 0.9)

    decision = await service.decide(_ref("pay_cost_audit"), t_decide=T)

    audit = decision.evaluation_audit_payload
    assert set(audit) == {"gates", "extra", "policy", "stage5_model"}
    assert audit["policy"] == {
        "policy_id": "default-recovery-v1", "policy_version": "1.1.0", "cost_currency": "INR",
        "supported_cost_currencies": ["INR", "USD"],
    }
    assert audit["stage5_model"] == {"name": "DeterministicBaselineModel", "version": "untrained-heuristic-v2"}
    assert audit["gates"]["policy_cost_currency"] == {
        "passed": False,
        "details": {
            "currency": "JPY",
            "policy_cost_currency": "INR",
            "supported_cost_currencies": ["INR", "USD"],
            "reason": "Policy route costs are defined in INR, USD; none exist for JPY",
        },
    }
    assert all(gate["passed"] for name, gate in audit["gates"].items() if name != "policy_cost_currency")
    assert audit["extra"] == {"candidates": []}
    [call] = repo.append_decision.await_args_list
    assert call.args[0].evaluation_audit_payload == audit


async def test_moderate_risk_payment_in_another_currency_keeps_its_risk_only_monitor_verdict():
    service, repo = _service("JPY", 0.6)

    decision = await service.decide(_ref("pay_cost_monitor"), t_decide=T)

    assert (decision.decision_type, decision.gate_verdict) == (DecisionType.MONITOR, GateVerdict.PASSED)
    assert decision.evaluation_audit_payload["gates"]["policy_cost_currency"]["passed"] is False
    assert decision.evaluation_audit_payload["candidates"] == []
    repo.has_recent_route_decision.assert_not_awaited()


@pytest.mark.parametrize("probability", [0.3, 0.6])
async def test_below_act_risk_verdicts_do_not_depend_on_route_costs(probability):
    inr, _ = _service("INR", probability)
    usd, _ = _service("JPY", probability)

    inr_decision = await inr.decide(_ref("pay_cost_low_inr"), t_decide=T)
    usd_decision = await usd.decide(_ref("pay_cost_low_usd"), t_decide=T)

    def verdict(d):
        return d.decision_type, d.gate_verdict, d.selected_route_id, d.decision_confidence

    assert verdict(inr_decision) == verdict(usd_decision)
    assert inr_decision.selected_route_id is None


async def test_a_policy_with_costs_in_the_payment_currency_scores_it():
    usd_policy = dataclasses.replace(get_default_policy(), policy_id="test-usd-cost-policy", cost_currency="USD")
    usd, _ = _service("USD", 0.9, usd_policy)
    inr, _ = _service("INR", 0.9, usd_policy)

    usd_decision = await usd.decide(_ref("pay_cost_usd_policy"), t_decide=T)
    inr_decision = await inr.decide(_ref("pay_cost_inr_under_usd"), t_decide=T)

    assert (usd_decision.decision_type, usd_decision.gate_verdict) == (DecisionType.ACT, GateVerdict.PASSED)
    assert (inr_decision.decision_type, inr_decision.gate_verdict) == (
        DecisionType.NO_ACTION, GateVerdict.FAILED_UNSUPPORTED_COST_CURRENCY,
    )
