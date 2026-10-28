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
Registration. Every attempt is registered once, from its trigger event, in the payment_attempts
registry under one provider (AttemptRef). Every Stage 5-8 write requires the registered attempt and
stores its provider (source_system); a key registered under one provider is never merged with another
provider's events. Only razorpay, synthetic and paypal (PIPELINE_PROVIDERS) can own an attempt; events
from any other source have no Stage 5-8 role.
Limitation: Stage 5-8 reads still route by the attempt key (prefix for PayPal, NOT IN paypal otherwise),
so a synthetic event sharing a Razorpay attempt's payment_id can still be read for that attempt.

Order-level outcome. The first PayPal capture outcome ingested for the order decides the attempt's
outcome. A later, contradictory outcome (for example a declined capture followed by a successful
retry) is recorded as a conflict in the observation's audit payload and never flips the outcome, so
a decline followed by a successful retry is never attributed as protected GMV.

Stages 2-4 remain Razorpay-only (see source_systems.py).
"""
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from src.core.domain.models import PaymentEvent
from src.core.domain.payment_attempt_exceptions import (
    InvalidAttemptIdentityError,
    PaymentAttemptError,
    ProviderIdentityMissingError,
    UnsupportedProviderError,
)
from src.core.domain.source_systems import PAYPAL_SOURCE_SYSTEM, PIPELINE_PROVIDERS

PAYPAL_ATTEMPT_PREFIX = "paypal-order:"
PIPELINE_TRIGGER_EVENT_TYPE = "payment.authorized"
PAYPAL_TRIGGER_EVENT_TYPE = "order.approved"
TERMINAL_EVENT_TYPES = ("payment.captured", "payment.failed")

# Must match the Identifier grammar in src/core/parsers/paypal.py and the registry CHECK constraint
# (enforced by a test). Used with re.fullmatch, so a trailing newline never matches.
PAYPAL_IDENTIFIER_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"


def paypal_attempt_id(order_id: str) -> str:
    return f"{PAYPAL_ATTEMPT_PREFIX}{order_id}"


def paypal_order_id(payment_attempt_id: Optional[str]) -> Optional[str]:
    """The PayPal order id behind a PayPal attempt id; None for every other attempt id."""
    if isinstance(payment_attempt_id, str) and payment_attempt_id.startswith(PAYPAL_ATTEMPT_PREFIX):
        return payment_attempt_id[len(PAYPAL_ATTEMPT_PREFIX):] or None
    return None


@dataclass(frozen=True)
class AttemptRef:
    """
    The identity of a Stage 5-8 payment attempt: its provider, attempt key and provider reference
    (Razorpay/synthetic payment_id, or PayPal order id). Construction enforces the same rules as the
    payment_attempts registry CHECK constraints.
    """
    source_system: str
    payment_attempt_id: str
    provider_reference: str

    def __post_init__(self) -> None:
        if self.source_system not in PIPELINE_PROVIDERS:
            raise UnsupportedProviderError(
                f"source_system={self.source_system!r} cannot own a Stage 5-8 payment attempt"
            )
        if self.is_paypal:
            valid = (
                re.fullmatch(PAYPAL_IDENTIFIER_PATTERN, self.provider_reference) is not None
                and self.payment_attempt_id == paypal_attempt_id(self.provider_reference)
            )
        else:
            valid = (
                self.provider_reference != ""
                and self.payment_attempt_id == self.provider_reference
                and not self.payment_attempt_id.startswith(PAYPAL_ATTEMPT_PREFIX)
            )
        if not valid:
            raise InvalidAttemptIdentityError(
                f"invalid {self.source_system} attempt identity: payment_attempt_id={self.payment_attempt_id!r} "
                f"provider_reference={self.provider_reference!r}"
            )

    @property
    def is_paypal(self) -> bool:
        return self.source_system == PAYPAL_SOURCE_SYSTEM

    @classmethod
    def for_key(cls, source_system: str, payment_attempt_id: str) -> "AttemptRef":
        """The attempt a stored key belongs to, given the provider recorded with it."""
        if source_system == PAYPAL_SOURCE_SYSTEM:
            reference = paypal_order_id(payment_attempt_id) or ""
        else:
            reference = payment_attempt_id
        return cls(source_system, payment_attempt_id, reference)

    @classmethod
    def for_trigger_event(cls, event) -> "AttemptRef":
        """The attempt a trigger event starts: PayPal order.approved, or another provider's payment.authorized."""
        expected = (
            PAYPAL_TRIGGER_EVENT_TYPE if event.source_system == PAYPAL_SOURCE_SYSTEM else PIPELINE_TRIGGER_EVENT_TYPE
        )
        if event.event_type != expected:
            raise InvalidAttemptIdentityError(
                f"{event.source_system} event_type={event.event_type!r} is not an attempt trigger"
            )
        if event.source_system == PAYPAL_SOURCE_SYSTEM:
            return cls(PAYPAL_SOURCE_SYSTEM, paypal_attempt_id(event.payment_id), event.payment_id)
        return cls(event.source_system, event.payment_id, event.payment_id)


@dataclass(frozen=True)
class RegisteredAttempt:
    """An attempt as recorded in the payment_attempts registry; only the registry repository creates one."""
    ref: AttemptRef
    trigger_event_id: uuid.UUID
    registered_at: datetime


def recorded_provider(source_system: Optional[str], record: str) -> str:
    """The provider stored on a Stage 5-8 record; a missing one is never guessed from the key."""
    if source_system is None:
        raise ProviderIdentityMissingError(f"{record} has no source_system")
    return source_system


class PipelineRole(str, Enum):
    TRIGGER = "TRIGGER"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True)
class PipelineAttempt:
    role: PipelineRole
    ref: AttemptRef

    @property
    def payment_attempt_id(self) -> str:
        return self.ref.payment_attempt_id


def in_paypal_attempt_namespace(event: PaymentEvent) -> bool:
    """True for a non-PayPal event whose payment_id would be read as a PayPal attempt id."""
    return event.source_system != PAYPAL_SOURCE_SYSTEM and paypal_order_id(event.payment_id) is not None


def _candidate_role(event: PaymentEvent) -> Optional[Tuple[PipelineRole, str]]:
    """The role an event's type gives it and the attempt key it names, before identity validation."""
    if event.source_system == PAYPAL_SOURCE_SYSTEM:
        if event.event_type == PAYPAL_TRIGGER_EVENT_TYPE and event.payment_id:
            return PipelineRole.TRIGGER, paypal_attempt_id(event.payment_id)
        if event.event_type in TERMINAL_EVENT_TYPES and event.order_id:
            return PipelineRole.TERMINAL, paypal_attempt_id(event.order_id)
        return None
    if event.event_type == PIPELINE_TRIGGER_EVENT_TYPE:
        return PipelineRole.TRIGGER, event.payment_id
    if event.event_type in TERMINAL_EVENT_TYPES:
        return PipelineRole.TERMINAL, event.payment_id
    return None


def pipeline_attempt_refusal(event: PaymentEvent) -> Optional[str]:
    """Why an event that would otherwise start or resolve an attempt is refused; None if it is not."""
    if in_paypal_attempt_namespace(event):
        return "payment_id uses the PayPal attempt namespace"
    candidate = _candidate_role(event)
    if candidate is None:
        return None
    try:
        AttemptRef.for_key(event.source_system, candidate[1])
    except PaymentAttemptError as exc:
        return str(exc)
    return None


def resolve_pipeline_attempt(event: PaymentEvent) -> Optional[PipelineAttempt]:
    """
    The Stage 5-8 role of a canonical event and the attempt it belongs to; None when it has none,
    including events refused by pipeline_attempt_refusal. Never raises.
    """
    if pipeline_attempt_refusal(event) is not None:
        return None
    candidate = _candidate_role(event)
    if candidate is None:
        return None
    role, key = candidate
    return PipelineAttempt(role, AttemptRef.for_key(event.source_system, key))
