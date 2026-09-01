import uuid
from typing import List, Optional
from datetime import datetime
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from src.infrastructure.models import DegradationSignalModel, DegradationEpisodeModel
from src.core.domain.degradation_models import DegradationSignal, DegradationEpisode, SignalType, EpisodeStatus, Severity

class DegradationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_latest_signals_for_segment(self, segment_dimension: str, segment_value: str) -> List[DegradationSignal]:
        stmt = (
            select(DegradationSignalModel)
            .filter_by(segment_dimension=segment_dimension, segment_value=segment_value)
            .order_by(
                DegradationSignalModel.window_start,
                DegradationSignalModel.evaluation_version.desc()
            )
            .distinct(DegradationSignalModel.window_start)
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()
        return [self._map_to_domain_signal(m) for m in models]

    async def get_max_evaluation_version(self, segment_dimension: str, segment_value: str, window_start: datetime) -> int:
        stmt = select(func.max(DegradationSignalModel.evaluation_version)).where(
            and_(
                DegradationSignalModel.segment_dimension == segment_dimension,
                DegradationSignalModel.segment_value == segment_value,
                DegradationSignalModel.window_start == window_start
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def append_signal(self, signal: DegradationSignal) -> None:
        model = DegradationSignalModel(
            signal_id=signal.signal_id,
            snapshot_id=signal.snapshot_id,
            segment_dimension=signal.segment_dimension,
            segment_value=signal.segment_value,
            window_start=signal.window_start,
            evaluation_version=signal.evaluation_version,
            signal_type=signal.signal_type.value,
            baseline_success_rate=signal.baseline_success_rate,
            absolute_drop=signal.absolute_drop,
            relative_drop=signal.relative_drop,
            evaluation_timestamp=signal.evaluation_timestamp
        )
        self.session.add(model)
        await self.session.flush()

    async def upsert_episode(self, episode: DegradationEpisode) -> None:
        stmt = insert(DegradationEpisodeModel).values(
            episode_id=episode.episode_id,
            segment_dimension=episode.segment_dimension,
            segment_value=episode.segment_value,
            started_at_window=episode.started_at_window,
            ended_at_window=episode.ended_at_window,
            status=episode.status.value,
            peak_absolute_drop=episode.peak_absolute_drop,
            affected_window_count=episode.affected_window_count,
            severity=episode.severity.value
        ).on_conflict_do_update(
            index_elements=['episode_id'],
            set_={
                'ended_at_window': episode.ended_at_window,
                'status': episode.status.value,
                'peak_absolute_drop': episode.peak_absolute_drop,
                'affected_window_count': episode.affected_window_count,
                'severity': episode.severity.value
            }
        )
        await self.session.execute(stmt)
        
    async def get_episodes_for_segment(self, segment_dimension: str, segment_value: str) -> List[DegradationEpisode]:
        stmt = select(DegradationEpisodeModel).where(
            and_(
                DegradationEpisodeModel.segment_dimension == segment_dimension,
                DegradationEpisodeModel.segment_value == segment_value
            )
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()
        return [self._map_to_domain_episode(m) for m in models]

    def _map_to_domain_signal(self, model: DegradationSignalModel) -> DegradationSignal:
        return DegradationSignal(
            signal_id=model.signal_id,
            snapshot_id=model.snapshot_id,
            segment_dimension=model.segment_dimension,
            segment_value=model.segment_value,
            window_start=model.window_start,
            evaluation_version=model.evaluation_version,
            signal_type=SignalType(model.signal_type),
            baseline_success_rate=model.baseline_success_rate,
            absolute_drop=model.absolute_drop,
            relative_drop=model.relative_drop,
            evaluation_timestamp=model.evaluation_timestamp
        )

    def _map_to_domain_episode(self, model: DegradationEpisodeModel) -> DegradationEpisode:
        return DegradationEpisode(
            episode_id=model.episode_id,
            segment_dimension=model.segment_dimension,
            segment_value=model.segment_value,
            started_at_window=model.started_at_window,
            status=EpisodeStatus(model.status),
            peak_absolute_drop=model.peak_absolute_drop,
            affected_window_count=model.affected_window_count,
            severity=Severity(model.severity),
            ended_at_window=model.ended_at_window
        )
