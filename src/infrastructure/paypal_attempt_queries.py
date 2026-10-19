"""
payment_events predicates for PayPal pipeline attempts (see src/core/domain/payment_attempts.py).
Every predicate requires source_system == paypal, so PayPal attempts never read Razorpay rows.
"""
from src.core.domain.payment_attempts import PAYPAL_TRIGGER_EVENT_TYPE, TERMINAL_EVENT_TYPES
from src.core.domain.source_systems import PAYPAL_SOURCE_SYSTEM
from src.infrastructure.models import PaymentEventModel


def paypal_trigger_events(order_id: str) -> tuple:
    """The order.approved events of one PayPal order; for order events payment_id is the order id."""
    return (
        PaymentEventModel.source_system == PAYPAL_SOURCE_SYSTEM,
        PaymentEventModel.event_type == PAYPAL_TRIGGER_EVENT_TYPE,
        PaymentEventModel.payment_id == order_id,
    )


def paypal_terminal_events(order_id: str) -> tuple:
    """Capture outcomes of one PayPal order, correlated by order_id; their payment_id is the capture id."""
    return (
        PaymentEventModel.source_system == PAYPAL_SOURCE_SYSTEM,
        PaymentEventModel.event_type.in_(TERMINAL_EVENT_TYPES),
        PaymentEventModel.order_id == order_id,
    )
