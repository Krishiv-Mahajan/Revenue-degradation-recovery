from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError

from src.core.services.ingestion_service import IngestionRepository
from src.core.domain.models import RawIngestionRecord, PaymentEvent
from src.core.domain.exceptions import DuplicateEventConflictError, PersistenceError
from src.infrastructure.models import RawIngestionRecordModel, PaymentEventModel

class SQLIngestionRepository(IngestionRepository):
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_raw_record(self, source_system: str, source_event_id: str) -> Optional[RawIngestionRecord]:
        try:
            stmt = select(RawIngestionRecordModel).where(
                RawIngestionRecordModel.source_system == source_system,
                RawIngestionRecordModel.source_event_id == source_event_id
            )
            result = await self.session.execute(stmt)
            record = result.scalars().first()

            if record:
                return RawIngestionRecord(
                    source_system=record.source_system,
                    source_event_id=record.source_event_id,
                    received_at=record.received_at,
                    raw_payload=record.raw_payload,
                    payload_hash=record.payload_hash
                )
            return None
        except Exception as e:
            raise PersistenceError(f"Failed to query raw record: {str(e)}")

    async def save_ingestion(self, raw_record: RawIngestionRecord, payment_event: PaymentEvent) -> None:
        raw_model = RawIngestionRecordModel(
            source_system=raw_record.source_system,
            source_event_id=raw_record.source_event_id,
            received_at=raw_record.received_at,
            raw_payload=raw_record.raw_payload,
            payload_hash=raw_record.payload_hash
        )

        event_model = PaymentEventModel(
            event_id=payment_event.event_id,
            source_system=payment_event.source_system,
            source_event_id=payment_event.source_event_id,
            payment_id=payment_event.payment_id,
            order_id=payment_event.order_id,
            timestamp=payment_event.timestamp,
            event_type=payment_event.event_type,
            currency=payment_event.currency,
            amount_minor_units=payment_event.amount_minor_units,
            payment_status=payment_event.payment_status,
            payment_method=payment_event.payment_method,
            bank=payment_event.bank,
            wallet=payment_event.wallet,
            error_code=payment_event.error_code,
            error_description=payment_event.error_description,
            error_source=payment_event.error_source,
            error_step=payment_event.error_step,
            error_reason=payment_event.error_reason,
            ingested_at=payment_event.ingested_at
        )

        try:
            self.session.add(raw_model)
            self.session.add(event_model)
            await self.session.commit()
        except IntegrityError as e:
            await self.session.rollback()
            # If a unique constraint is violated on (source_system, source_event_id)
            if 'uq_raw_ingestion_source_identity' in str(e.orig) or 'uq_payment_event_source_identity' in str(e.orig):
                raise DuplicateEventConflictError(
                    f"Conflict: Event {raw_record.source_event_id} from {raw_record.source_system} already exists."
                )
            raise PersistenceError(f"Database integrity error: {str(e)}")
        except Exception as e:
            await self.session.rollback()
            raise PersistenceError(f"Failed to save ingestion data: {str(e)}")
