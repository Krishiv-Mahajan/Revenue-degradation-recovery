"""
Payment method switch offers (src/core/domain/switch_offer_models.py) for the merchant channel and the
dashboard. Every route requires dashboard credentials.

- GET  /api/v1/offers                    offers with per-status counts
- GET  /api/v1/offers/{offer_id}         the offer, what to present to the customer, and its full audit trail
- POST /api/v1/offers/{offer_id}/decline the customer's explicit decline, reported by the merchant channel

The checkout link is returned only while the offer is OFFERED, so a finished offer is never re-served.
Acceptance is never recorded here: only PayPal's verified approval of the checkout accepts an offer.
The decline takes a JSON body, so a cross-site form cannot submit it.
"""
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.api.security import require_dashboard_auth
from src.core.domain.switch_offer_models import OfferStatus
from src.core.execution.switch_offer_service import SwitchOfferService
from src.infrastructure.models import (
    CounterfactualAttributionModel,
    InterventionCommandModel,
    InterventionDecisionModel,
    InterventionExecutionAttemptModel,
    PaymentAttemptModel,
    PaymentMethodSwitchOfferModel,
    PaymentOutcomeObservationModel,
)
from src.infrastructure.switch_offer_repository import SwitchOfferRepository

router = APIRouter(prefix="/api/v1/offers", tags=["switch-offers"], dependencies=[Depends(require_dashboard_auth)])


class DeclineRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=256)


def _iso(value):
    return value.isoformat() if value is not None else None


def _parse_offer_id(offer_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(offer_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")


def _summary(offer: PaymentMethodSwitchOfferModel) -> Dict[str, Any]:
    actionable = offer.status == OfferStatus.OFFERED.value
    return {
        "offer_id": str(offer.offer_id),
        "status": offer.status,
        "payment_attempt_id": offer.payment_attempt_id,
        "source_system": offer.source_system,
        "alternative_provider": offer.alternative_provider,
        "environment": offer.environment,
        "amount_minor_units": offer.amount_minor_units,
        "currency": offer.currency,
        "offered_at": _iso(offer.offered_at),
        "expires_at": _iso(offer.expires_at),
        "accepted_at": _iso(offer.accepted_at),
        "completion_deadline": _iso(offer.completion_deadline),
        "resolved_at": _iso(offer.resolved_at),
        "checkout": {
            "order_id": offer.checkout_order_id,
            # Withheld once the offer is no longer awaiting the customer, so it cannot be reused.
            "approve_url": offer.approve_url if actionable else None,
            "actionable": actionable,
        },
    }


@router.get("")
async def list_offers(
    status_filter: Optional[OfferStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    repo = SwitchOfferRepository(session)
    offers = await repo.list_offers(status_filter.value if status_filter else None, limit)
    counts = await repo.counts_by_status()
    return {
        "counts_by_status": {s.value: counts.get(s.value, 0) for s in OfferStatus},
        "items": [_summary(offer) for offer in offers],
    }


@router.get("/{offer_id}")
async def get_offer(offer_id: str, session: AsyncSession = Depends(get_db_session)) -> Dict[str, Any]:
    repo = SwitchOfferRepository(session)
    offer = await repo.get_offer(_parse_offer_id(offer_id))
    if offer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")

    attempt = await session.get(PaymentAttemptModel, offer.payment_attempt_id)
    if attempt is not None and attempt.source_system != offer.source_system:
        attempt = None
    decision = await session.get(InterventionDecisionModel, offer.decision_id)
    command = await session.get(InterventionCommandModel, offer.command_id)
    execution = await session.get(InterventionExecutionAttemptModel, offer.execution_attempt_id)
    observation = (await session.execute(
        select(PaymentOutcomeObservationModel)
        .where(PaymentOutcomeObservationModel.command_id == offer.command_id)
        .order_by(desc(PaymentOutcomeObservationModel.observation_version)).limit(1)
    )).scalar_one_or_none()
    attribution = (await session.execute(
        select(CounterfactualAttributionModel)
        .where(CounterfactualAttributionModel.command_id == offer.command_id)
        .order_by(desc(CounterfactualAttributionModel.attribution_version)).limit(1)
    )).scalar_one_or_none()
    events = await repo.list_events(offer.offer_id)

    return {
        **_summary(offer),
        "presentation": offer.presentation if offer.status == OfferStatus.OFFERED.value else None,
        "original_attempt": {
            "payment_attempt_id": offer.payment_attempt_id,
            "source_system": offer.source_system,
            "provider_reference": attempt.provider_reference if attempt else None,
        },
        "diagnosis": {
            "episode_id": str(offer.episode_id) if offer.episode_id else None,
            "dimension": offer.diagnosis_dimension,
            "value": offer.diagnosis_value,
            "evidence_strength": offer.diagnosis_strength,
        },
        "decision": {
            "decision_id": str(offer.decision_id),
            "decision_type": decision.decision_type if decision else None,
            "selected_route_id": decision.selected_route_id if decision else None,
            "failure_probability": decision.failure_probability if decision else None,
            "policy_version": decision.policy_version if decision else None,
        },
        "command": {
            "command_id": str(offer.command_id),
            "command_status": command.command_status if command else None,
        },
        "execution": {
            "attempt_id": str(offer.execution_attempt_id),
            "executor_name": execution.executor_name if execution else None,
            "is_simulation": execution.is_simulation if execution else None,
        },
        "observation": {
            "payment_outcome": observation.payment_outcome,
            "terminal_event_id": str(observation.terminal_event_id) if observation.terminal_event_id else None,
            "verified_switch_completion": (
                observation.terminal_event_id is not None
                and observation.terminal_event_id == offer.completion_event_id
            ),
        } if observation else None,
        "attribution": {
            "status": attribution.attribution_status,
            "attributed_protected_gmv_minor_units": attribution.attributed_protected_gmv_minor_units,
            "is_simulated_execution": attribution.is_simulated_execution,
        } if attribution else None,
        "events": [
            {
                "sequence": e.sequence,
                "event_type": e.event_type,
                "source": e.source,
                "from_status": e.from_status,
                "to_status": e.to_status,
                "occurred_at": _iso(e.occurred_at),
                "recorded_at": _iso(e.recorded_at),
                "payment_event_id": str(e.payment_event_id) if e.payment_event_id else None,
                "reason": e.reason,
            }
            for e in events
        ],
    }


@router.post("/{offer_id}/decline")
async def decline_offer(
    offer_id: str, body: DeclineRequest, session: AsyncSession = Depends(get_db_session)
) -> Dict[str, Any]:
    result = await SwitchOfferService(session).record_decline(_parse_offer_id(offer_id), body.reason)
    if result.offer is None:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")
    offer_status = result.offer.status
    await session.commit()
    if offer_status != OfferStatus.DECLINED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The offer is {offer_status}; a decline can no longer be applied",
        )
    return {"offer_id": offer_id, "status": offer_status, "applied": result.applied}
