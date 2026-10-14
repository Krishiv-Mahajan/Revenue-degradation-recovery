"""
Normalizes parsed PayPal webhook events into the canonical PaymentEvent.

Event mapping (pipeline-relevant event types are payment.captured and payment.failed,
the only terminal outcomes, and payment.authorized, the prediction trigger):

  PayPal event               event_type         payment_status  payment_id         order_id
  PAYMENT.CAPTURE.COMPLETED  payment.captured   captured        capture id         related order id
  PAYMENT.CAPTURE.DECLINED   payment.failed     failed          capture id         related order id
  PAYMENT.CAPTURE.PENDING    payment.pending    pending         capture id         related order id
  PAYMENT.CAPTURE.REFUNDED   payment.refunded   refunded        refunded capture   related order id
  CHECKOUT.ORDER.APPROVED    order.approved     approved        order id           order id
  CHECKOUT.ORDER.COMPLETED   order.completed    completed       order id           order id

Order events never map to payment.authorized or payment.captured: approval is buyer
consent, not captured funds, and the capture event already records the payment outcome.

payment_id is the capture id, mirroring Razorpay where payment_id is one payment attempt
and order_id groups the attempts for an order. A declined capture followed by a successful
retry on the same order therefore stays two attempts, each with one terminal outcome.

PAYMENT.CAPTURE.DENIED is not supported: PayPal documents it for Payments v1, whose
resources this v2 parser does not accept, while v2 captures report PAYMENT.CAPTURE.DECLINED.

Money is converted from PayPal decimal strings to integer minor units with exact decimal
arithmetic, using PayPal's own decimal rules per currency.
"""
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, Inexact, Rounded, localcontext
from types import MappingProxyType
from typing import Mapping

from src.core.domain.exceptions import NormalizationError
from src.core.domain.models import PaymentEvent
from src.core.parsers.paypal import (
    PayPalCaptureResource,
    PayPalMoney,
    PayPalOrderResource,
    PayPalWebhookEvent,
)

SOURCE_SYSTEM = "paypal"

# Minor-unit exponents for PayPal REST currencies. PayPal rejects decimals for HUF, JPY and
# TWD, so they are 0 here even though ISO 4217 lists HUF and TWD with 2.
PAYPAL_CURRENCY_EXPONENTS: Mapping[str, int] = MappingProxyType({
    "AUD": 2, "BRL": 2, "CAD": 2, "CHF": 2, "CNY": 2, "CZK": 2, "DKK": 2, "EUR": 2,
    "GBP": 2, "HKD": 2, "ILS": 2, "MXN": 2, "MYR": 2, "NOK": 2, "NZD": 2, "PHP": 2,
    "PLN": 2, "RUB": 2, "SEK": 2, "SGD": 2, "THB": 2, "USD": 2,
    "HUF": 0, "JPY": 0, "TWD": 0,
})

# payment_events.amount_minor_units is a 32-bit INTEGER column.
MAX_AMOUNT_MINOR_UNITS = 2_147_483_647

_MAX_AMOUNT_LENGTH = 32
_AMOUNT_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.([0-9]+))?$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"
)


@dataclass(frozen=True)
class _CanonicalEvent:
    event_type: str
    payment_status: str


PAYPAL_EVENT_MAPPING: Mapping[str, _CanonicalEvent] = MappingProxyType({
    "PAYMENT.CAPTURE.COMPLETED": _CanonicalEvent("payment.captured", "captured"),
    "PAYMENT.CAPTURE.DECLINED": _CanonicalEvent("payment.failed", "failed"),
    "PAYMENT.CAPTURE.PENDING": _CanonicalEvent("payment.pending", "pending"),
    "PAYMENT.CAPTURE.REFUNDED": _CanonicalEvent("payment.refunded", "refunded"),
    "CHECKOUT.ORDER.APPROVED": _CanonicalEvent("order.approved", "approved"),
    "CHECKOUT.ORDER.COMPLETED": _CanonicalEvent("order.completed", "completed"),
})


def to_minor_units(
    value: str,
    currency_code: str,
    exponents: Mapping[str, int] = PAYPAL_CURRENCY_EXPONENTS,
) -> int:
    """
    Converts a PayPal decimal amount string to positive integer minor units.

    Rejects non-string or malformed amounts, unsupported currencies, more fractional
    digits than the currency allows, zero, and values beyond MAX_AMOUNT_MINOR_UNITS.
    """
    if not isinstance(currency_code, str) or not _CURRENCY_PATTERN.match(currency_code):
        raise NormalizationError("Currency code must be three uppercase letters.")
    if currency_code not in exponents:
        raise NormalizationError(f"Unsupported PayPal currency: {currency_code}")
    exponent = exponents[currency_code]

    if not isinstance(value, str) or len(value) > _MAX_AMOUNT_LENGTH:
        raise NormalizationError("Amount must be a decimal string of at most 32 characters.")
    match = _AMOUNT_PATTERN.match(value)
    if not match:
        raise NormalizationError("Amount is not a well-formed non-negative decimal string.")
    fraction = match.group(3) or ""
    if len(fraction) > exponent:
        raise NormalizationError(
            f"Amount has more fractional digits than {currency_code} allows ({exponent})."
        )

    with localcontext() as ctx:
        ctx.prec = 64
        ctx.traps[Inexact] = True
        ctx.traps[Rounded] = True
        minor_units = int(Decimal(value).scaleb(exponent))

    if minor_units <= 0:
        raise NormalizationError("Amount must be greater than zero.")
    if minor_units > MAX_AMOUNT_MINOR_UNITS:
        raise NormalizationError("Amount exceeds the maximum supported minor-unit value.")
    return minor_units


def parse_paypal_timestamp(value: str) -> datetime:
    """Parses an RFC 3339 timestamp with an explicit offset and returns it in UTC."""
    if not isinstance(value, str) or not _RFC3339_PATTERN.match(value):
        raise NormalizationError("Timestamp must be RFC 3339 with an explicit UTC offset.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise NormalizationError("Timestamp is not a valid calendar date and time.") from None
    return parsed.astimezone(timezone.utc)


def _event_amount(event: PayPalWebhookEvent) -> PayPalMoney:
    resource = event.resource
    if isinstance(resource, PayPalOrderResource):
        if len(resource.purchase_units) != 1:
            raise NormalizationError(
                "PayPal orders with multiple purchase units are not supported."
            )
        return resource.purchase_units[0].amount
    return resource.amount


def normalize_paypal_event(event: PayPalWebhookEvent) -> PaymentEvent:
    """
    Normalizes a parsed PayPal webhook event into a PaymentEvent.
    The PayPal webhook event ID is preserved as source_event_id.
    """
    canonical = PAYPAL_EVENT_MAPPING.get(event.event_type)
    if canonical is None:
        raise NormalizationError(f"Unsupported PayPal event type: {event.event_type}")

    if isinstance(event.resource, PayPalOrderResource):
        payment_id = event.order_id
    else:
        payment_id = event.capture_id
    if not payment_id:
        raise NormalizationError("PayPal event has no payment identifier.")

    amount = _event_amount(event)
    error_reason = None
    if (
        canonical.event_type == "payment.failed"
        and isinstance(event.resource, PayPalCaptureResource)
        and event.resource.status_details is not None
    ):
        error_reason = event.resource.status_details.reason

    return PaymentEvent(
        event_id=uuid.uuid4(),
        source_system=SOURCE_SYSTEM,
        source_event_id=event.event_id,
        payment_id=payment_id,
        order_id=event.order_id,
        timestamp=parse_paypal_timestamp(event.create_time),
        event_type=canonical.event_type,
        currency=amount.currency_code,
        amount_minor_units=to_minor_units(amount.value, amount.currency_code),
        payment_status=canonical.payment_status,
        error_reason=error_reason,
        ingested_at=datetime.now(timezone.utc),
    )
