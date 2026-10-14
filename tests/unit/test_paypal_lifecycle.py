"""
P1a PayPal lifecycle contract: classification, identity and outcome safety of canonical
PayPal events. Events are produced by the real parser and normalizer from the shared samples.
"""
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.domain import paypal_lifecycle as lifecycle
from src.core.domain.models import PaymentEvent
from src.core.domain.paypal_lifecycle import (
    PAYPAL_LIFECYCLE_CLASSES,
    PayPalCaptureOutcome,
    PayPalLifecycleClass,
    PayPalLifecycleError,
    PayPalOrderCorrelation,
    capture_id,
    capture_outcome,
    classify_paypal_event,
    counts_as_captured_gmv,
    is_payment_sizing_event,
    is_terminal_capture,
    order_correlation,
    order_correlation_key,
    refunded_capture_id,
)
from src.core.domain.source_systems import NON_RAZORPAY_PIPELINE_SOURCES
from src.core.normalizers.paypal import PAYPAL_EVENT_MAPPING, normalize_paypal_event
from src.core.parsers.paypal import parse_paypal_event
from tests.unit import paypal_event_samples as samples

# Event types the existing pipeline treats as terminal outcomes / Stage 5 labels / trigger.
PIPELINE_TERMINAL_EVENT_TYPES = {"payment.captured", "payment.failed"}
PIPELINE_TRIGGER_EVENT_TYPE = "payment.authorized"

SECOND_CAPTURE_ID = "8MC585209K746392H"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _normalize(payload):
    return normalize_paypal_event(parse_paypal_event(payload))


def _with_order(payload, order_id=samples.ORDER_ID):
    payload["resource"]["supplementary_data"] = {"related_ids": {"order_id": order_id}}
    return payload


def _without_order(payload):
    payload["resource"].pop("supplementary_data", None)
    return payload


def _second_capture(builder=samples.capture_completed):
    payload = builder()
    payload["id"] = "WH-SECOND000000000000-000000000000000000"
    payload["resource"]["id"] = SECOND_CAPTURE_ID
    return payload


def _canonical(event_type, source_system="paypal"):
    now = datetime.now(timezone.utc)
    return PaymentEvent(
        event_id=uuid.uuid4(), source_system=source_system, source_event_id="evt", payment_id="pay",
        timestamp=now, event_type=event_type, currency="USD", amount_minor_units=100,
        payment_status="x", ingested_at=now,
    )


# --- A. lifecycle classes ------------------------------------------------------------

@pytest.mark.parametrize(
    "builder, expected_class, terminal, outcome",
    [
        (samples.order_approved, PayPalLifecycleClass.ORDER_CONSENT, False, None),
        (samples.order_completed, PayPalLifecycleClass.ORDER_INFORMATIONAL, False, None),
        (samples.capture_pending, PayPalLifecycleClass.CAPTURE_PENDING, False, None),
        (samples.capture_completed, PayPalLifecycleClass.CAPTURE_SUCCEEDED, True, PayPalCaptureOutcome.CAPTURED),
        (samples.capture_declined, PayPalLifecycleClass.CAPTURE_FAILED, True, PayPalCaptureOutcome.FAILED),
        (samples.capture_refunded, PayPalLifecycleClass.REFUND, False, None),
    ],
)
def test_lifecycle_class_and_terminal_semantics(builder, expected_class, terminal, outcome):
    event = _normalize(builder())
    assert classify_paypal_event(event) is expected_class
    assert is_terminal_capture(event) is terminal
    assert capture_outcome(event) is outcome


def test_every_canonical_paypal_event_type_has_exactly_one_lifecycle_class():
    emitted = {canonical.event_type for canonical in PAYPAL_EVENT_MAPPING.values()}
    assert set(PAYPAL_LIFECYCLE_CLASSES) == emitted
    assert set(PAYPAL_LIFECYCLE_CLASSES.values()) == set(PayPalLifecycleClass)


def test_terminal_classes_match_the_pipeline_terminal_event_types():
    terminal = {t for t, c in PAYPAL_LIFECYCLE_CLASSES.items() if c in lifecycle.TERMINAL_CAPTURE_OUTCOMES}
    assert terminal == PIPELINE_TERMINAL_EVENT_TYPES


@pytest.mark.parametrize("event_type", [PIPELINE_TRIGGER_EVENT_TYPE, "payment.denied", "order.voided", ""])
def test_unknown_event_types_fail_closed(event_type):
    event = _canonical(event_type)
    for helper in (classify_paypal_event, is_terminal_capture, capture_outcome, is_payment_sizing_event,
                   counts_as_captured_gmv, capture_id, refunded_capture_id, order_correlation_key,
                   order_correlation):
        with pytest.raises(PayPalLifecycleError):
            helper(event)


def test_non_paypal_events_fail_closed():
    with pytest.raises(PayPalLifecycleError):
        classify_paypal_event(_canonical("payment.captured", source_system="razorpay"))


# --- B. identity semantics -------------------------------------------------------------

def test_order_approval_uses_the_order_id():
    event = _normalize(samples.order_approved())
    assert event.payment_id == event.order_id == samples.ORDER_ID
    assert order_correlation_key(event) == samples.ORDER_ID
    assert capture_id(event) is None


@pytest.mark.parametrize("builder", [samples.capture_completed, samples.capture_declined])
def test_capture_success_and_decline_use_capture_ids_and_keep_order_id(builder):
    event = _normalize(builder())
    assert capture_id(event) == event.payment_id == samples.CAPTURE_ID
    assert order_correlation_key(event) == samples.ORDER_ID
    assert order_correlation(event) is PayPalOrderCorrelation.CORRELATABLE


@pytest.mark.parametrize("builder", [samples.capture_completed, samples.capture_declined, samples.capture_pending])
def test_capture_without_related_order_id_is_stored_but_not_correlatable(builder):
    event = _normalize(_without_order(builder()))
    assert event.order_id is None
    assert capture_id(event) == samples.CAPTURE_ID
    assert order_correlation_key(event) is None
    assert order_correlation(event) is PayPalOrderCorrelation.NOT_CORRELATABLE


def test_refund_payment_id_refers_to_the_refunded_capture_not_the_refund():
    payload = samples.capture_refunded()
    event = _normalize(payload)

    assert refunded_capture_id(event) == event.payment_id == samples.CAPTURE_ID
    assert capture_id(event) is None
    assert payload["resource"]["id"] == samples.REFUND_ID
    assert samples.REFUND_ID not in {str(v) for v in event.model_dump().values()}


def test_refund_without_order_id_stays_without_order_id():
    event = _normalize(samples.capture_refunded())
    assert event.order_id is None
    assert order_correlation_key(event) is None
    assert order_correlation(event) is PayPalOrderCorrelation.NOT_CORRELATABLE


def test_refund_with_order_id_is_correlatable_to_that_order():
    event = _normalize(_with_order(samples.capture_refunded()))
    assert order_correlation_key(event) == samples.ORDER_ID


def test_multiple_captures_on_one_order_remain_distinct():
    first = _normalize(samples.capture_completed())
    second = _normalize(_second_capture())
    assert capture_id(first) != capture_id(second)
    assert order_correlation_key(first) == order_correlation_key(second) == samples.ORDER_ID
    assert first.source_event_id != second.source_event_id


def test_decline_then_successful_capture_are_two_capture_identities_on_one_order():
    declined = _normalize(samples.capture_declined())
    succeeded = _normalize(_second_capture())
    assert capture_id(declined) != capture_id(succeeded)
    assert order_correlation_key(declined) == order_correlation_key(succeeded) == samples.ORDER_ID
    assert (capture_outcome(declined), capture_outcome(succeeded)) == (
        PayPalCaptureOutcome.FAILED, PayPalCaptureOutcome.CAPTURED,
    )
    # The order-level outcome is undecided: nothing combines capture outcomes per order.
    assert not [name for name in dir(lifecycle) if "order_outcome" in name.lower()]


# --- C. outcome safety ------------------------------------------------------------------

def test_refund_is_never_terminal_failed_recovered_or_sizing():
    refund = _normalize(_with_order(samples.capture_refunded()))
    assert not is_terminal_capture(refund)
    assert capture_outcome(refund) is None
    assert not is_payment_sizing_event(refund)
    assert not counts_as_captured_gmv(refund)
    assert refund.event_type not in PIPELINE_TERMINAL_EVENT_TYPES
    assert PayPalLifecycleClass.REFUND not in lifecycle.TERMINAL_CAPTURE_OUTCOMES
    assert PayPalLifecycleClass.REFUND not in lifecycle.PAYMENT_SIZING_CLASSES


def test_order_approval_is_never_a_successful_capture():
    approved = _normalize(samples.order_approved())
    assert is_payment_sizing_event(approved)
    assert not is_terminal_capture(approved)
    assert capture_outcome(approved) is None
    assert not counts_as_captured_gmv(approved)
    assert approved.event_type not in PIPELINE_TERMINAL_EVENT_TYPES
    assert approved.event_type != PIPELINE_TRIGGER_EVENT_TYPE


def test_order_completed_is_not_counted_as_a_capture():
    completed = _normalize(samples.order_completed())
    assert capture_id(completed) is None
    assert capture_outcome(completed) is None
    assert not counts_as_captured_gmv(completed)
    assert not is_payment_sizing_event(completed)


def test_refund_does_not_change_the_refunded_capture_gross_gmv_or_outcome():
    capture = _normalize(samples.capture_completed())
    refund = _normalize(_with_order(samples.capture_refunded()))
    assert refunded_capture_id(refund) == capture_id(capture)
    assert counts_as_captured_gmv(capture)
    assert capture_outcome(capture) is PayPalCaptureOutcome.CAPTURED
    assert not counts_as_captured_gmv(refund)


def test_only_successful_captures_count_as_captured_gmv_and_only_consent_sizes():
    events = {name: _normalize(builder()) for name, builder in samples.ALL_SUPPORTED.items()}
    assert {n for n, e in events.items() if counts_as_captured_gmv(e)} == {"PAYMENT.CAPTURE.COMPLETED"}
    assert {n for n, e in events.items() if is_payment_sizing_event(e)} == {"CHECKOUT.ORDER.APPROVED"}


# --- D. webhook deduplication identity ----------------------------------------------------

def test_redelivery_keeps_deduplication_identity_and_classification():
    first, redelivery = _normalize(samples.capture_completed()), _normalize(samples.capture_completed())
    assert (first.source_system, first.source_event_id) == (redelivery.source_system, redelivery.source_event_id)
    assert classify_paypal_event(first) is classify_paypal_event(redelivery)


def test_distinct_webhooks_for_one_capture_share_capture_identity_but_not_dedup_identity():
    first = _normalize(samples.capture_completed())
    payload = samples.capture_completed()
    payload["id"] = "WH-OTHER0000000000000-000000000000000000"
    other = _normalize(payload)
    assert capture_id(first) == capture_id(other)
    assert first.source_event_id != other.source_event_id


# --- E. provider isolation ------------------------------------------------------------------

def test_paypal_remains_excluded_from_the_pipeline():
    assert NON_RAZORPAY_PIPELINE_SOURCES == ("paypal",)


def test_lifecycle_helpers_are_not_wired_into_application_code():
    module_path = PROJECT_ROOT / "src/core/domain/paypal_lifecycle.py"
    assert module_path.is_file()
    importers = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "src").rglob("*.py")
        if path != module_path and "paypal_lifecycle" in path.read_text(encoding="utf-8")
    ]
    assert importers == []
