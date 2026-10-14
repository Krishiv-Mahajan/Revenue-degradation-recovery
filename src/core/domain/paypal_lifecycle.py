"""
PayPal lifecycle contract for canonical PaymentEvents (pure functions, no I/O).

Identity:
  source_event_id  PayPal webhook event id; the webhook-deduplication identity only.
  payment_id       unchanged canonical meaning: order id for order events, capture id for
                   capture events, and the refunded capture's id for refunds.
  order_id         the PayPal lifecycle correlation key, only as provided by the payload.

Lifecycle classes (keyed by canonical event_type):
  order.approved    ORDER_CONSENT        buyer consent; non-terminal, never a capture
  order.completed   ORDER_INFORMATIONAL  non-terminal; never counted as a capture or GMV
  payment.pending   CAPTURE_PENDING      non-terminal
  payment.captured  CAPTURE_SUCCEEDED    terminal success
  payment.failed    CAPTURE_FAILED       terminal failure
  payment.refunded  REFUND               post-settlement adjustment; never terminal,
                                         never failed or recovered, never a sizing amount

Outcome semantics:
  - Outcomes are per capture. A later refund does not turn the original capture into a failed
    capture; refunds are separate post-settlement adjustments.
  - Captured GMV is gross: a successful capture counts in full whether or not it is refunded.
  - The order-level outcome when a decline is followed by a successful capture is undecided.
    It must be resolved by a future lifecycle-aware pipeline milestone; this module
    deliberately provides no order-level outcome resolver.

Unknown event types and non-PayPal events raise PayPalLifecycleError (fail closed).
Not wired into the Stage 2-8 pipeline; PayPal stays excluded by NON_RAZORPAY_PIPELINE_SOURCES.

The canonical schema stores no refund resource id (it is kept only in the raw payload), so
distinct refunds of one capture are distinguishable only by their webhook event id.
"""
from enum import Enum
from types import MappingProxyType
from typing import FrozenSet, Mapping, Optional

from src.core.domain.models import PaymentEvent
from src.core.domain.source_systems import PAYPAL_SOURCE_SYSTEM


class PayPalLifecycleError(ValueError):
    """The event cannot be classified under the PayPal lifecycle contract."""


class PayPalLifecycleClass(str, Enum):
    ORDER_CONSENT = "ORDER_CONSENT"
    ORDER_INFORMATIONAL = "ORDER_INFORMATIONAL"
    CAPTURE_PENDING = "CAPTURE_PENDING"
    CAPTURE_SUCCEEDED = "CAPTURE_SUCCEEDED"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    REFUND = "REFUND"


class PayPalCaptureOutcome(str, Enum):
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"


class PayPalOrderCorrelation(str, Enum):
    CORRELATABLE = "CORRELATABLE"
    NOT_CORRELATABLE = "NOT_CORRELATABLE"


PAYPAL_LIFECYCLE_CLASSES: Mapping[str, PayPalLifecycleClass] = MappingProxyType({
    "order.approved": PayPalLifecycleClass.ORDER_CONSENT,
    "order.completed": PayPalLifecycleClass.ORDER_INFORMATIONAL,
    "payment.pending": PayPalLifecycleClass.CAPTURE_PENDING,
    "payment.captured": PayPalLifecycleClass.CAPTURE_SUCCEEDED,
    "payment.failed": PayPalLifecycleClass.CAPTURE_FAILED,
    "payment.refunded": PayPalLifecycleClass.REFUND,
})

CAPTURE_CLASSES: FrozenSet[PayPalLifecycleClass] = frozenset({
    PayPalLifecycleClass.CAPTURE_PENDING,
    PayPalLifecycleClass.CAPTURE_SUCCEEDED,
    PayPalLifecycleClass.CAPTURE_FAILED,
})

TERMINAL_CAPTURE_OUTCOMES: Mapping[PayPalLifecycleClass, PayPalCaptureOutcome] = MappingProxyType({
    PayPalLifecycleClass.CAPTURE_SUCCEEDED: PayPalCaptureOutcome.CAPTURED,
    PayPalLifecycleClass.CAPTURE_FAILED: PayPalCaptureOutcome.FAILED,
})

# Candidate amount for possible future pre-outcome sizing. Being a sizing candidate does not
# mean the order was captured, successful or recovered.
PAYMENT_SIZING_CLASSES: FrozenSet[PayPalLifecycleClass] = frozenset({
    PayPalLifecycleClass.ORDER_CONSENT,
})

# Gross captured GMV: only successful captures move funds, and refunds are not netted out.
# order.completed duplicates the capture's money.
CAPTURED_GMV_CLASSES: FrozenSet[PayPalLifecycleClass] = frozenset({
    PayPalLifecycleClass.CAPTURE_SUCCEEDED,
})


def classify_paypal_event(event: PaymentEvent) -> PayPalLifecycleClass:
    if event.source_system != PAYPAL_SOURCE_SYSTEM:
        raise PayPalLifecycleError("Lifecycle classification applies to PayPal events only.")
    lifecycle_class = PAYPAL_LIFECYCLE_CLASSES.get(event.event_type)
    if lifecycle_class is None:
        raise PayPalLifecycleError(f"Unknown PayPal canonical event type: {event.event_type}")
    return lifecycle_class


def is_terminal_capture(event: PaymentEvent) -> bool:
    return classify_paypal_event(event) in TERMINAL_CAPTURE_OUTCOMES


def capture_outcome(event: PaymentEvent) -> Optional[PayPalCaptureOutcome]:
    """CAPTURED or FAILED for terminal capture events; None for everything else."""
    return TERMINAL_CAPTURE_OUTCOMES.get(classify_paypal_event(event))


def is_payment_sizing_event(event: PaymentEvent) -> bool:
    return classify_paypal_event(event) in PAYMENT_SIZING_CLASSES


def counts_as_captured_gmv(event: PaymentEvent) -> bool:
    return classify_paypal_event(event) in CAPTURED_GMV_CLASSES


def capture_id(event: PaymentEvent) -> Optional[str]:
    """The capture's own id, for capture events only. A refund's payment_id is its parent capture."""
    if classify_paypal_event(event) in CAPTURE_CLASSES:
        return event.payment_id
    return None


def refunded_capture_id(event: PaymentEvent) -> Optional[str]:
    """The capture a refund adjusts; None for non-refund events."""
    if classify_paypal_event(event) is PayPalLifecycleClass.REFUND:
        return event.payment_id
    return None


def order_correlation_key(event: PaymentEvent) -> Optional[str]:
    """The PayPal order id carried by the event, never inferred from other identifiers."""
    classify_paypal_event(event)
    return event.order_id or None


def order_correlation(event: PaymentEvent) -> PayPalOrderCorrelation:
    if order_correlation_key(event) is None:
        return PayPalOrderCorrelation.NOT_CORRELATABLE
    return PayPalOrderCorrelation.CORRELATABLE
