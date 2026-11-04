"""
Stage 7 — Payment method switch offer lifecycle (see src/core/domain/switch_offer_models.py).

- open_offer: after a real sandbox execution of PROMPT_PAYMENT_METHOD_SWITCH, records the PayPal checkout
  as an offer to the customer. Idempotent: one offer per command, whatever the number of replays.
- apply_paypal_event: applies PayPal's verified events for the offer's checkout order (approval = customer
  acceptance; capture completed / declined = payment outcome). Replays are no-ops.
- record_decline: the customer's explicit decline, reported by the merchant channel.
- expire_due: moves offers past their deadlines to EXPIRED or ABANDONED.

Every change is evaluated by the pure lifecycle rules under a row lock and recorded as offer events.
Nothing here charges, captures or contacts PayPal.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.intervention_models import InterventionRouteKey
from src.core.domain.switch_offer_models import (
    ALTERNATIVE_PROVIDER,
    OFFER_ENVIRONMENT,
    OFFER_TTL,
    TERMINAL_STATUSES,
    OfferEventSource,
    OfferEventType,
    OfferSignal,
    OfferState,
    OfferStatus,
    OfferTransition,
    evaluate_offer_signal,
    make_offer_id,
    offer_presentation,
)
from src.infrastructure.models import (
    InterventionCommandModel,
    InterventionDecisionModel,
    PaymentEventModel,
    PaymentMethodSwitchOfferModel,
)
from src.infrastructure.switch_offer_repository import SwitchOfferRepository

logger = logging.getLogger(__name__)

SWITCH_ROUTE = InterventionRouteKey.PROMPT_PAYMENT_METHOD_SWITCH.value
_SIGNALS = {
    "order.approved": OfferSignal.APPROVAL,
    "payment.captured": OfferSignal.CAPTURE_COMPLETED,
    "payment.failed": OfferSignal.CAPTURE_DECLINED,
}


@dataclass(frozen=True)
class DeclineResult:
    offer: Optional[PaymentMethodSwitchOfferModel]
    applied: bool


@dataclass(frozen=True)
class PayPalEventResult:
    offer: Optional[PaymentMethodSwitchOfferModel]
    # Whether this event completed the offer (directly, or by releasing a capture held before acceptance).
    completed_now: bool = False


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _state(offer: PaymentMethodSwitchOfferModel) -> OfferState:
    return OfferState(
        status=OfferStatus(offer.status),
        expires_at=_utc(offer.expires_at),
        amount_minor_units=offer.amount_minor_units,
        currency=offer.currency,
        accepted_at=_utc(offer.accepted_at) if offer.accepted_at else None,
        completion_deadline=_utc(offer.completion_deadline) if offer.completion_deadline else None,
    )


class SwitchOfferService:
    def __init__(
        self,
        session: AsyncSession,
        repository: Optional[SwitchOfferRepository] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.session = session
        self.repo = repository or SwitchOfferRepository(session)
        self.clock = clock

    async def open_offer(self, command_id: uuid.UUID) -> Optional[PaymentMethodSwitchOfferModel]:
        """
        The offer for a succeeded PROMPT_PAYMENT_METHOD_SWITCH command whose real sandbox execution created a
        PayPal checkout, creating it on first call. None when there is no such execution (for example a
        simulated one): a simulation never produces an offer.
        """
        command = await self.session.get(InterventionCommandModel, command_id)
        if command is None or command.route_key != SWITCH_ROUTE or command.command_status != "SUCCEEDED":
            return None
        existing = await self.repo.get_offer_by_command(command_id)
        if existing is not None:
            return existing

        attempt = await self.repo.latest_real_successful_attempt(command_id)
        if attempt is None:
            return None
        payload = attempt.provider_response_payload or {}
        order_id, approve_url = payload.get("paypal_order_id"), payload.get("approve_url")
        if payload.get("environment") != OFFER_ENVIRONMENT or not order_id or not approve_url:
            logger.warning(f"[SwitchOffer] command {command_id} has no usable sandbox checkout; no offer created")
            return None

        decision = await self.session.get(InterventionDecisionModel, command.decision_id)
        if (
            decision is None
            or decision.decision_type != "ACT"
            or decision.gate_verdict != "PASSED"
            or decision.selected_route_id != SWITCH_ROUTE
        ):
            logger.warning(f"[SwitchOffer] command {command_id} is not backed by an eligible switch decision")
            return None
        audit = decision.evaluation_audit_payload or {}
        facts, upstream = audit.get("payment_facts", {}), audit.get("upstream_context", {})
        amount, currency = facts.get("amount_minor_units"), facts.get("currency")

        offered_at = _utc(attempt.completed_at)
        offer_id = make_offer_id(command_id)
        now = self.clock()
        inserted = await self.repo.insert_offer_if_absent({
            "offer_id": offer_id,
            "command_id": command_id,
            "decision_id": decision.decision_id,
            "execution_attempt_id": attempt.attempt_id,
            "payment_attempt_id": command.payment_attempt_id,
            "source_system": command.source_system,
            "episode_id": uuid.UUID(upstream["episode_id"]) if upstream.get("episode_id") else None,
            "diagnosis_dimension": upstream.get("rca_candidate_dimension"),
            "diagnosis_value": upstream.get("rca_candidate_value"),
            "diagnosis_strength": upstream.get("rca_evidence_strength"),
            "alternative_provider": ALTERNATIVE_PROVIDER,
            "environment": OFFER_ENVIRONMENT,
            "checkout_order_id": order_id,
            "approve_url": approve_url,
            "amount_minor_units": amount,
            "currency": currency,
            "status": OfferStatus.OFFERED.value,
            "offered_at": offered_at,
            "expires_at": offered_at + OFFER_TTL,
            "presentation": offer_presentation(amount, currency, upstream.get("rca_candidate_value")),
            "created_at": now,
            "updated_at": now,
        })
        if inserted:
            await self.repo.append_event(
                offer_id,
                event_type=OfferEventType.OFFER_CREATED.value,
                source=OfferEventSource.EXECUTOR.value,
                from_status=None,
                to_status=OfferStatus.OFFERED.value,
                occurred_at=offered_at,
                recorded_at=now,
                payment_event_id=None,
                reason="PayPal sandbox checkout offered as an alternative payment method",
                details={
                    "execution_attempt_id": str(attempt.attempt_id),
                    "checkout_order_id": order_id,
                    "expires_at": (offered_at + OFFER_TTL).isoformat(),
                },
            )
        return await self.repo.get_offer_by_command(command_id)

    async def apply_paypal_event(self, event) -> PayPalEventResult:
        """Applies a canonical PayPal event to the offer that owns its checkout order, if any."""
        if event.source_system != ALTERNATIVE_PROVIDER or event.event_type not in _SIGNALS:
            return PayPalEventResult(None)
        order_id = event.payment_id if event.event_type == "order.approved" else event.order_id
        if not order_id:
            return PayPalEventResult(None)
        offer_id = await self.repo.get_offer_id_by_order(order_id)
        if offer_id is None:
            return PayPalEventResult(None)
        offer = await self.repo.get_offer(offer_id, lock=True)
        if await self.repo.has_processed_payment_event(offer_id, event.event_id):
            return PayPalEventResult(offer)
        if (
            await self.repo.has_held_payment_event(offer_id, event.event_id)
            and OfferStatus(offer.status) is OfferStatus.OFFERED
        ):
            return PayPalEventResult(offer)

        before = OfferStatus(offer.status)
        transition = evaluate_offer_signal(
            _state(offer), _SIGNALS[event.event_type], _utc(event.timestamp),
            amount_minor_units=event.amount_minor_units, currency=event.currency,
        )
        await self._persist(offer, transition, OfferEventSource.PAYPAL_WEBHOOK, _utc(event.timestamp), event.event_id)

        if before is OfferStatus.OFFERED and transition.state.status is OfferStatus.ACCEPTED:
            # Captures delivered before the approval webhook are applied now that consent is recorded.
            for held in await self.repo.held_payment_events(offer_id):
                current = await self.repo.get_offer(offer_id)
                follow_up = evaluate_offer_signal(
                    _state(current), _SIGNALS[held.event_type], _utc(held.timestamp),
                    amount_minor_units=held.amount_minor_units, currency=held.currency,
                )
                await self._persist(current, follow_up, OfferEventSource.PAYPAL_WEBHOOK, _utc(held.timestamp), held.event_id)
        final = await self.repo.get_offer(offer_id)
        return PayPalEventResult(
            final, completed_now=before is not OfferStatus.COMPLETED and final.status == OfferStatus.COMPLETED.value,
        )

    async def record_decline(self, offer_id: uuid.UUID, reason: Optional[str] = None) -> DeclineResult:
        """
        The customer's explicit decline. A repeated decline of a DECLINED offer changes nothing; a decline
        of an accepted or otherwise finished offer is recorded as ignored and not applied.
        """
        offer = await self.repo.get_offer(offer_id, lock=True)
        if offer is None:
            return DeclineResult(None, False)
        if offer.status == OfferStatus.DECLINED.value:
            return DeclineResult(offer, False)
        at = self.clock()
        transition = evaluate_offer_signal(_state(offer), OfferSignal.DECLINE, at)
        await self._persist(
            offer, transition, OfferEventSource.MERCHANT_CHANNEL, at, None,
            details={"customer_reason": reason} if reason else None,
        )
        updated = await self.repo.get_offer(offer_id)
        return DeclineResult(updated, updated.status == OfferStatus.DECLINED.value)

    async def expire_due(self, now: Optional[datetime] = None) -> List[PaymentMethodSwitchOfferModel]:
        """Applies the response and completion deadlines to every offer past them."""
        now = now or self.clock()
        changed = []
        for offer_id in await self.repo.due_offer_ids(now):
            offer = await self.repo.get_offer(offer_id, lock=True)
            transition = evaluate_offer_signal(_state(offer), OfferSignal.CLOCK, now)
            if transition.changed:
                await self._persist(offer, transition, OfferEventSource.EXPIRY_SWEEP, now, None)
                changed.append(await self.repo.get_offer(offer_id))
        return changed

    async def _persist(
        self,
        offer: PaymentMethodSwitchOfferModel,
        transition: OfferTransition,
        source: OfferEventSource,
        occurred_at: datetime,
        payment_event_id: Optional[uuid.UUID],
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = self.clock()
        state = _state(offer)
        resolved_at = offer.resolved_at
        completion_event_id = offer.completion_event_id
        for step in transition.steps:
            # Deadline steps happened at the deadline itself and are deadline enforcement, whatever revealed
            # them; the signal's own step carries its source and event.
            if step.event_type is OfferEventType.OFFER_EXPIRED:
                step_at, step_event, step_source = state.expires_at, None, OfferEventSource.EXPIRY_SWEEP
            elif step.event_type is OfferEventType.OFFER_ABANDONED:
                step_at, step_event, step_source = state.completion_deadline, None, OfferEventSource.EXPIRY_SWEEP
            else:
                step_at, step_event, step_source = occurred_at, payment_event_id, source
            if step.to_status in TERMINAL_STATUSES and step.from_status not in TERMINAL_STATUSES:
                resolved_at = step_at
            if step.event_type is OfferEventType.PAYMENT_COMPLETED:
                completion_event_id = payment_event_id
            await self.repo.append_event(
                offer.offer_id,
                event_type=step.event_type.value,
                source=step_source.value,
                from_status=step.from_status.value,
                to_status=step.to_status.value,
                occurred_at=step_at,
                recorded_at=now,
                payment_event_id=step_event,
                reason=step.reason[:512],
                details=details or {},
            )
        if transition.changed:
            final = transition.state
            await self.repo.update_offer_state(
                offer.offer_id,
                status=final.status.value,
                accepted_at=final.accepted_at,
                completion_deadline=final.completion_deadline,
                resolved_at=resolved_at,
                completion_event_id=completion_event_id,
                updated_at=now,
            )
