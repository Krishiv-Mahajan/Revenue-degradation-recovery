import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, Optional, Protocol

from src.core.domain.exceptions import (
    DuplicateEventConflictError,
    InvalidWebhookSignatureError,
    MalformedInputError,
    UnsupportedEventTypeError,
)
from src.core.domain.models import PaymentEvent, RawIngestionRecord
from src.core.normalizers.hashing import generate_deterministic_hash
from src.core.normalizers.paypal import SOURCE_SYSTEM, normalize_paypal_event
from src.core.parsers.paypal import parse_paypal_event
from src.core.services.ingestion_service import IngestionRepository


class PayPalWebhookVerifier(Protocol):
    async def verify_webhook_signature(self, headers: Mapping[str, str], raw_body: bytes): ...


class PayPalIngestionOutcome(str, Enum):
    STORED = "stored"
    DUPLICATE = "duplicate"
    IGNORED_UNSUPPORTED = "ignored"


@dataclass(frozen=True)
class PayPalIngestionResult:
    outcome: PayPalIngestionOutcome
    source_event_id: Optional[str] = None
    payment_event: Optional[PaymentEvent] = None


def _reject_constant(value: str):
    raise ValueError(f"Non-standard JSON constant: {value}")


class PayPalIngestionService:
    """
    Verified PayPal webhook intake: verify, parse, normalize, deduplicate and persist.
    Persistence only; never triggers downstream pipeline stages.
    """

    def __init__(self, repository: IngestionRepository, verifier: PayPalWebhookVerifier):
        self.repository = repository
        self.verifier = verifier

    async def ingest(self, headers: Mapping[str, str], raw_body: bytes) -> PayPalIngestionResult:
        # Nothing is parsed or stored until PayPal confirms the signature.
        verification = await self.verifier.verify_webhook_signature(headers, raw_body)
        if not verification.verified:
            raise InvalidWebhookSignatureError("PayPal webhook signature verification failed")

        try:
            raw_payload = json.loads(raw_body, parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as e:
            raise MalformedInputError(f"Invalid JSON payload: {e}")
        if not isinstance(raw_payload, dict):
            raise MalformedInputError("PayPal webhook payload must be a JSON object")

        try:
            event = parse_paypal_event(raw_payload)
        except UnsupportedEventTypeError:
            return PayPalIngestionResult(outcome=PayPalIngestionOutcome.IGNORED_UNSUPPORTED)

        payload_hash = generate_deterministic_hash(raw_payload)

        existing = await self.repository.get_raw_record(SOURCE_SYSTEM, event.event_id)
        if existing is not None:
            return self._resolve_existing(existing, payload_hash, event.event_id)

        payment_event = normalize_paypal_event(event)
        raw_record = RawIngestionRecord(
            source_system=SOURCE_SYSTEM,
            source_event_id=event.event_id,
            received_at=datetime.now(timezone.utc),
            raw_payload=raw_payload,
            payload_hash=payload_hash,
        )

        try:
            await self.repository.save_ingestion(raw_record, payment_event)
        except DuplicateEventConflictError:
            # A concurrent delivery won the insert race; the stored hash decides the outcome.
            winner = await self.repository.get_raw_record(SOURCE_SYSTEM, event.event_id)
            if winner is None:
                raise
            return self._resolve_existing(winner, payload_hash, event.event_id)

        return PayPalIngestionResult(
            outcome=PayPalIngestionOutcome.STORED,
            source_event_id=event.event_id,
            payment_event=payment_event,
        )

    @staticmethod
    def _resolve_existing(
        existing: RawIngestionRecord, payload_hash: str, source_event_id: str
    ) -> PayPalIngestionResult:
        if existing.payload_hash != payload_hash:
            raise DuplicateEventConflictError(
                f"Conflict: PayPal event {source_event_id} already exists with a different payload"
            )
        return PayPalIngestionResult(
            outcome=PayPalIngestionOutcome.DUPLICATE, source_event_id=source_event_id
        )
