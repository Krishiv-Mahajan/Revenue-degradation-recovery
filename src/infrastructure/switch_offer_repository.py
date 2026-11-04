"""
Persistence for payment method switch offers (src/core/domain/switch_offer_models.py).

Offers change only through SwitchOfferService, under a row lock; their events are append-only.
"""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.execution_models import ExecutionResultStatus
from src.core.domain.switch_offer_models import OfferEventType, OfferStatus
from src.infrastructure.models import (
    InterventionExecutionAttemptModel,
    PaymentEventModel,
    PaymentMethodSwitchOfferEventModel,
    PaymentMethodSwitchOfferModel,
)


class SwitchOfferRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_offer_if_absent(self, values: Dict[str, Any]) -> bool:
        """Inserts the offer unless one already exists for its command. Returns whether it was inserted."""
        stmt = (
            insert(PaymentMethodSwitchOfferModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["command_id"])
            .returning(PaymentMethodSwitchOfferModel.offer_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def get_offer(self, offer_id: uuid.UUID, lock: bool = False) -> Optional[PaymentMethodSwitchOfferModel]:
        stmt = (
            select(PaymentMethodSwitchOfferModel)
            .where(PaymentMethodSwitchOfferModel.offer_id == offer_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_offer_by_command(self, command_id: uuid.UUID) -> Optional[PaymentMethodSwitchOfferModel]:
        stmt = (
            select(PaymentMethodSwitchOfferModel)
            .where(PaymentMethodSwitchOfferModel.command_id == command_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_offer_id_by_order(self, checkout_order_id: str) -> Optional[uuid.UUID]:
        stmt = select(PaymentMethodSwitchOfferModel.offer_id).where(
            PaymentMethodSwitchOfferModel.checkout_order_id == checkout_order_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def latest_real_successful_attempt(self, command_id: uuid.UUID) -> Optional[InterventionExecutionAttemptModel]:
        """The latest real (never simulated) successful execution attempt of a command."""
        stmt = (
            select(InterventionExecutionAttemptModel)
            .where(
                InterventionExecutionAttemptModel.command_id == command_id,
                InterventionExecutionAttemptModel.is_simulation.is_(False),
                InterventionExecutionAttemptModel.execution_status == ExecutionResultStatus.SUCCESS.value,
            )
            .order_by(InterventionExecutionAttemptModel.attempt_number.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def update_offer_state(self, offer_id: uuid.UUID, **values) -> None:
        await self.session.execute(
            update(PaymentMethodSwitchOfferModel)
            .where(PaymentMethodSwitchOfferModel.offer_id == offer_id)
            .values(**values)
        )

    async def append_event(self, offer_id: uuid.UUID, **values) -> None:
        sequence = (await self.session.execute(
            select(func.coalesce(func.max(PaymentMethodSwitchOfferEventModel.sequence), 0))
            .where(PaymentMethodSwitchOfferEventModel.offer_id == offer_id)
        )).scalar_one() + 1
        self.session.add(PaymentMethodSwitchOfferEventModel(
            offer_event_id=uuid.uuid4(), offer_id=offer_id, sequence=sequence, **values,
        ))
        await self.session.flush()

    async def has_processed_payment_event(self, offer_id: uuid.UUID, payment_event_id: uuid.UUID) -> bool:
        """Whether this PayPal event was already applied (anything but a hold) to the offer."""
        stmt = select(PaymentMethodSwitchOfferEventModel.offer_event_id).where(
            PaymentMethodSwitchOfferEventModel.offer_id == offer_id,
            PaymentMethodSwitchOfferEventModel.payment_event_id == payment_event_id,
            PaymentMethodSwitchOfferEventModel.event_type != OfferEventType.PAYMENT_OUTCOME_HELD.value,
        ).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def has_held_payment_event(self, offer_id: uuid.UUID, payment_event_id: uuid.UUID) -> bool:
        stmt = select(PaymentMethodSwitchOfferEventModel.offer_event_id).where(
            PaymentMethodSwitchOfferEventModel.offer_id == offer_id,
            PaymentMethodSwitchOfferEventModel.payment_event_id == payment_event_id,
            PaymentMethodSwitchOfferEventModel.event_type == OfferEventType.PAYMENT_OUTCOME_HELD.value,
        ).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def held_payment_events(self, offer_id: uuid.UUID) -> List[PaymentEventModel]:
        """Capture outcomes held before acceptance and not applied since, in ingestion order."""
        held = select(PaymentMethodSwitchOfferEventModel.payment_event_id).where(
            PaymentMethodSwitchOfferEventModel.offer_id == offer_id,
            PaymentMethodSwitchOfferEventModel.event_type == OfferEventType.PAYMENT_OUTCOME_HELD.value,
        )
        applied = select(PaymentMethodSwitchOfferEventModel.payment_event_id).where(
            PaymentMethodSwitchOfferEventModel.offer_id == offer_id,
            PaymentMethodSwitchOfferEventModel.event_type != OfferEventType.PAYMENT_OUTCOME_HELD.value,
            PaymentMethodSwitchOfferEventModel.payment_event_id.isnot(None),
        )
        stmt = (
            select(PaymentEventModel)
            .where(PaymentEventModel.event_id.in_(held), PaymentEventModel.event_id.notin_(applied))
            .order_by(PaymentEventModel.ingested_at.asc(), PaymentEventModel.event_id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def due_offer_ids(self, now: datetime) -> List[uuid.UUID]:
        """Offers whose response deadline or completion deadline has passed."""
        stmt = select(PaymentMethodSwitchOfferModel.offer_id).where(or_(
            and_(
                PaymentMethodSwitchOfferModel.status == OfferStatus.OFFERED.value,
                PaymentMethodSwitchOfferModel.expires_at <= now,
            ),
            and_(
                PaymentMethodSwitchOfferModel.status == OfferStatus.ACCEPTED.value,
                PaymentMethodSwitchOfferModel.completion_deadline <= now,
            ),
        )).order_by(PaymentMethodSwitchOfferModel.offer_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_events(self, offer_id: uuid.UUID) -> List[PaymentMethodSwitchOfferEventModel]:
        stmt = (
            select(PaymentMethodSwitchOfferEventModel)
            .where(PaymentMethodSwitchOfferEventModel.offer_id == offer_id)
            .order_by(PaymentMethodSwitchOfferEventModel.sequence)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_offers(self, status: Optional[str], limit: int) -> List[PaymentMethodSwitchOfferModel]:
        stmt = select(PaymentMethodSwitchOfferModel)
        if status is not None:
            stmt = stmt.where(PaymentMethodSwitchOfferModel.status == status)
        stmt = stmt.order_by(PaymentMethodSwitchOfferModel.offered_at.desc(), PaymentMethodSwitchOfferModel.offer_id).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def counts_by_status(self) -> Dict[str, int]:
        stmt = select(PaymentMethodSwitchOfferModel.status, func.count()).group_by(PaymentMethodSwitchOfferModel.status)
        return {status: count for status, count in (await self.session.execute(stmt)).all()}
