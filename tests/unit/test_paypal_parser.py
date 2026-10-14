import pytest

from src.core.domain.exceptions import (
    MalformedInputError,
    SchemaValidationError,
    UnsupportedEventTypeError,
)
from src.core.parsers.paypal import (
    SUPPORTED_EVENT_TYPES,
    PayPalCaptureResource,
    PayPalOrderResource,
    PayPalRefundResource,
    parse_paypal_event,
)
from tests.unit import paypal_event_samples as samples


def test_supported_event_types_are_exactly_the_initial_set():
    assert SUPPORTED_EVENT_TYPES == frozenset(samples.ALL_SUPPORTED)


def test_parse_capture_completed():
    event = parse_paypal_event(samples.capture_completed())
    assert event.event_id == samples.EVENT_ID
    assert event.event_type == "PAYMENT.CAPTURE.COMPLETED"
    assert event.resource_type == "capture"
    assert event.create_time == samples.CREATE_TIME
    assert isinstance(event.resource, PayPalCaptureResource)
    assert event.capture_id == samples.CAPTURE_ID
    assert event.order_id == samples.ORDER_ID
    assert event.resource.amount.value == "500.00"
    assert event.resource.amount.currency_code == "USD"


def test_parse_capture_declined():
    event = parse_paypal_event(samples.capture_declined())
    assert event.event_type == "PAYMENT.CAPTURE.DECLINED"
    assert event.resource.status == "DECLINED"
    assert event.resource.status_details.reason == "DECLINED_BY_RISK_FRAUD_FILTERS"
    assert event.capture_id == samples.CAPTURE_ID


def test_parse_capture_pending():
    event = parse_paypal_event(samples.capture_pending())
    assert event.resource.status == "PENDING"
    assert event.capture_id == samples.CAPTURE_ID


def test_parse_capture_refunded_resolves_refunded_capture_from_up_link():
    event = parse_paypal_event(samples.capture_refunded())
    assert isinstance(event.resource, PayPalRefundResource)
    assert event.resource.id == samples.REFUND_ID
    assert event.capture_id == samples.CAPTURE_ID
    assert event.order_id is None


@pytest.mark.parametrize("builder, status", [
    (samples.order_approved, "APPROVED"),
    (samples.order_completed, "COMPLETED"),
])
def test_parse_order_events(builder, status):
    event = parse_paypal_event(builder())
    assert isinstance(event.resource, PayPalOrderResource)
    assert event.resource.status == status
    assert event.order_id == samples.ORDER_ID
    assert event.capture_id is None


# --- input and envelope ---------------------------------------------------------

@pytest.mark.parametrize("payload", [None, [], "event", b"{}", 42])
def test_non_object_input_is_malformed(payload):
    with pytest.raises(MalformedInputError):
        parse_paypal_event(payload)


@pytest.mark.parametrize("event_id", ["__missing__", "", None, 12345, "WH id with spaces"])
def test_missing_or_invalid_event_id(event_id):
    payload = samples.capture_completed()
    if event_id == "__missing__":
        del payload["id"]
    else:
        payload["id"] = event_id
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


@pytest.mark.parametrize("event_type", ["__missing__", "", None, 7, "payment.capture.completed"])
def test_missing_or_malformed_event_type(event_type):
    payload = samples.capture_completed()
    if event_type == "__missing__":
        del payload["event_type"]
    else:
        payload["event_type"] = event_type
    with pytest.raises(SchemaValidationError) as exc_info:
        parse_paypal_event(payload)
    assert not isinstance(exc_info.value, UnsupportedEventTypeError)


@pytest.mark.parametrize("event_type", [
    "PAYMENT.SALE.COMPLETED",
    "PAYMENT.CAPTURE.DENIED",
    "PAYMENT.CAPTURE.REVERSED",
    "CHECKOUT.PAYMENT-APPROVAL.REVERSED",
])
def test_unsupported_event_type(event_type):
    payload = samples.capture_completed()
    payload["event_type"] = event_type
    with pytest.raises(UnsupportedEventTypeError):
        parse_paypal_event(payload)


def test_v1_capture_denied_is_rejected_even_with_a_declined_capture_resource():
    payload = samples.capture_declined()
    payload["event_type"] = "PAYMENT.CAPTURE.DENIED"
    with pytest.raises(UnsupportedEventTypeError):
        parse_paypal_event(payload)


def test_unsupported_event_type_is_a_schema_validation_error():
    assert issubclass(UnsupportedEventTypeError, SchemaValidationError)


@pytest.mark.parametrize("create_time", ["__missing__", None, 1715836759])
def test_missing_or_non_string_create_time(create_time):
    payload = samples.capture_completed()
    if create_time == "__missing__":
        del payload["create_time"]
    else:
        payload["create_time"] = create_time
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


# --- resource -------------------------------------------------------------------

@pytest.mark.parametrize("resource", ["__missing__", None, [], "capture", 1])
def test_missing_or_malformed_resource(resource):
    payload = samples.capture_completed()
    if resource == "__missing__":
        del payload["resource"]
    else:
        payload["resource"] = resource
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


def test_resource_type_must_match_event_type():
    payload = samples.capture_completed()
    payload["resource_type"] = "checkout-order"
    with pytest.raises(SchemaValidationError, match="resource_type"):
        parse_paypal_event(payload)


@pytest.mark.parametrize("version", ["__missing__", "1.0", 2.0])
def test_only_resource_version_2_is_supported(version):
    payload = samples.capture_completed()
    if version == "__missing__":
        del payload["resource_version"]
    else:
        payload["resource_version"] = version
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


@pytest.mark.parametrize("builder, wrong_status", [
    (samples.capture_completed, "PENDING"),
    (samples.capture_declined, "COMPLETED"),
    (samples.capture_pending, "COMPLETED"),
    (samples.capture_refunded, "PENDING"),
    (samples.order_approved, "COMPLETED"),
    (samples.order_completed, "APPROVED"),
])
def test_resource_status_must_be_consistent_with_event_type(builder, wrong_status):
    payload = builder()
    payload["resource"]["status"] = wrong_status
    with pytest.raises(SchemaValidationError, match="inconsistent"):
        parse_paypal_event(payload)


@pytest.mark.parametrize("builder", list(samples.ALL_SUPPORTED.values()))
def test_missing_resource_identifier(builder):
    payload = builder()
    del payload["resource"]["id"]
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


@pytest.mark.parametrize("builder", list(samples.ALL_SUPPORTED.values()))
def test_malformed_resource_identifier(builder):
    payload = builder()
    payload["resource"]["id"] = "../captures/../../x"
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


def test_capture_without_related_order_id_is_accepted():
    payload = samples.capture_completed()
    del payload["resource"]["supplementary_data"]
    event = parse_paypal_event(payload)
    assert event.capture_id == samples.CAPTURE_ID
    assert event.order_id is None


@pytest.mark.parametrize("links", [
    [],
    [{"href": f"{samples.SANDBOX}/v2/payments/refunds/{samples.REFUND_ID}", "rel": "self"}],
    [{"href": f"https://evil.example.com/v2/payments/captures/{samples.CAPTURE_ID}", "rel": "up"}],
    [{"href": f"http://api.sandbox.paypal.com/v2/payments/captures/{samples.CAPTURE_ID}", "rel": "up"}],
    [{"href": f"{samples.SANDBOX}/v2/checkout/orders/{samples.ORDER_ID}", "rel": "up"}],
    [
        {"href": f"{samples.SANDBOX}/v2/payments/captures/AAA", "rel": "up"},
        {"href": f"{samples.SANDBOX}/v2/payments/captures/BBB", "rel": "up"},
    ],
])
def test_refund_requires_exactly_one_paypal_capture_up_link(links):
    payload = samples.capture_refunded()
    payload["resource"]["links"] = links
    with pytest.raises(SchemaValidationError, match="up"):
        parse_paypal_event(payload)


def test_refund_without_links_is_rejected():
    payload = samples.capture_refunded()
    del payload["resource"]["links"]
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


@pytest.mark.parametrize("purchase_units", ["__missing__", [], None, [{}]])
def test_order_requires_purchase_units_with_amount(purchase_units):
    payload = samples.order_approved()
    if purchase_units == "__missing__":
        del payload["resource"]["purchase_units"]
    else:
        payload["resource"]["purchase_units"] = purchase_units
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


@pytest.mark.parametrize("field", ["value", "currency_code"])
def test_capture_missing_amount_field(field):
    payload = samples.capture_completed()
    del payload["resource"]["amount"][field]
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


def test_capture_missing_amount():
    payload = samples.capture_completed()
    del payload["resource"]["amount"]
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


@pytest.mark.parametrize("value", [500.0, 500, None])
def test_numeric_amount_value_is_rejected(value):
    payload = samples.capture_completed()
    payload["resource"]["amount"]["value"] = value
    with pytest.raises(SchemaValidationError):
        parse_paypal_event(payload)


def test_error_messages_do_not_echo_payload_values():
    payload = samples.capture_completed()
    payload["id"] = "SENSITIVE VALUE buyer@example.com"
    with pytest.raises(SchemaValidationError) as exc_info:
        parse_paypal_event(payload)
    assert "SENSITIVE" not in str(exc_info.value)
    assert "buyer@example.com" not in str(exc_info.value)
