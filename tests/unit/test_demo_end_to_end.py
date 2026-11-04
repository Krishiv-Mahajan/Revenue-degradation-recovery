"""The demonstration's generated webhooks are valid inputs for the real parsers and normalizers."""
import json
from datetime import datetime, timezone

import pytest

from scripts import demo_end_to_end as demo
from src.core.normalizers.paypal import normalize_paypal_event
from src.core.normalizers.razorpay import normalize_razorpay_event
from src.core.parsers.paypal import parse_paypal_event
from src.core.parsers.razorpay import parse_razorpay_event

AT = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("event, kwargs, expected", [
    ("payment.captured", {"method": "upi"}, ("payment.captured", "upi", "INR", 250000)),
    ("payment.failed", {"failed": True}, ("payment.failed", "card", "INR", 250000)),
    ("payment.authorized", {"currency": "USD", "amount": 2000}, ("payment.authorized", "card", "USD", 2000)),
])
def test_razorpay_webhooks_normalize(event, kwargs, expected):
    body = demo._razorpay_webhook(event, "pay_DemoCheck001", int(AT.timestamp()), **kwargs)
    normalized = normalize_razorpay_event(parse_razorpay_event(body), "evt_demo_check")
    assert (normalized.event_type, normalized.payment_method, normalized.currency, normalized.amount_minor_units) == expected


@pytest.mark.parametrize("declined, event_type", [(True, "payment.failed"), (False, "payment.captured")])
def test_paypal_capture_webhooks_normalize(declined, event_type):
    body = demo._paypal_capture("WH-DEMO-CHECK-1", "DEMOCAPCHECK1", "DEMOORDCHECK1", AT, declined=declined)
    normalized = normalize_paypal_event(parse_paypal_event(json.loads(body)))
    assert (normalized.event_type, normalized.order_id, normalized.currency, normalized.amount_minor_units) == (
        event_type, "DEMOORDCHECK1", "USD", 50000,
    )
    assert normalized.timestamp == AT
    assert (normalized.error_reason is not None) is declined


def test_paypal_approval_webhook_normalizes_to_an_order_attempt_trigger():
    body = demo._paypal_approval("WH-DEMO-CHECK-2", "DEMOAPPROVEDCHECK", AT)
    normalized = normalize_paypal_event(parse_paypal_event(json.loads(body)))
    assert (normalized.event_type, normalized.payment_id, normalized.amount_minor_units) == ("order.approved", "DEMOAPPROVEDCHECK", 50000)


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery",  # development database
    "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery_demo",
    "postgresql+asyncpg://postgres:password@db.example.com:5434/payment_recovery_test",
])
def test_the_demo_runs_only_on_the_disposable_test_database(url):
    with pytest.raises(SystemExit, match="Refusing to run the demo"):
        demo._target_database(url)
    assert demo._target_database("postgresql+asyncpg://postgres:password@localhost:5434/payment_recovery_test")[1] == (
        "disposable test database"
    )
