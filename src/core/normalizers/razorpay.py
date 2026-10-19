from datetime import datetime, timezone
import uuid

from src.core.domain.models import PaymentEvent
from src.core.domain.exceptions import NormalizationError
from src.core.domain.payment_attempts import paypal_order_id
from src.core.normalizers.hashing import generate_deterministic_hash  # noqa: F401 (re-exported for existing callers)
from src.core.parsers.razorpay import RazorpayWebhookEvent


def normalize_razorpay_event(
    event: RazorpayWebhookEvent,
    source_event_id: str
) -> PaymentEvent:
    """
    Normalizes a Razorpay webhook event into the canonical PaymentEvent domain model.
    Validates timezone-aware timestamps and ensures monetary types are integers.
    """
    payment_entity = event.payload.payment.entity

    # Timestamp semantics: Razorpay sends epoch seconds in the webhook envelope event.created_at.
    # We convert to UTC datetime to preserve event-sourcing semantics for lifecycle events.
    try:
        timestamp_utc = datetime.fromtimestamp(event.created_at, tz=timezone.utc)
    except (ValueError, TypeError, OSError) as e:
        raise NormalizationError(f"Invalid timestamp in Razorpay payload: {str(e)}")

    if not isinstance(payment_entity.amount, int):
        raise NormalizationError(f"Amount must be an integer (minor units), got {type(payment_entity.amount)}")

    # Stage 5-8 records carry no provider column, so a Razorpay payment id inside the PayPal attempt
    # namespace would be read and written as that PayPal order's attempt by every downstream caller.
    if paypal_order_id(payment_entity.id) is not None:
        raise NormalizationError("Razorpay payment id uses the reserved PayPal attempt namespace")

    # We do NOT include customer_id per instructions unless Razorpay provides a clean one,
    # which we'll exclude for now to be safe. We only include what is semantically pure.

    return PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="razorpay",
        source_event_id=source_event_id,
        payment_id=payment_entity.id,
        order_id=payment_entity.order_id,
        timestamp=timestamp_utc,
        event_type=event.event,
        currency=payment_entity.currency.upper(),
        amount_minor_units=payment_entity.amount,
        payment_status=payment_entity.status,
        payment_method=payment_entity.method,
        bank=payment_entity.bank,
        wallet=payment_entity.wallet,
        error_code=payment_entity.error_code,
        error_description=payment_entity.error_description,
        error_source=payment_entity.error_source,
        error_step=payment_entity.error_step,
        error_reason=payment_entity.error_reason,
        ingested_at=datetime.now(timezone.utc)
    )
