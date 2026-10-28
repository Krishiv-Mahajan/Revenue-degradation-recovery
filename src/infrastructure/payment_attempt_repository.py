"""
Repository for the payment_attempts registry of Stage 5-8 payment attempts.

APPEND-ONLY: an attempt is registered once, from its first trigger event, and its row is never
updated or deleted. The database enforces the identity rules (CHECK constraints) and that the trigger
event exists under the same provider (fk_payment_attempt_trigger).
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.payment_attempt_exceptions import (
    AttemptNotRegisteredError,
    AttemptProviderConflictError,
)
from src.core.domain.payment_attempts import AttemptRef, RegisteredAttempt
from src.infrastructure.models import PaymentAttemptModel


class PaymentAttemptRepository:
    """Registers payment attempts and proves an attempt is registered before any Stage 5-8 write."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(self, trigger_event) -> RegisteredAttempt:
        """
        Register the attempt a trigger event starts, in the caller's transaction. Idempotent: an attempt
        already registered keeps its first trigger event. Raises AttemptProviderConflictError if the key
        is registered under another provider or reference; the existing row is never changed.
        """
        ref = AttemptRef.for_trigger_event(trigger_event)
        stmt = (
            insert(PaymentAttemptModel)
            .values(
                payment_attempt_id=ref.payment_attempt_id,
                source_system=ref.source_system,
                provider_reference=ref.provider_reference,
                trigger_event_id=trigger_event.event_id,
                registered_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing(index_elements=[PaymentAttemptModel.payment_attempt_id])
        )
        await self.session.execute(stmt)
        return await self.require(ref)

    async def require(self, ref: AttemptRef) -> RegisteredAttempt:
        """The registered attempt for ref; raises unless it is registered under exactly this identity."""
        stmt = select(
            PaymentAttemptModel.source_system,
            PaymentAttemptModel.provider_reference,
            PaymentAttemptModel.trigger_event_id,
            PaymentAttemptModel.registered_at,
        ).where(PaymentAttemptModel.payment_attempt_id == ref.payment_attempt_id)
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            raise AttemptNotRegisteredError(f"payment attempt {ref.payment_attempt_id!r} is not registered")
        if (row.source_system, row.provider_reference) != (ref.source_system, ref.provider_reference):
            raise AttemptProviderConflictError(
                f"payment attempt {ref.payment_attempt_id!r} is registered as {row.source_system}:"
                f"{row.provider_reference}, not {ref.source_system}:{ref.provider_reference}"
            )
        return RegisteredAttempt(ref=ref, trigger_event_id=row.trigger_event_id, registered_at=row.registered_at)
