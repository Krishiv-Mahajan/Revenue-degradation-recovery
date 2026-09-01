import pytest
from pydantic import ValidationError
from src.core.normalizers.razorpay import normalize_razorpay_event
from src.core.parsers.razorpay import RazorpayWebhookEvent
from src.core.domain.exceptions import NormalizationError, SchemaValidationError

def test_normalize_razorpay_event_success():
    event = RazorpayWebhookEvent.model_validate({
        "entity": "event",
        "account_id": "acc_123",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_123",
                    "entity": "payment",
                    "amount": 10000,
                    "currency": "inr",
                    "status": "captured",
                    "created_at": 1600000000
                }
            }
        },
        "created_at": 1600000000
    })
    
    payment_event = normalize_razorpay_event(event, "evt_123")
    
    assert payment_event.source_system == "razorpay"
    assert payment_event.source_event_id == "evt_123"
    assert payment_event.payment_id == "pay_123"
    assert payment_event.amount_minor_units == 10000
    assert payment_event.currency == "INR" # Normalized to uppercase
    assert payment_event.event_type == "payment.captured"
    assert payment_event.payment_status == "captured"
    assert payment_event.timestamp.timestamp() == 1600000000

def test_normalize_amount_float_rejection():
    with pytest.raises(ValidationError):
        event = RazorpayWebhookEvent.model_validate({
            "entity": "event",
            "account_id": "acc_123",
            "event": "payment.captured",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_123",
                        "entity": "payment",
                        "amount": 100.00, # Float instead of int
                        "currency": "INR",
                        "status": "captured",
                        "created_at": 1600000000
                    }
                }
            },
            "created_at": 1600000000
        })
