import hmac
import hashlib
import json
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator, ValidationError, StrictInt
from src.core.domain.exceptions import (
    InvalidWebhookSignatureError,
    MalformedInputError,
    SchemaValidationError
)

def verify_razorpay_signature(payload_body: bytes, signature: str, secret: str) -> None:
    """
    Verifies the Razorpay webhook signature using the raw HTTP body.
    """
    if not signature:
        raise InvalidWebhookSignatureError("Missing Razorpay signature header")
    if not secret:
        raise InvalidWebhookSignatureError("Webhook secret is not configured")
        
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        payload_body,
        hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(expected_signature, signature):
        raise InvalidWebhookSignatureError("Signature verification failed")


class RazorpayPaymentEntity(BaseModel):
    """Represents the actual payment object inside the webhook payload."""
    id: str
    entity: str
    amount: StrictInt
    currency: str
    status: str
    order_id: Optional[str] = None
    method: Optional[str] = None
    bank: Optional[str] = None
    wallet: Optional[str] = None
    vpa: Optional[str] = None
    created_at: int
    
    error_code: Optional[str] = None
    error_description: Optional[str] = None
    error_source: Optional[str] = None
    error_step: Optional[str] = None
    error_reason: Optional[str] = None


class RazorpayPaymentWrapper(BaseModel):
    """Wraps the payment entity."""
    entity: RazorpayPaymentEntity


class RazorpayPayload(BaseModel):
    """The payload section of the webhook."""
    payment: RazorpayPaymentWrapper


class RazorpayWebhookEvent(BaseModel):
    """The root structure of a Razorpay webhook event."""
    entity: str
    account_id: str
    event: str
    contains: List[str]
    payload: RazorpayPayload
    created_at: int
    
    @field_validator('entity')
    @classmethod
    def check_entity_type(cls, v: str) -> str:
        if v != 'event':
            raise ValueError("Webhook root entity must be 'event'")
        return v


def parse_razorpay_event(payload_body: bytes) -> RazorpayWebhookEvent:
    """
    Parses the raw JSON body into the RazorpayWebhookEvent schema.
    Raises MalformedInputError if JSON is invalid.
    Raises SchemaValidationError if it doesn't match the expected structure.
    """
    try:
        data = json.loads(payload_body)
    except json.JSONDecodeError as e:
        raise MalformedInputError(f"Invalid JSON payload: {str(e)}")
        
    try:
        return RazorpayWebhookEvent.model_validate(data)
    except ValidationError as e:
        raise SchemaValidationError(f"Payload schema validation failed: {str(e)}")
