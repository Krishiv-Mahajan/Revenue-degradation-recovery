import hashlib
import hmac
import json
import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError, InterfaceError
from sqlalchemy.ext.asyncio import async_sessionmaker
from src.main import app
from src.api.dependencies import (
    RAZORPAY_WEBHOOK_SECRET_ENV,
    get_ingestion_service,
    get_razorpay_webhook_secret,
)
from src.core.domain.exceptions import (
    DuplicateEventConflictError,
    InvalidWebhookSignatureError,
    MalformedInputError,
    NormalizationError,
    SchemaValidationError,
)
from src.core.services.ingestion_service import IngestionService
from src.infrastructure.database import engine as app_engine, get_db_session
from src.infrastructure.models import PaymentEventModel, RawIngestionRecordModel
from src.infrastructure.repository import SQLIngestionRepository

client = TestClient(app)

class MockIngestionService:
    async def ingest_razorpay_webhook(self, payload_body, signature_header, source_event_id):
        from src.core.domain.models import PaymentEvent
        import uuid
        from datetime import datetime, timezone
        
        return PaymentEvent(
            event_id=uuid.uuid4(),
            source_system="razorpay",
            source_event_id=source_event_id,
            payment_id="pay_123",
            timestamp=datetime.now(timezone.utc),
            event_type="test",
            currency="INR",
            amount_minor_units=1000,
            payment_status="failed",
            ingested_at=datetime.now(timezone.utc)
        )

app.dependency_overrides[get_ingestion_service] = MockIngestionService

@pytest.mark.asyncio
async def test_health_check(db_engine):
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    async def override_get_db_session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session
    try:
        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.get("/health")
    finally:
        app.dependency_overrides.pop(get_db_session, None)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_ingest_missing_event_id():
    response = client.post("/ingest/razorpay", data=b'{}', headers={"X-Razorpay-Signature": "sig"})
    assert response.status_code == 400
    assert "Missing X-Razorpay-Event-Id header" in response.json()["detail"]

def test_ingest_success():
    with patch("src.api.routes.run_live_pipeline") as mock_pipeline:
        response = client.post(
            "/ingest/razorpay", 
            data=b'{"some":"payload"}',
            headers={"X-Razorpay-Signature": "sig", "X-Razorpay-Event-Id": "evt_123"}
        )
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert "event_id" in response.json()
    mock_pipeline.assert_called_once()
    (scheduled_event,) = mock_pipeline.call_args.args
    assert str(scheduled_event.event_id) == response.json()["event_id"]


RAZORPAY_URL = "/ingest/razorpay"
RAZORPAY_TEST_SECRET = "razorpay-route-test-secret"
SENTINEL = "RAZORPAY_PAYLOAD_SENTINEL_buyer@example.com"


@pytest.fixture
def overrides():
    """Installs dependency overrides for one test and restores the previous ones afterwards."""
    previous = dict(app.dependency_overrides)
    yield app.dependency_overrides
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


@pytest.fixture
def pipeline_spy():
    with patch("src.api.routes.run_live_pipeline") as run_live_pipeline:
        yield run_live_pipeline
    run_live_pipeline.assert_not_called()


def _signed_razorpay_request(secret=RAZORPAY_TEST_SECRET):
    payment = {
        "id": "pay_log_hygiene", "entity": "payment", "amount": 50000, "currency": "INR",
        "status": "authorized", "method": "upi", "email": SENTINEL, "created_at": 1600000000,
    }
    body = json.dumps({
        "entity": "event", "account_id": "acc_log_hygiene", "event": "payment.authorized",
        "contains": ["payment"], "payload": {"payment": {"entity": payment}}, "created_at": 1600000000,
    }).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": "evt_log_hygiene"}


class _NoRowsResult:
    def scalars(self):
        return self

    def first(self):
        return None


class FailingCommitSession:
    """AsyncSession stand-in: no stored raw record, commit raises integrity_error, rollback optionally fails."""

    def __init__(self, integrity_error, rollback_error=None):
        self.integrity_error = integrity_error
        self.rollback_error = rollback_error

    async def execute(self, statement):
        return _NoRowsResult()

    def add(self, instance):
        pass

    async def commit(self):
        raise self.integrity_error

    async def rollback(self):
        if self.rollback_error is not None:
            raise self.rollback_error


def _assert_sentinel_absent(response, caplog):
    assert SENTINEL not in response.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        assert record.exc_info is None or SENTINEL not in logging.Formatter().formatException(record.exc_info)
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize(
    "rollback_error, expected_log",
    [
        (None, "Razorpay webhook persistence failed (PersistenceError)"),
        (InterfaceError("ROLLBACK", None, Exception("connection is closed")),
         "Unexpected error during Razorpay ingestion (InterfaceError)"),
    ],
    ids=["persistence-error", "unexpected-error"],
)
def test_razorpay_database_failure_never_leaks_sql_parameters(
    overrides, pipeline_spy, caplog, rollback_error, expected_log
):
    integrity_error = IntegrityError(
        "INSERT INTO raw_ingestion_records (source_system, source_event_id, received_at, raw_payload, "
        "payload_hash) VALUES ($1, $2, $3, $4, $5)",
        ("razorpay", "evt_log_hygiene", "2020-09-13T12:26:40Z",
         {"payload": {"payment": {"entity": {"email": SENTINEL}}}}, "hash"),
        Exception('new row violates check constraint "ck_unrelated"'),
    )
    assert SENTINEL in str(integrity_error)
    session = FailingCommitSession(integrity_error, rollback_error)
    overrides[get_ingestion_service] = lambda: IngestionService(
        SQLIngestionRepository(session), RAZORPAY_TEST_SECRET
    )
    body, headers = _signed_razorpay_request()
    caplog.set_level(logging.DEBUG)

    response = TestClient(app).post(RAZORPAY_URL, content=body, headers=headers)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    _assert_sentinel_absent(response, caplog)
    assert any(r.name == "src.api.routes" and r.getMessage() == expected_log for r in caplog.records)


@pytest.mark.parametrize(
    "error, expected_status, expected_detail",
    [
        (InvalidWebhookSignatureError(f"Signature mismatch for {SENTINEL}"), 401, "Invalid webhook signature"),
        (MalformedInputError(f"Invalid JSON payload near {SENTINEL}"), 400, "Malformed request"),
        (SchemaValidationError(f"input_value='{SENTINEL}'"), 422, "Unprocessable webhook event"),
        (NormalizationError(f"Invalid timestamp in Razorpay payload: {SENTINEL}"), 422,
         "Unprocessable webhook event"),
        (DuplicateEventConflictError(f"Event {SENTINEL} already exists"), 409, "Conflicting duplicate event"),
    ],
    ids=["signature", "malformed", "schema", "normalization", "conflict"],
)
def test_razorpay_rejections_return_fixed_details_and_log_type_only(
    overrides, pipeline_spy, caplog, error, expected_status, expected_detail
):
    class RaisingService:
        last_event_is_duplicate = False

        async def ingest_razorpay_webhook(self, payload_body, signature_header, source_event_id):
            raise error

    overrides[get_ingestion_service] = RaisingService
    body, headers = _signed_razorpay_request()
    caplog.set_level(logging.DEBUG)

    response = TestClient(app).post(RAZORPAY_URL, content=body, headers=headers)

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    _assert_sentinel_absent(response, caplog)
    assert any(
        r.name == "src.api.routes" and type(error).__name__ in r.getMessage() for r in caplog.records
    )


def test_app_engine_hides_sql_parameters_in_errors():
    assert app_engine.sync_engine.hide_parameters is True


class RecordingSession:
    """AsyncSession stand-in: no stored raw record; records added models and commits."""

    def __init__(self):
        self.added = []
        self.commits = 0

    async def execute(self, statement):
        return _NoRowsResult()

    def add(self, instance):
        self.added.append(instance)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


@pytest.fixture
def razorpay_intake(overrides):
    """Uses the real secret, service and repository wiring; returns every session opened."""
    overrides.pop(get_ingestion_service, None)
    overrides.pop(get_razorpay_webhook_secret, None)
    sessions = []

    async def recording_db_session():
        session = RecordingSession()
        sessions.append(session)
        yield session

    overrides[get_db_session] = recording_db_session
    return sessions


def _post_signed(secret):
    body, headers = _signed_razorpay_request(secret)
    return TestClient(app).post(RAZORPAY_URL, content=body, headers=headers)


@pytest.mark.parametrize(
    "configured, expected_problem",
    [
        (None, "is not set"),
        ("", "is not set"),
        ("   ", "is not set"),
        ("dummy_secret_for_local_dev", "is a known placeholder value"),
        ("your_razorpay_webhook_secret_here", "is a known placeholder value"),
        ("test_webhook_secret", "is a known placeholder value"),
        (" DUMMY_SECRET_FOR_LOCAL_DEV ", "is a known placeholder value"),
    ],
    ids=["missing", "empty", "blank", "legacy-default", "env-example", "readme-default", "placeholder-variant"],
)
def test_unusable_razorpay_secret_fails_closed(
    razorpay_intake, pipeline_spy, monkeypatch, caplog, configured, expected_problem
):
    if configured is None:
        monkeypatch.delenv(RAZORPAY_WEBHOOK_SECRET_ENV, raising=False)
    else:
        monkeypatch.setenv(RAZORPAY_WEBHOOK_SECRET_ENV, configured)
    caplog.set_level(logging.DEBUG)

    # Signed with the configured value itself: a forger who knows the placeholder must still be refused.
    response = _post_signed(configured or "")

    assert response.status_code == 503
    assert response.json() == {"detail": "Webhook intake temporarily unavailable"}
    assert razorpay_intake == []
    assert any(
        r.name == "src.api.dependencies"
        and r.getMessage() == f"Razorpay webhook intake disabled: {RAZORPAY_WEBHOOK_SECRET_ENV} {expected_problem}"
        for r in caplog.records
    )
    if configured and configured.strip():
        assert configured.strip() not in response.text
        assert configured.strip() not in caplog.text


def test_readme_example_secret_is_refused_before_signature_verification(
    razorpay_intake, pipeline_spy, monkeypatch, caplog
):
    readme_example = "replace_with_a_local_test_secret"
    monkeypatch.setenv(RAZORPAY_WEBHOOK_SECRET_ENV, readme_example)
    caplog.set_level(logging.DEBUG)

    with patch("src.core.services.ingestion_service.verify_razorpay_signature") as verify:
        response = _post_signed(readme_example)

    assert response.status_code == 503
    assert response.json() == {"detail": "Webhook intake temporarily unavailable"}
    assert razorpay_intake == []
    verify.assert_not_called()
    assert readme_example not in response.text
    assert readme_example not in caplog.text
    assert any(
        r.name == "src.api.dependencies"
        and r.getMessage() == f"Razorpay webhook intake disabled: {RAZORPAY_WEBHOOK_SECRET_ENV} is a known placeholder value"
        for r in caplog.records
    )


def test_configured_secret_accepts_correctly_signed_webhook(razorpay_intake, monkeypatch, caplog):
    monkeypatch.setenv(RAZORPAY_WEBHOOK_SECRET_ENV, RAZORPAY_TEST_SECRET)
    caplog.set_level(logging.DEBUG)

    with patch("src.api.routes.run_live_pipeline") as run_live_pipeline:
        response = _post_signed(RAZORPAY_TEST_SECRET)

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    [session] = razorpay_intake
    assert session.commits == 1
    assert [type(m) for m in session.added] == [RawIngestionRecordModel, PaymentEventModel]
    (scheduled_event,) = run_live_pipeline.call_args.args
    assert str(scheduled_event.event_id) == response.json()["event_id"]
    assert RAZORPAY_TEST_SECRET not in response.text
    assert RAZORPAY_TEST_SECRET not in caplog.text


def test_configured_secret_rejects_invalid_signature(razorpay_intake, pipeline_spy, monkeypatch, caplog):
    monkeypatch.setenv(RAZORPAY_WEBHOOK_SECRET_ENV, RAZORPAY_TEST_SECRET)
    caplog.set_level(logging.DEBUG)

    response = _post_signed("attacker-guessed-secret")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}
    [session] = razorpay_intake
    assert session.added == [] and session.commits == 0
    assert RAZORPAY_TEST_SECRET not in response.text
    assert RAZORPAY_TEST_SECRET not in caplog.text


def test_corrected_secret_takes_effect_without_restart(razorpay_intake, monkeypatch):
    monkeypatch.setenv(RAZORPAY_WEBHOOK_SECRET_ENV, "dummy_secret_for_local_dev")
    assert _post_signed("dummy_secret_for_local_dev").status_code == 503

    monkeypatch.setenv(RAZORPAY_WEBHOOK_SECRET_ENV, RAZORPAY_TEST_SECRET)
    with patch("src.api.routes.run_live_pipeline"):
        assert _post_signed(RAZORPAY_TEST_SECRET).status_code == 200
