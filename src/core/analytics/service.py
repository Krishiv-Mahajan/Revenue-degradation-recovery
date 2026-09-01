from datetime import datetime, timedelta, timezone
from typing import List, Set, Tuple

from src.infrastructure.analytics_repository import AnalyticsRepository
from src.core.analytics.calculator import calculate_health_metrics_for_window, calculate_baseline

class PaymentHealthAnalyticsService:
    def __init__(self, repository: AnalyticsRepository):
        self.repository = repository

    def _get_window_boundaries(self, dt: datetime) -> Tuple[datetime, datetime]:
        """
        Returns the (start, end) of the 5-minute tumbling window that `dt` falls into.
        e.g., 10:03:45 -> (10:00:00, 10:05:00)
        """
        minute = dt.minute
        window_start_minute = (minute // 5) * 5
        window_start = dt.replace(minute=window_start_minute, second=0, microsecond=0)
        window_end = window_start + timedelta(minutes=5)
        return window_start, window_end

    async def calculate_and_save_window(self, window_start: datetime, window_end: datetime) -> None:
        """
        Calculates the health metrics and baselines for a specific window, and upserts them.
        """
        # 1. Fetch all events in this window
        events = await self.repository.get_events_in_window(window_start, window_end)

        # 2. Calculate metrics (returns snapshots with baseline_success_rate = None)
        snapshots = calculate_health_metrics_for_window(events, window_start, window_end)

        # 3. Calculate baselines for each snapshot
        for snapshot in snapshots:
            historical_snapshots = await self.repository.get_historical_snapshots(
                target_window_start=window_start,
                target_window_end=window_end,
                segment_dimension=snapshot.segment_dimension,
                segment_value=snapshot.segment_value,
                weeks_back=4
            )
            baseline = calculate_baseline(historical_snapshots)

            # Since Pydantic models are frozen, we must use model_copy(update=...)
            updated_snapshot = snapshot.model_copy(update={'baseline_success_rate': baseline})

            # Replace the snapshot in the list
            idx = snapshots.index(snapshot)
            snapshots[idx] = updated_snapshot

        # 4. Upsert to database
        await self.repository.upsert_snapshots(snapshots)

    async def recalculate_late_events(self, since_ingested_at: datetime) -> None:
        """
        Finds all events ingested after `since_ingested_at` and identifies their 5-minute windows.
        It then recalculates each affected window to incorporate the late-arriving events.
        """
        # Fetch events ingested after since_ingested_at
        # To do this cleanly, we can add a method to the repo: `get_events_ingested_after`
        # But for now we can just rely on the repo having a method for it.
        events = await self.repository.get_events_ingested_after(since_ingested_at)

        affected_windows: Set[Tuple[datetime, datetime]] = set()
        for event in events:
            window = self._get_window_boundaries(event.timestamp)
            affected_windows.add(window)

        for window_start, window_end in affected_windows:
            await self.calculate_and_save_window(window_start, window_end)
