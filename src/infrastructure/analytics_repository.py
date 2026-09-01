from datetime import datetime, timedelta, timezone
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from src.core.domain.models import PaymentEvent
from src.core.domain.analytics_models import PaymentHealthSnapshot
from src.infrastructure.models import PaymentEventModel, PaymentHealthSnapshotModel

class AnalyticsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_events_in_window(self, window_start: datetime, window_end: datetime) -> List[PaymentEvent]:
        stmt = select(PaymentEventModel).where(
            PaymentEventModel.timestamp >= window_start,
            PaymentEventModel.timestamp < window_end
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()

        events = []
        for m in models:
            events.append(PaymentEvent(
                event_id=m.event_id,
                source_system=m.source_system,
                source_event_id=m.source_event_id,
                payment_id=m.payment_id,
                order_id=m.order_id,
                timestamp=m.timestamp,
                event_type=m.event_type,
                currency=m.currency,
                amount_minor_units=m.amount_minor_units,
                payment_status=m.payment_status,
                payment_method=m.payment_method,
                bank=m.bank,
                wallet=m.wallet,
                error_code=m.error_code,
                error_description=m.error_description,
                error_source=m.error_source,
                error_step=m.error_step,
                error_reason=m.error_reason,
                ingested_at=m.ingested_at
            ))
        return events

    async def get_events_ingested_after(self, ingested_after: datetime) -> List[PaymentEvent]:
        stmt = select(PaymentEventModel).where(
            PaymentEventModel.ingested_at > ingested_after
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()

        events = []
        for m in models:
            events.append(PaymentEvent(
                event_id=m.event_id,
                source_system=m.source_system,
                source_event_id=m.source_event_id,
                payment_id=m.payment_id,
                order_id=m.order_id,
                timestamp=m.timestamp,
                event_type=m.event_type,
                currency=m.currency,
                amount_minor_units=m.amount_minor_units,
                payment_status=m.payment_status,
                payment_method=m.payment_method,
                bank=m.bank,
                wallet=m.wallet,
                error_code=m.error_code,
                error_description=m.error_description,
                error_source=m.error_source,
                error_step=m.error_step,
                error_reason=m.error_reason,
                ingested_at=m.ingested_at
            ))
        return events

    async def get_historical_snapshots(
        self,
        target_window_start: datetime,
        target_window_end: datetime,
        segment_dimension: str,
        segment_value: str,
        weeks_back: int = 4
    ) -> List[PaymentHealthSnapshot]:
        """
        Retrieves matching windows from the past N weeks for the same day of week and time of day.
        """
        historical_starts = [
            target_window_start - timedelta(weeks=i) for i in range(1, weeks_back + 1)
        ]

        if not historical_starts:
            return []

        stmt = select(PaymentHealthSnapshotModel).where(
            PaymentHealthSnapshotModel.window_start.in_(historical_starts),
            PaymentHealthSnapshotModel.segment_dimension == segment_dimension,
            PaymentHealthSnapshotModel.segment_value == segment_value
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()

        snapshots = []
        for m in models:
            snapshots.append(PaymentHealthSnapshot(
                snapshot_id=m.snapshot_id,
                window_start=m.window_start,
                window_end=m.window_end,
                segment_dimension=m.segment_dimension,
                segment_value=m.segment_value,
                transaction_count=m.transaction_count,
                successful_transaction_count=m.successful_transaction_count,
                failed_transaction_count=m.failed_transaction_count,
                success_rate=m.success_rate,
                failure_rate=m.failure_rate,
                total_gmv_minor_units=m.total_gmv_minor_units,
                successful_gmv_minor_units=m.successful_gmv_minor_units,
                failed_gmv_minor_units=m.failed_gmv_minor_units,
                baseline_success_rate=m.baseline_success_rate,
                insufficient_volume=m.insufficient_volume,
                calculated_at=m.calculated_at
            ))
        return snapshots

    async def upsert_snapshots(self, snapshots: List[PaymentHealthSnapshot]) -> None:
        if not snapshots:
            return

        for snapshot in snapshots:
            stmt = insert(PaymentHealthSnapshotModel).values(
                snapshot_id=snapshot.snapshot_id,
                window_start=snapshot.window_start,
                window_end=snapshot.window_end,
                segment_dimension=snapshot.segment_dimension,
                segment_value=snapshot.segment_value,
                transaction_count=snapshot.transaction_count,
                successful_transaction_count=snapshot.successful_transaction_count,
                failed_transaction_count=snapshot.failed_transaction_count,
                success_rate=snapshot.success_rate,
                failure_rate=snapshot.failure_rate,
                total_gmv_minor_units=snapshot.total_gmv_minor_units,
                successful_gmv_minor_units=snapshot.successful_gmv_minor_units,
                failed_gmv_minor_units=snapshot.failed_gmv_minor_units,
                baseline_success_rate=snapshot.baseline_success_rate,
                insufficient_volume=snapshot.insufficient_volume,
                calculated_at=snapshot.calculated_at
            )

            # Upsert logic based on the unique constraint uq_snapshot_identity
            stmt = stmt.on_conflict_do_update(
                index_elements=['window_start', 'window_end', 'segment_dimension', 'segment_value'],
                set_={
                    'transaction_count': stmt.excluded.transaction_count,
                    'successful_transaction_count': stmt.excluded.successful_transaction_count,
                    'failed_transaction_count': stmt.excluded.failed_transaction_count,
                    'success_rate': stmt.excluded.success_rate,
                    'failure_rate': stmt.excluded.failure_rate,
                    'total_gmv_minor_units': stmt.excluded.total_gmv_minor_units,
                    'successful_gmv_minor_units': stmt.excluded.successful_gmv_minor_units,
                    'failed_gmv_minor_units': stmt.excluded.failed_gmv_minor_units,
                    'baseline_success_rate': stmt.excluded.baseline_success_rate,
                    'insufficient_volume': stmt.excluded.insufficient_volume,
                    'calculated_at': stmt.excluded.calculated_at
                }
            )
            await self.session.execute(stmt)
        await self.session.commit()
