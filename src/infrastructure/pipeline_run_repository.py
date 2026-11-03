"""
Repository for pipeline_runs, the transactional outbox of Stages 2-8 runs (see
src/core/domain/pipeline_runs.py). Claims are single atomic UPDATE statements, so a background task and
the reconciler can never both run the same event.
"""
import uuid
from datetime import datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.pipeline_runs import (
    MAX_PIPELINE_ATTEMPTS,
    PENDING_GRACE,
    RUNNING_LEASE,
    PipelineRunStatus,
)
from src.infrastructure.models import PaymentEventModel, PipelineRunModel


class ClaimResult(str, Enum):
    CLAIMED = "CLAIMED"
    UNTRACKED = "UNTRACKED"  # no outbox row (stored before the outbox existed): run without tracking
    NOT_CLAIMABLE = "NOT_CLAIMABLE"  # done, attempts exhausted, or held by a live claim


def new_pending_run(event_id: uuid.UUID, source_system: str, now: datetime) -> PipelineRunModel:
    return PipelineRunModel(
        event_id=event_id, source_system=source_system, status=PipelineRunStatus.PENDING.value,
        attempts=0, created_at=now, updated_at=now,
    )


def _claimable(now: datetime):
    return and_(
        PipelineRunModel.attempts < MAX_PIPELINE_ATTEMPTS,
        or_(
            PipelineRunModel.status.in_([PipelineRunStatus.PENDING.value, PipelineRunStatus.FAILED.value]),
            and_(
                PipelineRunModel.status == PipelineRunStatus.RUNNING.value,
                PipelineRunModel.updated_at <= now - RUNNING_LEASE,
            ),
        ),
    )


class PipelineRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(self, event_id: uuid.UUID, now: datetime) -> ClaimResult:
        """Atomically mark a claimable run RUNNING (attempts + 1) and commit."""
        stmt = (
            update(PipelineRunModel)
            .where(PipelineRunModel.event_id == event_id, _claimable(now))
            .values(status=PipelineRunStatus.RUNNING.value, attempts=PipelineRunModel.attempts + 1, updated_at=now)
            .returning(PipelineRunModel.event_id)
        )
        claimed = (await self.session.execute(stmt)).scalar_one_or_none()
        if claimed is not None:
            await self.session.commit()
            return ClaimResult.CLAIMED
        exists = await self.session.scalar(select(PipelineRunModel.event_id).where(PipelineRunModel.event_id == event_id))
        await self.session.rollback()
        return ClaimResult.NOT_CLAIMABLE if exists is not None else ClaimResult.UNTRACKED

    async def finish(
        self, event_id: uuid.UUID, now: datetime, *, succeeded: bool,
        failed_stage: Optional[str] = None, error_type: Optional[str] = None,
    ) -> None:
        """Record the outcome of a claimed run and commit. Only a RUNNING row is updated."""
        values = (
            dict(status=PipelineRunStatus.SUCCEEDED.value, completed_at=now, updated_at=now)
            if succeeded
            else dict(
                status=PipelineRunStatus.FAILED.value, updated_at=now,
                last_failed_stage=(failed_stage or "")[:64] or None, last_error_type=(error_type or "")[:128] or None,
            )
        )
        await self.session.execute(
            update(PipelineRunModel)
            .where(PipelineRunModel.event_id == event_id, PipelineRunModel.status == PipelineRunStatus.RUNNING.value)
            .values(**values)
        )
        await self.session.commit()

    async def find_due(self, now: datetime, limit: int = 100) -> List[PaymentEventModel]:
        """
        Events whose runs need (re)running: PENDING past the grace period (the background task never
        ran), FAILED with attempts left, or RUNNING past the lease (the worker died). Oldest first.
        """
        due = and_(
            _claimable(now),
            or_(
                PipelineRunModel.status != PipelineRunStatus.PENDING.value,
                PipelineRunModel.created_at <= now - PENDING_GRACE,
            ),
        )
        stmt = (
            select(PaymentEventModel)
            .join(PipelineRunModel, PipelineRunModel.event_id == PaymentEventModel.event_id)
            .where(due)
            .order_by(PipelineRunModel.created_at.asc(), PipelineRunModel.event_id.asc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())
