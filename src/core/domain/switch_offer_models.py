"""
Stage 7 — Customer-approved payment method switch offers (PROMPT_PAYMENT_METHOD_SWITCH).

When Stage 6 selects PROMPT_PAYMENT_METHOD_SWITCH and the real PayPal sandbox executor creates a checkout
order, the customer is OFFERED that checkout as an alternative payment method. The offer is never acted on
without the customer:
- acceptance is PayPal's verified CHECKOUT.ORDER.APPROVED for the offer's order (the customer approved the
  payment at PayPal); nothing else counts as consent;
- a decline is the customer's explicit refusal, reported by the merchant channel that presented the offer;
- this engine never captures, charges or switches anything itself; capturing an approved order is the
  merchant's step, and only PayPal's verified capture of the offer's order completes it.

Lifecycle (terminal states are final; later events are recorded but never applied):

    OFFERED --approval before expiry--> ACCEPTED --verified capture, same amount--> COMPLETED
       |                                    |--capture declined--> PAYMENT_FAILED
       |--explicit decline--> DECLINED      '--no completion by the deadline--> ABANDONED
       '--no response by expires_at--> EXPIRED

The four customer outcomes (accepted, declined, expired, abandoned) are distinct statuses, kept apart from
the payment outcome (COMPLETED, PAYMENT_FAILED).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Tuple

NAMESPACE_SWITCH_OFFER: uuid.UUID = uuid.UUID("5e1f0c3a-7d2b-4c86-9a41-0b6d2f8e7c15")

# The customer has this long after the offer is created to respond; afterwards it is EXPIRED.
OFFER_TTL = timedelta(minutes=30)
# After accepting, the payment must complete within this window; afterwards the offer is ABANDONED.
COMPLETION_WINDOW = timedelta(hours=1)

ALTERNATIVE_PROVIDER = "paypal"
OFFER_ENVIRONMENT = "sandbox"


class OfferStatus(str, Enum):
    OFFERED = "OFFERED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    ABANDONED = "ABANDONED"
    COMPLETED = "COMPLETED"
    PAYMENT_FAILED = "PAYMENT_FAILED"


TERMINAL_STATUSES = frozenset({
    OfferStatus.DECLINED, OfferStatus.EXPIRED, OfferStatus.ABANDONED, OfferStatus.COMPLETED, OfferStatus.PAYMENT_FAILED,
})


class OfferSignal(str, Enum):
    """What happened: a customer action, a verified PayPal payment outcome, or the passage of time."""
    APPROVAL = "APPROVAL"
    DECLINE = "DECLINE"
    CAPTURE_COMPLETED = "CAPTURE_COMPLETED"
    CAPTURE_DECLINED = "CAPTURE_DECLINED"
    CLOCK = "CLOCK"


class OfferEventType(str, Enum):
    OFFER_CREATED = "OFFER_CREATED"
    CUSTOMER_ACCEPTED = "CUSTOMER_ACCEPTED"
    CUSTOMER_DECLINED = "CUSTOMER_DECLINED"
    OFFER_EXPIRED = "OFFER_EXPIRED"
    OFFER_ABANDONED = "OFFER_ABANDONED"
    PAYMENT_COMPLETED = "PAYMENT_COMPLETED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    # A capture outcome that arrived before PayPal's approval webhook; re-applied once acceptance is recorded.
    PAYMENT_OUTCOME_HELD = "PAYMENT_OUTCOME_HELD"
    EVENT_IGNORED = "EVENT_IGNORED"


class OfferEventSource(str, Enum):
    EXECUTOR = "EXECUTOR"
    PAYPAL_WEBHOOK = "PAYPAL_WEBHOOK"
    MERCHANT_CHANNEL = "MERCHANT_CHANNEL"
    EXPIRY_SWEEP = "EXPIRY_SWEEP"


def make_offer_id(command_id: uuid.UUID) -> uuid.UUID:
    """One offer per command, so re-executions and replays always address the same offer."""
    return uuid.uuid5(NAMESPACE_SWITCH_OFFER, str(command_id))


@dataclass(frozen=True)
class OfferState:
    status: OfferStatus
    expires_at: datetime
    amount_minor_units: int
    currency: str
    accepted_at: Optional[datetime] = None
    completion_deadline: Optional[datetime] = None


@dataclass(frozen=True)
class OfferStep:
    """One audit entry produced by a transition evaluation."""
    event_type: OfferEventType
    from_status: OfferStatus
    to_status: OfferStatus
    reason: str


@dataclass(frozen=True)
class OfferTransition:
    state: OfferState
    steps: Tuple[OfferStep, ...]

    @property
    def changed(self) -> bool:
        return any(step.from_status != step.to_status for step in self.steps)


def evaluate_offer_signal(
    state: OfferState,
    signal: OfferSignal,
    occurred_at: datetime,
    amount_minor_units: Optional[int] = None,
    currency: Optional[str] = None,
) -> OfferTransition:
    """
    Deterministically applies one signal at occurred_at. Deadlines are applied first, so an approval,
    decline or capture that happens after the offer expired (or after the completion deadline) can never
    revive it. Terminal offers record the signal as ignored and never change.
    """
    steps = []
    current = state

    def move(event_type: OfferEventType, target: OfferStatus, reason: str, **changes) -> None:
        nonlocal current
        steps.append(OfferStep(event_type, current.status, target, reason))
        current = replace(current, status=target, **changes)

    if current.status is OfferStatus.OFFERED and occurred_at >= current.expires_at:
        move(OfferEventType.OFFER_EXPIRED, OfferStatus.EXPIRED, "no customer response before the offer expired")
    elif (
        current.status is OfferStatus.ACCEPTED
        and current.completion_deadline is not None
        and occurred_at >= current.completion_deadline
    ):
        move(OfferEventType.OFFER_ABANDONED, OfferStatus.ABANDONED, "accepted, but the payment did not complete in time")

    if signal is OfferSignal.CLOCK:
        return OfferTransition(current, tuple(steps))

    def ignore(reason: str) -> OfferTransition:
        steps.append(OfferStep(OfferEventType.EVENT_IGNORED, current.status, current.status, f"{signal.value}: {reason}"))
        return OfferTransition(current, tuple(steps))

    if current.status in TERMINAL_STATUSES:
        return ignore(f"the offer is already {current.status.value}")

    if current.status is OfferStatus.OFFERED:
        if signal is OfferSignal.APPROVAL:
            move(
                OfferEventType.CUSTOMER_ACCEPTED, OfferStatus.ACCEPTED, "the customer approved the checkout at PayPal",
                accepted_at=occurred_at, completion_deadline=occurred_at + COMPLETION_WINDOW,
            )
            return OfferTransition(current, tuple(steps))
        if signal is OfferSignal.DECLINE:
            move(OfferEventType.CUSTOMER_DECLINED, OfferStatus.DECLINED, "the customer declined the offer")
            return OfferTransition(current, tuple(steps))
        steps.append(OfferStep(
            OfferEventType.PAYMENT_OUTCOME_HELD, current.status, current.status,
            f"{signal.value}: held until the customer's acceptance is recorded",
        ))
        return OfferTransition(current, tuple(steps))

    # ACCEPTED
    if signal is OfferSignal.CAPTURE_COMPLETED:
        if (amount_minor_units, currency) != (current.amount_minor_units, current.currency):
            return ignore(
                f"captured {amount_minor_units} {currency} does not match the offered "
                f"{current.amount_minor_units} {current.currency}"
            )
        move(OfferEventType.PAYMENT_COMPLETED, OfferStatus.COMPLETED, "PayPal verified the capture of the offered amount")
        return OfferTransition(current, tuple(steps))
    if signal is OfferSignal.CAPTURE_DECLINED:
        move(OfferEventType.PAYMENT_FAILED, OfferStatus.PAYMENT_FAILED, "PayPal declined the capture")
        return OfferTransition(current, tuple(steps))
    if signal is OfferSignal.DECLINE:
        return ignore("the customer already accepted at PayPal")
    return ignore("the customer already accepted")


def offer_presentation(amount_minor_units: int, currency: str, diagnosed_method: Optional[str]) -> dict:
    """The customer-facing terms of the offer. The merchant channel must present them unchanged."""
    method = diagnosed_method or "your payment method"
    return {
        "headline": "Pay with PayPal instead",
        "message": (
            f"Payments with {method} are currently failing. You can complete this payment with PayPal instead."
        ),
        "amount_minor_units": amount_minor_units,
        "currency": currency,
        "requires_customer_action": True,
        "terms": [
            "Nothing is charged unless you approve the payment at PayPal.",
            "Your original payment method is not changed and is not charged by this offer.",
            "The offer expires if you do not respond in time.",
        ],
    }
