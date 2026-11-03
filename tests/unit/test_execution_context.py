"""The facts the live pipeline hands an executor come from the decision Stage 6 recorded, never later events."""
import uuid
from types import SimpleNamespace

from src.core.domain.payment_attempts import AttemptRef, paypal_attempt_id
from src.core.services.live_pipeline import execution_context


def _decision(payment_facts):
    return SimpleNamespace(decision_id=uuid.uuid4(), evaluation_audit_payload={"payment_facts": payment_facts})


def test_context_carries_the_decision_amount_currency_and_provider_identity():
    decision = _decision({"amount_minor_units": 50000, "currency": "USD", "payment_method": "PAYPAL"})
    attempt = AttemptRef("paypal", paypal_attempt_id("5O190127TN364715T"), "5O190127TN364715T")

    assert execution_context(decision, attempt) == {
        "source_system": "paypal",
        "payment_attempt_id": "paypal-order:5O190127TN364715T",
        "provider_reference": "5O190127TN364715T",
        "decision_id": str(decision.decision_id),
        "amount_minor_units": 50000,
        "currency": "USD",
    }


def test_context_reports_missing_facts_as_none_instead_of_guessing():
    decision = SimpleNamespace(decision_id=uuid.uuid4(), evaluation_audit_payload={"gates": {}})
    context = execution_context(decision, AttemptRef.for_key("razorpay", "pay_ctx_1"))

    assert (context["amount_minor_units"], context["currency"]) == (None, None)
    assert (context["source_system"], context["provider_reference"]) == ("razorpay", "pay_ctx_1")
