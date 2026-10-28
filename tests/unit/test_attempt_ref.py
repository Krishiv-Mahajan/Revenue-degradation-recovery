"""
Stage 5-8 attempt identity (AttemptRef) and provider-bearing event resolution. Offline.

AttemptRef enforces the same rules as the payment_attempts registry CHECK constraints, so an
identity the database would reject is refused before any query; the PayPal grammar corpus is the one
shared with the parser and the database-backed grammar test.
"""
import uuid
from datetime import datetime, timezone

import pytest

from src.core.domain.models import PaymentEvent
from src.core.domain.payment_attempt_exceptions import (
    InvalidAttemptIdentityError,
    ProviderIdentityMissingError,
    UnsupportedProviderError,
)
from src.core.domain.payment_attempts import (
    AttemptRef,
    PipelineAttempt,
    PipelineRole,
    pipeline_attempt_refusal,
    recorded_provider,
    resolve_pipeline_attempt,
)
from tests.unit.test_migration_sql import PAYPAL_ID_CORPUS

T = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)

# The registry CHECK cases of tests/migration/test_revision_a.py: (source, key, reference, error).
INVALID_IDENTITIES = {
    "unsupported-test_system": ("test_system", "pay_x000000001", "pay_x000000001", UnsupportedProviderError),
    "unsupported-test": ("test", "pay_x000000001", "pay_x000000001", UnsupportedProviderError),
    "unsupported-uppercase": ("RAZORPAY", "pay_x000000001", "pay_x000000001", UnsupportedProviderError),
    "unsupported-empty": ("", "pay_x000000001", "pay_x000000001", UnsupportedProviderError),
    "paypal-no-prefix": ("paypal", "ORDER0001", "ORDER0001", InvalidAttemptIdentityError),
    "paypal-reference-mismatch": ("paypal", "paypal-order:ORDER0001", "ORDER0002", InvalidAttemptIdentityError),
    "paypal-empty-reference": ("paypal", "paypal-order:", "", InvalidAttemptIdentityError),
    "paypal-colon": ("paypal", "paypal-order:AB:CD", "AB:CD", InvalidAttemptIdentityError),
    "paypal-leading-dash": ("paypal", "paypal-order:-ABC", "-ABC", InvalidAttemptIdentityError),
    "paypal-129-chars": ("paypal", "paypal-order:" + "A" * 129, "A" * 129, InvalidAttemptIdentityError),
    "razorpay-prefixed": ("razorpay", "paypal-order:ORDER0001", "paypal-order:ORDER0001", InvalidAttemptIdentityError),
    "razorpay-key-mismatch": ("razorpay", "pay_x000000001", "pay_y000000001", InvalidAttemptIdentityError),
    "razorpay-empty": ("razorpay", "", "", InvalidAttemptIdentityError),
    "synthetic-prefixed": ("synthetic", "paypal-order:ORDER0001", "paypal-order:ORDER0001", InvalidAttemptIdentityError),
    "synthetic-key-mismatch": ("synthetic", "pay_syn_1", "pay_syn_2", InvalidAttemptIdentityError),
}


def _event(source_system, event_type, payment_id="pay_RefTest000001", order_id=None):
    return PaymentEvent(
        event_id=uuid.uuid4(), source_system=source_system, source_event_id=f"evt_{uuid.uuid4().hex}",
        payment_id=payment_id, order_id=order_id, timestamp=T, event_type=event_type, currency="INR",
        amount_minor_units=50000, payment_status=event_type.split(".")[-1], ingested_at=T,
    )


@pytest.mark.parametrize("case", sorted(INVALID_IDENTITIES))
def test_identities_the_registry_rejects_are_refused(case):
    source, key, reference, error = INVALID_IDENTITIES[case]
    with pytest.raises(error):
        AttemptRef(source, key, reference)


@pytest.mark.parametrize("source, key, reference", [
    ("razorpay", "pay_valid000001", "pay_valid000001"),
    ("synthetic", "pay_syn_valid01", "pay_syn_valid01"),
    ("paypal", "paypal-order:A", "A"),
    ("paypal", "paypal-order:" + "Z" * 128, "Z" * 128),
])
def test_identities_the_registry_accepts_are_accepted(source, key, reference):
    ref = AttemptRef(source, key, reference)
    assert ref == AttemptRef.for_key(source, key)
    assert ref.is_paypal is (source == "paypal")


@pytest.mark.parametrize("identifier, accepted", PAYPAL_ID_CORPUS)
def test_paypal_references_follow_the_parser_grammar(identifier, accepted):
    if accepted:
        assert AttemptRef("paypal", f"paypal-order:{identifier}", identifier).provider_reference == identifier
    else:
        with pytest.raises(InvalidAttemptIdentityError):
            AttemptRef("paypal", f"paypal-order:{identifier}", identifier)


def test_for_trigger_event_builds_each_providers_attempt():
    assert AttemptRef.for_trigger_event(_event("razorpay", "payment.authorized")) == AttemptRef(
        "razorpay", "pay_RefTest000001", "pay_RefTest000001"
    )
    assert AttemptRef.for_trigger_event(_event("synthetic", "payment.authorized")) == AttemptRef(
        "synthetic", "pay_RefTest000001", "pay_RefTest000001"
    )
    assert AttemptRef.for_trigger_event(_event("paypal", "order.approved", payment_id="ORDER0001")) == AttemptRef(
        "paypal", "paypal-order:ORDER0001", "ORDER0001"
    )


@pytest.mark.parametrize("source, event_type", [
    ("razorpay", "payment.captured"),
    ("razorpay", "order.approved"),
    ("paypal", "payment.authorized"),
    ("paypal", "payment.captured"),
])
def test_for_trigger_event_rejects_non_trigger_events(source, event_type):
    with pytest.raises(InvalidAttemptIdentityError, match="not an attempt trigger"):
        AttemptRef.for_trigger_event(_event(source, event_type))


def test_for_trigger_event_rejects_unsupported_providers():
    with pytest.raises(UnsupportedProviderError):
        AttemptRef.for_trigger_event(_event("test_system", "payment.authorized"))


@pytest.mark.parametrize("source", ["test_system", "test", "RAZORPAY"])
@pytest.mark.parametrize("event_type", ["payment.authorized", "payment.captured", "payment.failed"])
def test_unsupported_providers_get_no_pipeline_role_and_are_reported(source, event_type):
    event = _event(source, event_type)
    assert resolve_pipeline_attempt(event) is None
    assert "cannot own a Stage 5-8 payment attempt" in pipeline_attempt_refusal(event)


@pytest.mark.parametrize("source", ["razorpay", "synthetic"])
def test_supported_providers_resolve_to_their_own_attempt(source):
    authorized, captured = _event(source, "payment.authorized"), _event(source, "payment.captured")
    ref = AttemptRef(source, "pay_RefTest000001", "pay_RefTest000001")
    assert resolve_pipeline_attempt(authorized) == PipelineAttempt(PipelineRole.TRIGGER, ref)
    assert resolve_pipeline_attempt(captured) == PipelineAttempt(PipelineRole.TERMINAL, ref)
    assert pipeline_attempt_refusal(authorized) is None


def test_events_without_a_pipeline_role_are_not_refusals():
    assert pipeline_attempt_refusal(_event("test_system", "payment.refunded")) is None
    assert pipeline_attempt_refusal(_event("paypal", "order.completed")) is None


def test_namespace_lookalikes_keep_their_refusal_reason():
    lookalike = _event("razorpay", "payment.authorized", payment_id="paypal-order:ORDER0001")
    assert resolve_pipeline_attempt(lookalike) is None
    assert pipeline_attempt_refusal(lookalike) == "payment_id uses the PayPal attempt namespace"


def test_an_empty_non_paypal_payment_id_is_refused_not_resolved():
    event = _event("razorpay", "payment.authorized", payment_id="")
    assert resolve_pipeline_attempt(event) is None
    assert "invalid razorpay attempt identity" in pipeline_attempt_refusal(event)


def test_recorded_provider_never_guesses_a_missing_provider():
    assert recorded_provider("paypal", "row") == "paypal"
    with pytest.raises(ProviderIdentityMissingError, match="failure prediction 1 has no source_system"):
        recorded_provider(None, "failure prediction 1")
