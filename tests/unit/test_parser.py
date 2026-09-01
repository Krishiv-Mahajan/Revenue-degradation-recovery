import pytest
import hmac
import hashlib
import json
from src.core.parsers.razorpay import parse_razorpay_event, verify_razorpay_signature
from src.core.domain.exceptions import (
    InvalidWebhookSignatureError,
    MalformedInputError,
    SchemaValidationError
)

def test_verify_razorpay_signature_valid():
    secret = "my_secret"
    payload = b'{"event":"payment.failed"}'
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    
    # Should not raise
    verify_razorpay_signature(payload, signature, secret)

def test_verify_razorpay_signature_invalid():
    secret = "my_secret"
    payload = b'{"event":"payment.failed"}'
    
    with pytest.raises(InvalidWebhookSignatureError):
        verify_razorpay_signature(payload, "invalid_sig", secret)

def test_parse_razorpay_event_valid():
    payload = json.dumps({
        "entity": "event",
        "account_id": "acc_123",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_123",
                    "entity": "payment",
                    "amount": 50000,
                    "currency": "INR",
                    "status": "failed",
                    "created_at": 1600000000
                }
            }
        },
        "created_at": 1600000000
    }).encode()
    
    event = parse_razorpay_event(payload)
    assert event.event == "payment.failed"
    assert event.payload.payment.entity.amount == 50000
    assert event.payload.payment.entity.currency == "INR"

def test_parse_razorpay_event_invalid_json():
    with pytest.raises(MalformedInputError):
        parse_razorpay_event(b'{"invalid": json')

def test_parse_razorpay_event_schema_error():
    payload = json.dumps({
        "entity": "wrong_entity", # should be 'event'
        "account_id": "acc_123",
    }).encode()
    
    with pytest.raises(SchemaValidationError):
        parse_razorpay_event(payload)
