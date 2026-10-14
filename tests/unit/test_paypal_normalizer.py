from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import pytest

from src.core.domain.exceptions import NormalizationError
from src.core.normalizers.paypal import (
    MAX_AMOUNT_MINOR_UNITS,
    PAYPAL_CURRENCY_EXPONENTS,
    PAYPAL_EVENT_MAPPING,
    normalize_paypal_event,
    parse_paypal_timestamp,
    to_minor_units,
)
from src.core.parsers.paypal import SUPPORTED_EVENT_TYPES, parse_paypal_event
from tests.unit import paypal_event_samples as samples

# Event types the existing pipeline classifies (see live_pipeline, analytics, RCA, Stage 5).
PIPELINE_TERMINAL_EVENT_TYPES = {"payment.captured", "payment.failed"}
PIPELINE_TRIGGER_EVENT_TYPE = "payment.authorized"

EXPECTED_TIMESTAMP = datetime(2024, 5, 16, 5, 19, 19, 355000, tzinfo=timezone.utc)


def _normalize(payload):
    return normalize_paypal_event(parse_paypal_event(payload))


def test_mapping_covers_every_supported_event_type():
    assert set(PAYPAL_EVENT_MAPPING) == set(SUPPORTED_EVENT_TYPES)


# --- event semantics ------------------------------------------------------------

def test_capture_completed():
    event = _normalize(samples.capture_completed())
    assert event.source_system == "paypal"
    assert event.source_event_id == samples.EVENT_ID
    assert isinstance(event.event_id, UUID)
    assert event.payment_id == samples.CAPTURE_ID
    assert event.order_id == samples.ORDER_ID
    assert event.event_type == "payment.captured"
    assert event.payment_status == "captured"
    assert event.currency == "USD"
    assert event.amount_minor_units == 50000
    assert isinstance(event.amount_minor_units, int)
    assert event.timestamp == EXPECTED_TIMESTAMP
    assert event.ingested_at.tzinfo is not None
    assert event.error_reason is None


def test_capture_declined_is_a_failed_terminal_outcome():
    event = _normalize(samples.capture_declined())
    assert event.event_type == "payment.failed"
    assert event.payment_status == "failed"
    assert event.payment_id == samples.CAPTURE_ID
    assert event.error_reason == "DECLINED_BY_RISK_FRAUD_FILTERS"


def test_capture_pending_is_not_terminal():
    event = _normalize(samples.capture_pending())
    assert event.event_type == "payment.pending"
    assert event.payment_status == "pending"
    assert event.event_type not in PIPELINE_TERMINAL_EVENT_TYPES
    assert event.error_reason is None


def test_capture_refunded_references_the_refunded_capture():
    event = _normalize(samples.capture_refunded(value="100.00"))
    assert event.event_type == "payment.refunded"
    assert event.payment_status == "refunded"
    assert event.payment_id == samples.CAPTURE_ID
    assert event.amount_minor_units == 10000
    assert event.event_type not in PIPELINE_TERMINAL_EVENT_TYPES


def test_order_approved_is_not_a_completed_or_captured_payment():
    event = _normalize(samples.order_approved())
    assert event.event_type == "order.approved"
    assert event.payment_status == "approved"
    assert event.payment_id == samples.ORDER_ID
    assert event.order_id == samples.ORDER_ID
    assert event.event_type not in PIPELINE_TERMINAL_EVENT_TYPES
    assert event.event_type != PIPELINE_TRIGGER_EVENT_TYPE
    assert event.payment_status != "captured"


def test_order_completed_does_not_double_count_the_capture():
    event = _normalize(samples.order_completed())
    assert event.event_type == "order.completed"
    assert event.payment_status == "completed"
    assert event.payment_id == samples.ORDER_ID
    assert event.event_type not in PIPELINE_TERMINAL_EVENT_TYPES
    assert event.amount_minor_units == 50000


def test_only_capture_completion_and_decline_reach_pipeline_terminal_types():
    terminal = {
        paypal_type
        for paypal_type, canonical in PAYPAL_EVENT_MAPPING.items()
        if canonical.event_type in PIPELINE_TERMINAL_EVENT_TYPES
    }
    assert terminal == {"PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DECLINED"}
    assert "PAYMENT.CAPTURE.DENIED" not in PAYPAL_EVENT_MAPPING
    assert all(c.event_type != PIPELINE_TRIGGER_EVENT_TYPE for c in PAYPAL_EVENT_MAPPING.values())


# --- order / capture correlation ------------------------------------------------

def test_order_lifecycle_events_share_order_id_and_keep_capture_as_payment_id():
    approved = _normalize(samples.order_approved())
    captured = _normalize(samples.capture_completed())
    refunded_payload = samples.capture_refunded()
    refunded_payload["resource"]["supplementary_data"] = {"related_ids": {"order_id": samples.ORDER_ID}}
    refunded = _normalize(refunded_payload)
    completed = _normalize(samples.order_completed())

    assert {e.order_id for e in (approved, captured, refunded, completed)} == {samples.ORDER_ID}
    assert captured.payment_id == refunded.payment_id == samples.CAPTURE_ID
    assert approved.payment_id == completed.payment_id == samples.ORDER_ID
    terminal = [e for e in (approved, captured, refunded, completed) if e.event_type in PIPELINE_TERMINAL_EVENT_TYPES]
    assert terminal == [captured]


def test_declined_capture_and_successful_retry_are_separate_attempts_on_one_order():
    declined = _normalize(samples.capture_declined())
    retry_payload = samples.capture_completed()
    retry_payload["id"] = "WH-RETRY0000000000000-000000000000000000"
    retry_payload["resource"]["id"] = "8MC585209K746392H"
    retry = _normalize(retry_payload)

    assert declined.order_id == retry.order_id == samples.ORDER_ID
    assert declined.payment_id != retry.payment_id
    assert declined.source_event_id != retry.source_event_id
    assert (declined.event_type, retry.event_type) == ("payment.failed", "payment.captured")


def test_redelivered_webhook_keeps_its_deduplication_identity():
    first = _normalize(samples.capture_completed())
    redelivery = _normalize(samples.capture_completed())
    assert (first.source_system, first.source_event_id) == (redelivery.source_system, redelivery.source_event_id)
    assert first.payment_id == redelivery.payment_id


def test_order_with_multiple_purchase_units_is_rejected():
    payload = samples.order_approved()
    unit = payload["resource"]["purchase_units"][0]
    payload["resource"]["purchase_units"] = [unit, dict(unit, reference_id="second")]
    with pytest.raises(NormalizationError, match="multiple purchase units"):
        _normalize(payload)


def test_unsupported_event_type_fails_in_normalizer():
    parsed = parse_paypal_event(samples.capture_completed())
    with pytest.raises(NormalizationError, match="Unsupported"):
        normalize_paypal_event(replace(parsed, event_type="PAYMENT.SALE.COMPLETED"))


def test_missing_payment_identifier_fails_in_normalizer():
    parsed = parse_paypal_event(samples.capture_completed())
    with pytest.raises(NormalizationError, match="payment identifier"):
        normalize_paypal_event(replace(parsed, capture_id=None))


# --- currency and amounts -------------------------------------------------------

def test_currency_exponent_policy():
    assert PAYPAL_CURRENCY_EXPONENTS["USD"] == 2
    assert PAYPAL_CURRENCY_EXPONENTS["EUR"] == 2
    for zero_decimal in ("JPY", "HUF", "TWD"):
        assert PAYPAL_CURRENCY_EXPONENTS[zero_decimal] == 0
    assert "INR" not in PAYPAL_CURRENCY_EXPONENTS


@pytest.mark.parametrize("value, currency, expected", [
    ("500.00", "USD", 50000),
    ("10.5", "EUR", 1050),
    ("0.01", "GBP", 1),
    ("0.29", "USD", 29),
    ("4.35", "USD", 435),
    ("1.15", "CAD", 115),
    ("1500", "JPY", 1500),
    ("100", "HUF", 100),
    ("21474836.47", "USD", MAX_AMOUNT_MINOR_UNITS),
])
def test_to_minor_units_exact_conversion(value, currency, expected):
    result = to_minor_units(value, currency)
    assert result == expected
    assert isinstance(result, int)


def test_zero_decimal_currency_end_to_end():
    event = _normalize(samples.capture_completed(value="1500", currency="JPY"))
    assert event.currency == "JPY"
    assert event.amount_minor_units == 1500


def test_policy_supports_other_exponents_via_explicit_table():
    assert to_minor_units("1.234", "KWD", exponents={"KWD": 3}) == 1234
    with pytest.raises(NormalizationError):
        to_minor_units("1.2345", "KWD", exponents={"KWD": 3})


@pytest.mark.parametrize("value, currency", [
    ("10.505", "USD"),
    ("0.001", "EUR"),
    ("1500.5", "JPY"),
    ("1500.0", "JPY"),
    ("100.00", "HUF"),
])
def test_fractional_precision_beyond_currency_exponent_is_rejected(value, currency):
    with pytest.raises(NormalizationError, match="fractional digits"):
        to_minor_units(value, currency)


@pytest.mark.parametrize("value", [
    "", " 10.00", "10.00 ", "abc", "-1.00", "+1.00", "1e3", "1E3", "1,000.00",
    "10.", ".50", "007", "NaN", "Infinity", "0x10", "１０", "10..0",
])
def test_malformed_decimal_amounts_are_rejected(value):
    with pytest.raises(NormalizationError):
        to_minor_units(value, "USD")


@pytest.mark.parametrize("value", [10.5, 10, None, b"10.00"])
def test_non_string_amounts_are_rejected(value):
    with pytest.raises(NormalizationError):
        to_minor_units(value, "USD")


@pytest.mark.parametrize("value", ["0", "0.00", "0.0"])
def test_zero_amount_is_rejected(value):
    with pytest.raises(NormalizationError, match="greater than zero"):
        to_minor_units(value, "USD")


@pytest.mark.parametrize("value", ["21474836.48", "99999999999999999999", "1" * 33])
def test_out_of_range_amounts_are_rejected(value):
    with pytest.raises(NormalizationError):
        to_minor_units(value, "USD")


@pytest.mark.parametrize("currency", ["INR", "KWD", "XYZ"])
def test_unsupported_currency_is_rejected(currency):
    with pytest.raises(NormalizationError, match="Unsupported PayPal currency"):
        to_minor_units("10.00", currency)


@pytest.mark.parametrize("currency", ["usd", "US", "USDD", "", 840, None])
def test_malformed_currency_code_is_rejected(currency):
    with pytest.raises(NormalizationError, match="three uppercase letters"):
        to_minor_units("10.00", currency)


def test_unsupported_currency_fails_end_to_end():
    with pytest.raises(NormalizationError, match="Unsupported PayPal currency"):
        _normalize(samples.capture_completed(value="500.00", currency="INR"))


def test_malformed_amount_fails_end_to_end():
    with pytest.raises(NormalizationError):
        _normalize(samples.capture_completed(value="5OO.00"))


# --- timestamps -----------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("2024-05-16T05:19:19.355Z", EXPECTED_TIMESTAMP),
    ("2024-05-16T05:19:19Z", datetime(2024, 5, 16, 5, 19, 19, tzinfo=timezone.utc)),
    ("2024-05-16T10:49:19+05:30", datetime(2024, 5, 16, 5, 19, 19, tzinfo=timezone.utc)),
])
def test_timestamps_are_parsed_to_utc(value, expected):
    parsed = parse_paypal_timestamp(value)
    assert parsed == expected
    assert parsed.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("value", [
    "", "not-a-date", "2024-05-16", "2024-05-16T05:19:19", "2024-05-16 05:19:19Z",
    "20240516T051919Z", "2024-13-16T05:19:19Z", "2024-02-30T05:19:19Z", "1715836759",
])
def test_malformed_or_naive_timestamps_are_rejected(value):
    with pytest.raises(NormalizationError):
        parse_paypal_timestamp(value)


def test_malformed_timestamp_fails_end_to_end():
    payload = samples.capture_completed()
    payload["create_time"] = "2024-05-16T05:19:19"
    with pytest.raises(NormalizationError, match="Timestamp"):
        _normalize(payload)
