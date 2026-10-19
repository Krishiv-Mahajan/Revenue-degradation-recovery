"""
Payment attempt identity for pipeline Stages 5-8.

Razorpay (and the synthetic demo source): one attempt per payment. The attempt id is the
payment_id; payment.authorized triggers prediction and payment.captured / payment.failed with the
same payment_id resolve it. This is the original pipeline contract and is unchanged.

PayPal: one attempt per checkout order. CHECKOUT.ORDER.APPROVED (canonical order.approved, whose
payment_id is the order id) triggers prediction; capture outcomes (canonical payment.captured /
payment.failed, whose payment_id is the capture id) resolve it through their order_id. Pending
captures, refunds and order.completed never start or resolve an attempt.

Provider identity. PayPal attempt ids carry PAYPAL_ATTEMPT_PREFIX, and every query for a PayPal
attempt also requires source_system == paypal, so an overlapping payment_id or order_id cannot join
one provider's attempt to the other provider's events. The prefix is enforced where attempts are
created: resolve_pipeline_attempt gives no Stage 5-8 role to a non-PayPal event whose payment_id lies
in the PayPal namespace, so such an event can never act as, or resolve, a PayPal attempt. Razorpay
webhook normalization rejects payment ids in the namespace, so only rows written directly to the
database (scripts, synthetic data) can carry one.
Limitation: Stage 5-8 rows store only payment_attempt_id, not the provider. Code that reads payment ids
from the database and calls the Stage 5-8 services directly (demo orchestrator, model training) treats
a prefixed id as a PayPal attempt. Recording the provider explicitly needs a schema change (a provider
column on predictions, decisions, commands, execution attempts, observations and attributions).

Order-level outcome. The first PayPal capture outcome ingested for the order decides the attempt's
outcome. A later, contradictory outcome (for example a declined capture followed by a successful
retry) is recorded as a conflict in the observation's audit payload and never flips the outcome, so
a decline followed by a successful retry is never attributed as protected GMV.

Stages 2-4 remain Razorpay-only (see source_systems.py).
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.core.domain.models import PaymentEvent
from src.core.domain.source_systems import PAYPAL_SOURCE_SYSTEM

PAYPAL_ATTEMPT_PREFIX = "paypal-order:"
PIPELINE_TRIGGER_EVENT_TYPE = "payment.authorized"
PAYPAL_TRIGGER_EVENT_TYPE = "order.approved"
TERMINAL_EVENT_TYPES = ("payment.captured", "payment.failed")


def paypal_attempt_id(order_id: str) -> str:
    return f"{PAYPAL_ATTEMPT_PREFIX}{order_id}"


def paypal_order_id(payment_attempt_id: Optional[str]) -> Optional[str]:
    """The PayPal order id behind a PayPal attempt id; None for every other attempt id."""
    if isinstance(payment_attempt_id, str) and payment_attempt_id.startswith(PAYPAL_ATTEMPT_PREFIX):
        return payment_attempt_id[len(PAYPAL_ATTEMPT_PREFIX):] or None
    return None


class PipelineRole(str, Enum):
    TRIGGER = "TRIGGER"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True)
class PipelineAttempt:
    role: PipelineRole
    payment_attempt_id: str


def in_paypal_attempt_namespace(event: PaymentEvent) -> bool:
    """True for a non-PayPal event whose payment_id would be read as a PayPal attempt id."""
    return event.source_system != PAYPAL_SOURCE_SYSTEM and paypal_order_id(event.payment_id) is not None


def resolve_pipeline_attempt(event: PaymentEvent) -> Optional[PipelineAttempt]:
    """The Stage 5-8 role of a canonical event and the attempt it belongs to; None when it has none."""
    if event.source_system == PAYPAL_SOURCE_SYSTEM:
        if event.event_type == PAYPAL_TRIGGER_EVENT_TYPE and event.payment_id:
            return PipelineAttempt(PipelineRole.TRIGGER, paypal_attempt_id(event.payment_id))
        if event.event_type in TERMINAL_EVENT_TYPES and event.order_id:
            return PipelineAttempt(PipelineRole.TERMINAL, paypal_attempt_id(event.order_id))
        return None
    if in_paypal_attempt_namespace(event):
        return None
    if event.event_type == PIPELINE_TRIGGER_EVENT_TYPE:
        return PipelineAttempt(PipelineRole.TRIGGER, event.payment_id)
    if event.event_type in TERMINAL_EVENT_TYPES:
        return PipelineAttempt(PipelineRole.TERMINAL, event.payment_id)
    return None
