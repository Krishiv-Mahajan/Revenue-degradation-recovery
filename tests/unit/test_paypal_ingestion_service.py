"""
Offline tests for PayPalIngestionService: verify, parse, normalize, deduplicate and persist.
PayPal is simulated with httpx.MockTransport; persistence with an in-memory repository
that enforces the same (source_system, source_event_id) uniqueness as the database.
"""
import asyncio
import json

import httpx
import pytest

import src.core.services.paypal_ingestion_service as paypal_ingestion_module
from src.core.domain.exceptions import (
    DuplicateEventConflictError,
    InvalidWebhookSignatureError,
    MalformedInputError,
    NormalizationError,
    PersistenceError,
    SchemaValidationError,
)
from src.core.normalizers import hashing, razorpay as razorpay_normalizer
from src.core.normalizers.hashing import generate_deterministic_hash
from src.core.services.ingestion_service import IngestionRepository
from src.core.services.paypal_ingestion_service import (
    PayPalIngestionOutcome,
    PayPalIngestionService,
)
from src.infrastructure.paypal_client import (
    PayPalAuthError,
    PayPalClient,
    PayPalConfig,
    PayPalConfigurationError,
    PayPalUnavailableError,
    PayPalVerificationRequestRejectedError,
    PayPalWebhookVerification,
)
from tests.unit import paypal_event_samples as samples

CLIENT_ID = "AbCdEf-client_id-123"
CLIENT_SECRET = "EXAMPLE-client-secret-value-XYZ"
ACCESS_TOKEN = "A21AAExampleAccessTokenValue-123"
WEBHOOK_ID = "8PT597110X687430LKGECATA"
VERIFY_PATH = "/v1/notifications/verify-webhook-signature"
TOKEN_PATH = "/v1/oauth2/token"

HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-360caa42-fca2a594-a5cafa77",
    "PAYPAL-TRANSMISSION-ID": "69cd13f0-d67a-11e5-baa3-778b53f4ae55",
    "PAYPAL-TRANSMISSION-SIG": "c2lnbmF0dXJlLWV4YW1wbGU=",
    "PAYPAL-TRANSMISSION-TIME": "2016-02-18T20:01:35Z",
}


def body_of(event) -> bytes:
    return json.dumps(event).encode("utf-8")


def unsupported_event():
    event = samples.capture_completed()
    event["event_type"] = "BILLING.SUBSCRIPTION.CREATED"
    event["resource_type"] = "subscription"
    return event


class PayPalStub:
    """MockTransport handler: answers the token request and replies to verification calls."""

    def __init__(self, verify_status="SUCCESS", verify_response=None, token_response=None):
        self.requests = []
        self._verify_response = verify_response or (
            lambda request: httpx.Response(200, json={"verification_status": verify_status})
        )
        self._token_response = token_response or (
            lambda request: httpx.Response(
                200, json={"access_token": ACCESS_TOKEN, "token_type": "Bearer", "expires_in": 32400}
            )
        )

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == TOKEN_PATH:
            return self._token_response(request)
        return self._verify_response(request)

    @property
    def verify_requests(self):
        return [r for r in self.requests if r.url.path == VERIFY_PATH]


def paypal_client(stub, **config_overrides) -> PayPalClient:
    values = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "webhook_id": WEBHOOK_ID}
    values.update(config_overrides)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    return PayPalClient(PayPalConfig(**values), http_client=http_client)


class InMemoryIngestionRepository(IngestionRepository):
    """Enforces the unique (source_system, source_event_id) identity like the real tables."""

    def __init__(self, read_barrier=0):
        self.raw_records = {}
        self.payment_events = {}
        self.get_calls = 0
        self.save_calls = 0
        self._read_barrier = read_barrier
        self._all_arrived = asyncio.Event()

    async def get_raw_record(self, source_system, source_event_id):
        self.get_calls += 1
        snapshot = self.raw_records.get((source_system, source_event_id))
        if self.get_calls <= self._read_barrier:
            # The first N reads all see the pre-insert state before any delivery inserts.
            if self.get_calls == self._read_barrier:
                self._all_arrived.set()
            await self._all_arrived.wait()
        return snapshot

    async def save_ingestion(self, raw_record, payment_event):
        self.save_calls += 1
        key = (raw_record.source_system, raw_record.source_event_id)
        if key in self.raw_records:
            raise DuplicateEventConflictError("unique constraint violated")
        self.raw_records[key] = raw_record
        self.payment_events[key] = payment_event


class LostRaceRepository(InMemoryIngestionRepository):
    """Another delivery inserts its record between our pre-check and our insert."""

    def __init__(self, winner_service_payload):
        super().__init__()
        self._winner_payload = winner_service_payload

    async def save_ingestion(self, raw_record, payment_event):
        winner = raw_record.model_copy(
            update={
                "raw_payload": self._winner_payload,
                "payload_hash": generate_deterministic_hash(self._winner_payload),
            }
        )
        self.raw_records[(winner.source_system, winner.source_event_id)] = winner
        await super().save_ingestion(raw_record, payment_event)


def make_service(stub=None, repository=None, **config_overrides):
    stub = stub or PayPalStub()
    repository = repository or InMemoryIngestionRepository()
    return PayPalIngestionService(repository, paypal_client(stub, **config_overrides)), stub, repository


# --- verification gate ---------------------------------------------------------

async def test_verified_event_persists_raw_evidence_and_canonical_event():
    event = samples.capture_completed()
    raw_body = body_of(event)
    service, stub, repository = make_service()

    result = await service.ingest(HEADERS, raw_body)

    assert result.outcome is PayPalIngestionOutcome.STORED
    assert result.source_event_id == samples.EVENT_ID
    key = ("paypal", samples.EVENT_ID)
    raw_record = repository.raw_records[key]
    assert raw_record.raw_payload == event
    assert raw_record.payload_hash == generate_deterministic_hash(event)
    assert raw_record.received_at.tzinfo is not None
    payment_event = repository.payment_events[key]
    assert payment_event is result.payment_event
    assert payment_event.source_system == "paypal"
    assert payment_event.source_event_id == samples.EVENT_ID
    assert payment_event.payment_id == samples.CAPTURE_ID
    assert payment_event.amount_minor_units == 50000
    assert payment_event.currency == "USD"

    [verify_request] = stub.verify_requests
    assert verify_request.content.endswith(b',"webhook_event":' + raw_body + b"}")


@pytest.mark.parametrize("event_type", sorted(samples.ALL_SUPPORTED))
async def test_every_supported_event_type_is_stored(event_type):
    service, _, repository = make_service()
    result = await service.ingest(HEADERS, body_of(samples.ALL_SUPPORTED[event_type]()))
    assert result.outcome is PayPalIngestionOutcome.STORED
    assert len(repository.raw_records) == len(repository.payment_events) == 1


async def test_failed_verification_never_parses_or_persists(monkeypatch):
    parsed = []
    monkeypatch.setattr(paypal_ingestion_module, "parse_paypal_event", lambda e: parsed.append(e))
    service, stub, repository = make_service(PayPalStub(verify_status="FAILURE"))

    with pytest.raises(InvalidWebhookSignatureError):
        await service.ingest(HEADERS, body_of(samples.capture_completed()))

    assert len(stub.verify_requests) == 1
    assert parsed == []
    assert repository.get_calls == repository.save_calls == 0


async def test_missing_transmission_headers_reject_before_network_or_persistence():
    service, stub, repository = make_service()
    headers = {k: v for k, v in HEADERS.items() if k != "PAYPAL-TRANSMISSION-SIG"}

    with pytest.raises(InvalidWebhookSignatureError):
        await service.ingest(headers, body_of(samples.capture_completed()))

    assert stub.requests == []
    assert repository.get_calls == repository.save_calls == 0


async def test_missing_webhook_id_fails_closed():
    service, stub, repository = make_service(webhook_id=None)

    with pytest.raises(PayPalConfigurationError):
        await service.ingest(HEADERS, body_of(samples.capture_completed()))

    assert stub.requests == []
    assert repository.get_calls == repository.save_calls == 0


@pytest.mark.parametrize("raw_body", [b"not json", b'{"id": NaN}', b"\xff\xfe", b"[]"])
async def test_malformed_body_is_rejected_without_persistence(raw_body):
    service, _, repository = make_service()

    with pytest.raises(MalformedInputError):
        await service.ingest(HEADERS, raw_body)

    assert repository.get_calls == repository.save_calls == 0


async def test_deeply_nested_body_after_verification_is_malformed_not_a_crash():
    class AlwaysVerified:
        async def verify_webhook_signature(self, headers, raw_body):
            return PayPalWebhookVerification(verified=True)

    repository = InMemoryIngestionRepository()
    service = PayPalIngestionService(repository, AlwaysVerified())
    depth = 50_000

    with pytest.raises(MalformedInputError):
        await service.ingest(HEADERS, b'{"a":' * depth + b"1" + b"}" * depth)
    assert repository.get_calls == repository.save_calls == 0


# --- provider outages ----------------------------------------------------------

@pytest.mark.parametrize(
    "stub, expected",
    [
        (PayPalStub(verify_response=lambda r: httpx.Response(503)), PayPalUnavailableError),
        (PayPalStub(verify_response=lambda r: httpx.Response(500, text="oops")), PayPalUnavailableError),
        (PayPalStub(token_response=lambda r: httpx.Response(401)), PayPalAuthError),
        (PayPalStub(verify_response=lambda r: httpx.Response(400, json={"name": "VALIDATION_ERROR"})),
         PayPalVerificationRequestRejectedError),
    ],
)
async def test_provider_failures_propagate_without_persistence(stub, expected):
    service, _, repository = make_service(stub)

    with pytest.raises(expected):
        await service.ingest(HEADERS, body_of(samples.capture_completed()))

    assert repository.get_calls == repository.save_calls == 0


async def test_network_error_is_unavailable():
    def raise_connect_error(request):
        raise httpx.ConnectError("connection refused", request=request)

    service, _, repository = make_service(PayPalStub(verify_response=raise_connect_error))

    with pytest.raises(PayPalUnavailableError):
        await service.ingest(HEADERS, body_of(samples.capture_completed()))
    assert repository.save_calls == 0


# --- parsing and normalization -------------------------------------------------

async def test_verified_unsupported_event_is_ignored_and_not_persisted():
    service, stub, repository = make_service()

    result = await service.ingest(HEADERS, body_of(unsupported_event()))

    assert result.outcome is PayPalIngestionOutcome.IGNORED_UNSUPPORTED
    assert result.payment_event is None
    assert len(stub.verify_requests) == 1
    assert repository.get_calls == repository.save_calls == 0


async def test_supported_event_with_invalid_schema_is_not_persisted():
    event = samples.capture_completed()
    del event["resource"]["amount"]
    service, _, repository = make_service()

    with pytest.raises(SchemaValidationError):
        await service.ingest(HEADERS, body_of(event))
    assert repository.save_calls == 0


async def test_normalization_failure_is_not_persisted():
    service, _, repository = make_service()

    with pytest.raises(NormalizationError):
        await service.ingest(HEADERS, body_of(samples.capture_completed(currency="ZZZ")))
    assert repository.save_calls == 0


# --- deduplication -------------------------------------------------------------

async def test_identical_redelivery_is_an_idempotent_duplicate():
    service, _, repository = make_service()
    raw_body = body_of(samples.capture_completed())
    first = await service.ingest(HEADERS, raw_body)

    second = await service.ingest(HEADERS, raw_body)

    assert first.outcome is PayPalIngestionOutcome.STORED
    assert second.outcome is PayPalIngestionOutcome.DUPLICATE
    assert second.source_event_id == samples.EVENT_ID
    assert second.payment_event is None
    assert repository.save_calls == 1
    assert len(repository.raw_records) == len(repository.payment_events) == 1


async def test_redelivery_with_different_formatting_is_still_a_duplicate():
    event = samples.capture_completed()
    service, _, repository = make_service()
    await service.ingest(HEADERS, body_of(event))

    reformatted = json.dumps(event, indent=2, sort_keys=True).encode("utf-8")
    result = await service.ingest(HEADERS, reformatted)

    assert result.outcome is PayPalIngestionOutcome.DUPLICATE
    assert repository.save_calls == 1


async def test_same_event_id_with_different_payload_conflicts_and_keeps_original():
    original = samples.capture_completed()
    service, _, repository = make_service()
    await service.ingest(HEADERS, body_of(original))
    stored = repository.raw_records[("paypal", samples.EVENT_ID)]

    with pytest.raises(DuplicateEventConflictError):
        await service.ingest(HEADERS, body_of(samples.capture_completed(value="999.00")))

    assert repository.raw_records[("paypal", samples.EVENT_ID)] is stored
    assert stored.raw_payload == original
    assert repository.save_calls == 1


async def test_lost_insert_race_with_identical_payload_is_a_duplicate():
    event = samples.capture_completed()
    repository = LostRaceRepository(winner_service_payload=event)
    service, _, _ = make_service(repository=repository)

    result = await service.ingest(HEADERS, body_of(event))

    assert result.outcome is PayPalIngestionOutcome.DUPLICATE
    assert repository.get_calls == 2


async def test_lost_insert_race_with_different_payload_is_a_conflict():
    repository = LostRaceRepository(winner_service_payload=samples.capture_completed(value="999.00"))
    service, _, _ = make_service(repository=repository)

    with pytest.raises(DuplicateEventConflictError):
        await service.ingest(HEADERS, body_of(samples.capture_completed()))


async def test_unique_violation_without_a_stored_raw_record_is_reraised():
    class CanonicalOnlyConflict(InMemoryIngestionRepository):
        async def save_ingestion(self, raw_record, payment_event):
            self.save_calls += 1
            raise DuplicateEventConflictError("payment_events unique constraint violated")

    service, _, repository = make_service(repository=CanonicalOnlyConflict())

    with pytest.raises(DuplicateEventConflictError):
        await service.ingest(HEADERS, body_of(samples.capture_completed()))
    assert repository.get_calls == 2


async def test_concurrent_identical_deliveries_store_once():
    repository = InMemoryIngestionRepository(read_barrier=2)
    service, _, _ = make_service(repository=repository)
    raw_body = body_of(samples.capture_completed())

    results = await asyncio.gather(service.ingest(HEADERS, raw_body), service.ingest(HEADERS, raw_body))

    assert sorted(r.outcome.value for r in results) == ["duplicate", "stored"]
    assert repository.save_calls == 2
    assert len(repository.raw_records) == len(repository.payment_events) == 1


async def test_concurrent_conflicting_deliveries_keep_the_winner_and_reject_the_other():
    repository = InMemoryIngestionRepository(read_barrier=2)
    service, _, _ = make_service(repository=repository)
    payloads = [samples.capture_completed(), samples.capture_completed(value="999.00")]

    results = await asyncio.gather(
        *(service.ingest(HEADERS, body_of(p)) for p in payloads), return_exceptions=True
    )

    stored = [i for i, r in enumerate(results) if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, DuplicateEventConflictError)]
    assert len(stored) == 1 and len(conflicts) == 1
    assert results[stored[0]].outcome is PayPalIngestionOutcome.STORED
    assert repository.save_calls == 2
    assert repository.raw_records[("paypal", samples.EVENT_ID)].raw_payload == payloads[stored[0]]


async def test_persistence_errors_propagate():
    class FailingRepository(InMemoryIngestionRepository):
        async def save_ingestion(self, raw_record, payment_event):
            raise PersistenceError("database unavailable")

    service, _, _ = make_service(repository=FailingRepository())
    with pytest.raises(PersistenceError):
        await service.ingest(HEADERS, body_of(samples.capture_completed()))


# --- shared hashing -------------------------------------------------------------

def test_razorpay_normalizer_reexports_the_shared_hash_function():
    assert razorpay_normalizer.generate_deterministic_hash is hashing.generate_deterministic_hash
