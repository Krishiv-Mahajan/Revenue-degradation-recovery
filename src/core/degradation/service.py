from typing import List, Optional
import uuid
from datetime import datetime

from src.infrastructure.degradation_repository import DegradationRepository
from src.core.domain.analytics_models import PaymentHealthSnapshot
from src.core.degradation.signal import evaluate_snapshot
from src.core.degradation.state_machine import reconcile_timeline
from src.core.degradation.zscore import DetectionMode, detection_mode
from src.core.domain.degradation_models import EpisodeStatus

class DegradationService:
    def __init__(self, repository: DegradationRepository, mode: Optional[DetectionMode] = None):
        self.repository = repository
        self.mode = mode or detection_mode()

    async def process_snapshots(self, snapshots: List[PaymentHealthSnapshot], evaluation_timestamp: datetime) -> None:
        """
        Processes a sequence of snapshots for a single segment, starting from a replay point.
        The caller is responsible for providing all snapshots from the earliest affected window forward.
        """
        if not snapshots:
            return
            
        segment_dimension = snapshots[0].segment_dimension
        segment_value = snapshots[0].segment_value
        provider_scope = snapshots[0].provider_scope
        if any(s.provider_scope != provider_scope for s in snapshots):
            raise ValueError("process_snapshots needs snapshots of a single analytics scope")
        
        # Sort chronologically
        snapshots_sorted = sorted(snapshots, key=lambda s: s.window_start)
        
        # 1. Re-evaluate and append new signals
        for snapshot in snapshots_sorted:
            max_version = await self.repository.get_max_evaluation_version(
                segment_dimension, segment_value, snapshot.window_start, provider_scope
            )
            new_version = max_version + 1
            
            signal = evaluate_snapshot(snapshot, new_version, evaluation_timestamp, self.mode)
            await self.repository.append_signal(signal)
            
        # 2. Fetch the latest evaluations for the entire segment history
        latest_signals = await self.repository.get_latest_signals_for_segment(
            segment_dimension, segment_value, provider_scope
        )
        
        # 3. Reconstruct state machine
        valid_episodes = reconcile_timeline(latest_signals)
        
        reconciliation_run_id = uuid.uuid4()
        
        # 4. Reconcile episodes (Invalidate stale ones)
        existing_episodes = await self.repository.get_episodes_for_segment(
            segment_dimension, segment_value, provider_scope
        )
        valid_episode_ids = {e.episode_id for e in valid_episodes}
        
        for ep in existing_episodes:
            if ep.episode_id not in valid_episode_ids and ep.status != EpisodeStatus.INVALIDATED:
                ep.status = EpisodeStatus.INVALIDATED
                await self.repository.upsert_episode(ep, reconciliation_run_id, evaluation_timestamp)
                
        # 5. Upsert valid episodes (creates or updates)
        for ep in valid_episodes:
            await self.repository.upsert_episode(ep, reconciliation_run_id, evaluation_timestamp)
