"""
Offline tests for POST /webhooks/paypal and the PayPalClient application lifecycle.
PayPal is simulated with httpx.MockTransport and persistence with an in-memory repository;
no test touches a database or the network.
"""
import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, InterfaceError

import src.api.dependencies as dependencies
from src.api.dependencies import get_paypal_ingestion_service
from src.api.routes import MAX_PAYPAL_WEBHOOK_BODY_BYTES
from src.core.domain.exceptions import PersistenceError
from src.core.services.paypal_ingestion_service import PayPalIngestionService
from src.infrastructure.database import get_db_session
from src.infrastructure.models import PaymentEventModel, RawIngestionRecordModel
from src.infrastructure.paypal_client import PayPalClient
from src.infrastructure.repository import SQLIngestionRepository
from src.main import app
from tests.unit import paypal_event_samples as samples
from tests.unit.test_paypal_ingestion_service import (
    ACCESS_TOKEN,
    CLIENT_SECRET,
    HEADERS,
    WEBHOOK_ID,
    InMemoryIngestionRepository,
    PayPalStub,
    body_of,
    paypal_client,
    unsupported_event,
)

URL = "/webhooks/paypal"
SENSITIVE_VALUES = (CLIENT_SECRET, ACCESS_TOKEN, WEBHOOK_ID, HEADERS["PAYPAL-TRANSMISSION-SIG"])
PAYPAL_ENV_VARS = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID", "PAYPAL_ENVIRONMENT")


@pytest.fixture
def overrides():
    """Installs dependency overrides for one test and restores the previous ones afterwards."""
    previous = dict(app.dependency_overrides)
    yield app.dependency_overrides
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


@pytest.fixture
def pipeline_spies():
    """Fails loudly if the route schedules the live pipeline or any background task."""
    with patch("src.api.routes.run_live_pipeline") as run_live_pipeline, \
            patch.object(BackgroundTasks, "add_task") as add_task:
        yield run_live_pipeline, add_task
    run_live_pipeline.assert_not_called()
    add_task.assert_not_called()


@pytest.fixture
def intake(overrides, pipeline_spies):
    """Wires the real service with a MockTransport PayPal client and an in-memory repository."""

    def build(stub=None, repository=None, **config_overrides):
        stub = stub or PayPalStub()
        repository = repository or InMemoryIngestionRepository()
        service = PayPalIngestionService(repository, paypal_client(stub, **config_overrides))
        overrides[get_paypal_ingestion_service] = lambda: service
        return TestClient(app), stub, repository

    return build


def _assert_safe(response):
    for value in SENSITIVE_VALUES:
        assert value not in response.text


# --- success and idempotency ----------------------------------------------------

def test_verified_event_is_stored_without_triggering_the_pipeline(intake, pipeline_spies):
    client, stub, repository = intake()

    response = client.post(URL, content=body_of(samples.capture_completed()), headers=HEADERS)

    assert response.status_code == 200
    stored = repository.payment_events[("paypal", samples.EVENT_ID)]
    assert response.json() == {"status": "stored", "event_id": str(stored.event_id)}
    assert len(repository.raw_records) == 1
    assert len(stub.verify_requests) == 1
    run_live_pipeline, add_task = pipeline_spies
    run_live_pipeline.assert_not_called()
    add_task.assert_not_called()


def test_identical_redelivery_is_acknowledged_as_duplicate(intake):
    client, _, repository = intake()
    raw_body = body_of(samples.capture_completed())
    client.post(URL, content=raw_body, headers=HEADERS)

    response = client.post(URL, content=raw_body, headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate"}
    assert repository.save_calls == 1


def test_conflicting_redelivery_returns_409_and_keeps_original(intake):
    client, _, repository = intake()
    original = samples.capture_completed()
    client.post(URL, content=body_of(original), headers=HEADERS)

    response = client.post(URL, content=body_of(samples.capture_completed(value="999.00")), headers=HEADERS)

    assert response.status_code == 409
    assert response.json() == {"detail": "Conflicting duplicate event"}
    assert repository.raw_records[("paypal", samples.EVENT_ID)].raw_payload == original


def test_verified_unsupported_event_is_acknowledged_but_not_stored(intake):
    client, stub, repository = intake()

    response = client.post(URL, content=body_of(unsupported_event()), headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert len(stub.verify_requests) == 1
    assert repository.raw_records == {} and repository.save_calls == 0


# --- rejected deliveries ---------------------------------------------------------

def test_failed_signature_returns_401_without_persistence(intake):
    client, _, repository = intake(PayPalStub(verify_status="FAILURE"))

    response = client.post(URL, content=body_of(samples.capture_completed()), headers=HEADERS)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}
    assert repository.get_calls == repository.save_calls == 0


def test_missing_transmission_headers_return_401_without_calling_paypal(intake):
    client, stub, repository = intake()

    response = client.post(URL, content=body_of(samples.capture_completed()))

    assert response.status_code == 401
    assert stub.requests == []
    assert repository.get_calls == 0


@pytest.mark.parametrize("raw_body", [b"not json", b'{"id": NaN}', b"[]"])
def test_malformed_body_returns_400(intake, raw_body):
    client, stub, repository = intake()

    response = client.post(URL, content=raw_body, headers=HEADERS)

    assert response.status_code == 400
    assert response.json() == {"detail": "Malformed request"}
    assert stub.requests == []
    assert repository.get_calls == 0


def test_deeply_nested_body_returns_400_without_calling_paypal(intake):
    client, stub, repository = intake()
    depth = 20_000
    raw_body = b'{"a":' * depth + b"1" + b"}" * depth
    assert len(raw_body) <= MAX_PAYPAL_WEBHOOK_BODY_BYTES

    response = client.post(URL, content=raw_body, headers=HEADERS)

    assert response.status_code == 400
    assert response.json() == {"detail": "Malformed request"}
    assert stub.requests == []
    assert repository.get_calls == repository.save_calls == 0


@pytest.mark.parametrize(
    "event",
    [samples.capture_completed(currency="ZZZ"), {**samples.capture_completed(), "resource": {"id": "x"}}],
    ids=["normalization", "schema"],
)
def test_unprocessable_supported_event_returns_422(intake, event):
    client, _, repository = intake()

    response = client.post(URL, content=body_of(event), headers=HEADERS)

    assert response.status_code == 422
    assert response.json() == {"detail": "Unprocessable webhook event"}
    assert repository.save_calls == 0


def test_declared_oversized_body_returns_413_before_verification(intake):
    client, stub, repository = intake()

    response = client.post(URL, content=b"x" * (MAX_PAYPAL_WEBHOOK_BODY_BYTES + 1), headers=HEADERS)

    assert response.status_code == 413
    assert stub.requests == []
    assert repository.get_calls == 0


def test_streamed_oversized_body_without_content_length_returns_413(intake):
    client, stub, _ = intake()
    chunk = b"x" * 65536
    chunks = iter([chunk] * (MAX_PAYPAL_WEBHOOK_BODY_BYTES // len(chunk) + 1))

    response = client.post(URL, content=chunks, headers=HEADERS)

    assert response.status_code == 413
    assert stub.requests == []


def test_body_at_the_limit_is_accepted_for_verification(intake):
    client, stub, _ = intake()
    event = samples.capture_completed()
    raw_body = body_of(event)
    padded = raw_body[:-1] + b" " * (MAX_PAYPAL_WEBHOOK_BODY_BYTES - len(raw_body)) + b"}"
    assert len(padded) == MAX_PAYPAL_WEBHOOK_BODY_BYTES

    response = client.post(URL, content=padded, headers=HEADERS)

    assert response.status_code == 200
    assert len(stub.verify_requests) == 1


# --- configuration and provider outages ------------------------------------------

def test_missing_configuration_fails_closed_without_opening_a_session(overrides, pipeline_spies):
    session_opened = MagicMock()

    async def tracking_session():
        session_opened()
        yield None

    overrides[get_db_session] = tracking_session
    app.state.paypal_client = None

    response = TestClient(app).post(URL, content=body_of(samples.capture_completed()), headers=HEADERS)

    assert response.status_code == 503
    assert response.json() == {"detail": "PayPal webhook intake is not configured"}
    session_opened.assert_not_called()


def test_missing_webhook_id_returns_503(intake):
    client, stub, repository = intake(webhook_id=None)

    response = client.post(URL, content=body_of(samples.capture_completed()), headers=HEADERS)

    assert response.status_code == 503
    assert stub.requests == []
    assert repository.get_calls == 0


def _connect_error(request):
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    "stub, expected_status, expected_detail",
    [
        (PayPalStub(verify_response=lambda r: httpx.Response(503)), 503,
         "Webhook verification temporarily unavailable"),
        (PayPalStub(verify_response=_connect_error), 503, "Webhook verification temporarily unavailable"),
        (PayPalStub(token_response=lambda r: httpx.Response(401, json={"error": "invalid_client"})), 503,
         "Webhook verification temporarily unavailable"),
        (PayPalStub(verify_response=lambda r: httpx.Response(
            400, json={"name": "VALIDATION_ERROR", "debug_id": "dbg-123", "details": [{"issue": WEBHOOK_ID}]})),
         400, "Webhook could not be verified"),
    ],
    ids=["paypal-5xx", "network-error", "auth-rejected", "verify-request-rejected"],
)
def test_provider_failures_map_to_safe_responses(intake, stub, expected_status, expected_detail):
    client, _, repository = intake(stub)

    response = client.post(URL, content=body_of(samples.capture_completed()), headers=HEADERS)

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert "dbg-123" not in response.text
    _assert_safe(response)
    assert repository.get_calls == repository.save_calls == 0


def test_persistence_failure_returns_generic_500(intake):
    class FailingRepository(InMemoryIngestionRepository):
        async def save_ingestion(self, raw_record, payment_event):
            raise PersistenceError("connection to db-host:5432 failed for user admin")

    client, _, _ = intake(repository=FailingRepository())

    response = client.post(URL, content=body_of(samples.capture_completed()), headers=HEADERS)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}


def test_persistence_failure_logs_never_include_the_exception_message(intake, caplog):
    sentinel = "PAYLOAD_SECRET_SENTINEL_buyer@example.com"

    class LeakyFailingRepository(InMemoryIngestionRepository):
        async def save_ingestion(self, raw_record, payment_event):
            raise PersistenceError(f"Database integrity error: [parameters: ({{'email': '{sentinel}'}},)]")

    client, _, _ = intake(repository=LeakyFailingRepository())
    caplog.set_level(logging.DEBUG)

    response = client.post(URL, content=body_of(samples.capture_completed()), headers=HEADERS)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert sentinel not in response.text
    for record in caplog.records:
        assert sentinel not in record.getMessage()
        assert record.exc_info is None or sentinel not in logging.Formatter().formatException(record.exc_info)
    assert sentinel not in caplog.text
    route_records = [r for r in caplog.records if r.name == "src.api.routes"]
    assert any("PersistenceError" in r.getMessage() for r in route_records)


class _NoRowsResult:
    def scalars(self):
        return self

    def first(self):
        return None


class CommitThenRollbackFailingSession:
    """AsyncSession stand-in: commit raises a sentinel-bearing IntegrityError, then rollback fails."""

    def __init__(self, integrity_error):
        self.integrity_error = integrity_error
        self.added = []
        self.calls = []

    async def execute(self, statement):
        self.calls.append("execute")
        return _NoRowsResult()

    def add(self, instance):
        self.added.append(instance)

    async def commit(self):
        self.calls.append("commit")
        raise self.integrity_error

    async def rollback(self):
        self.calls.append("rollback")
        raise InterfaceError("ROLLBACK", None, Exception("connection is closed"))


def test_unexpected_database_failure_logs_never_include_sql_parameters(intake, caplog):
    sentinel = "PAYLOAD_SECRET_SENTINEL_buyer@example.com"
    integrity_error = IntegrityError(
        "INSERT INTO raw_ingestion_records (source_system, source_event_id, received_at, raw_payload, "
        "payload_hash) VALUES ($1, $2, $3, $4, $5)",
        ("paypal", samples.EVENT_ID, "2024-05-16T05:19:19Z", {"payer": {"email_address": sentinel}}, "hash"),
        Exception('new row violates check constraint "ck_unrelated"'),
    )
    assert sentinel in str(integrity_error)
    session = CommitThenRollbackFailingSession(integrity_error)
    client, stub, repository = intake(repository=SQLIngestionRepository(session))
    assert type(repository) is SQLIngestionRepository
    caplog.set_level(logging.DEBUG)

    response = client.post(URL, content=body_of(samples.capture_completed()), headers=HEADERS)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert sentinel not in response.text
    assert len(stub.verify_requests) == 1
    assert session.calls == ["execute", "commit", "rollback"]
    assert [type(m) for m in session.added] == [RawIngestionRecordModel, PaymentEventModel]
    for record in caplog.records:
        assert sentinel not in record.getMessage()
        assert record.exc_info is None or sentinel not in logging.Formatter().formatException(record.exc_info)
    assert sentinel not in caplog.text
    assert any(
        r.name == "src.api.routes" and r.getMessage() == "Unexpected error during PayPal ingestion (InterfaceError)"
        for r in caplog.records
    )


# --- application lifecycle ----------------------------------------------------------

def test_lifespan_builds_client_from_environment_and_closes_it(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "lifespan-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "lifespan-client-secret")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "lifespan-webhook-id")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")

    with TestClient(app):
        created = app.state.paypal_client
        assert isinstance(created, PayPalClient)
        assert not created._http.is_closed

    assert created._http.is_closed
    assert app.state.paypal_client is None


def test_lifespan_without_configuration_fails_closed(monkeypatch, overrides, pipeline_spies):
    for name in PAYPAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    overrides[get_db_session] = lambda: None

    with TestClient(app) as client:
        assert app.state.paypal_client is None
        response = client.post(URL, content=body_of(samples.capture_completed()), headers=HEADERS)

    assert response.status_code == 503
    assert response.json() == {"detail": "PayPal webhook intake is not configured"}


def test_lifespan_client_is_shared_across_requests(monkeypatch, overrides, pipeline_spies):
    stub = PayPalStub()
    factory = MagicMock(return_value=paypal_client(stub))
    monkeypatch.setattr(dependencies, "create_paypal_client", factory)

    async def unused_session():
        yield None

    overrides[get_db_session] = unused_session

    with TestClient(app) as client:
        for _ in range(2):
            response = client.post(URL, content=body_of(unsupported_event()), headers=HEADERS)
            assert response.json() == {"status": "ignored"}

    factory.assert_called_once_with()
    assert len(stub.verify_requests) == 2
    assert len([r for r in stub.requests if r.url.path == "/v1/oauth2/token"]) == 1
