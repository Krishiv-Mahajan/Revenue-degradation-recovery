import json
from datetime import datetime, timezone
from typing import Optional

from src.core.domain.models import RawIngestionRecord, PaymentEvent
from src.core.domain.exceptions import (
    DuplicateEventConflictError,
    PersistenceError
)
from src.core.parsers.razorpay import parse_razorpay_event, verify_razorpay_signature
from src.core.normalizers.razorpay import normalize_razorpay_event, generate_deterministic_hash

class IngestionRepository:
    """
    Abstract interface for persistence. To be implemented by infrastructure.
    """
    async def get_raw_record(self, source_system: str, source_event_id: str) -> Optional[RawIngestionRecord]:
        raise NotImplementedError

    async def save_ingestion(self, raw_record: RawIngestionRecord, payment_event: PaymentEvent) -> None:
        """
        Atomically saves both the raw evidence and canonical event.
        Must raise DuplicateEventConflictError if identity conflict occurs.
        """
        raise NotImplementedError


class IngestionService:
    def __init__(self, repository: IngestionRepository, razorpay_webhook_secret: str):
        self.repository = repository
        self.razorpay_webhook_secret = razorpay_webhook_secret

    async def ingest_razorpay_webhook(
        self,
        payload_body: bytes,
        signature_header: str,
        source_event_id: str
    ) -> PaymentEvent:
        """
        Orchestrates the ingestion of a Razorpay webhook.
        1. Verifies authenticity (signature).
        2. Parses and validates schema.
        3. Generates deterministic hash.
        4. Checks deduplication idempotency vs conflict.
        5. Normalizes to canonical format.
        6. Persists transactionally.
        """
        # 1. Verify Authenticity
        verify_razorpay_signature(payload_body, signature_header, self.razorpay_webhook_secret)

        # 2. Parse & Validate Schema
        parsed_event = parse_razorpay_event(payload_body)
        raw_payload_dict = json.loads(payload_body)

        # 3. Generate deterministic hash
        payload_hash = generate_deterministic_hash(raw_payload_dict)

        # 4. Deduplication Check
        existing_record = await self.repository.get_raw_record(
            source_system="razorpay",
            source_event_id=source_event_id
        )

        if existing_record:
            if existing_record.payload_hash == payload_hash:
                # Idempotent retry, do not create another event
                # Note: We should ideally return the existing PaymentEvent, but returning a newly normalized
                # instance based on the original payload represents the same canonical fact.
                # Since we don't have get_payment_event in repo yet, we can normalize it again for the response.
                return normalize_razorpay_event(parsed_event, source_event_id)
            else:
                # Conflict
                raise DuplicateEventConflictError(
                    f"Event {source_event_id} from razorpay already exists with a different payload."
                )

        # 5. Normalize
        payment_event = normalize_razorpay_event(parsed_event, source_event_id)

        # 6. Prepare Raw Evidence
        raw_record = RawIngestionRecord(
            source_system="razorpay",
            source_event_id=source_event_id,
            received_at=datetime.now(timezone.utc),
            raw_payload=raw_payload_dict,
            payload_hash=payload_hash
        )

        # 7. Persist
        await self.repository.save_ingestion(raw_record, payment_event)

        return payment_event
