"""
Regression tests for the autouse default_session_guard in tests/conftest.py.

The application's default AsyncSessionLocal is bound to DATABASE_URL, which falls back
to the development database. No test may reach it. Each test below triggers the guard
deliberately, checks it fired, then resets the counter so teardown does not fail.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import src.core.services.live_pipeline as live_pipeline
import src.infrastructure.database as app_database
from src.api.dependencies import get_ingestion_service
from src.core.domain.models import PaymentEvent
from src.infrastructure.db_safety import UnsafeDatabaseError
from src.main import app


def _payment_event() -> PaymentEvent:
    now = datetime.now(timezone.utc)
    return PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="razorpay",
        source_event_id="evt_guard",
        payment_id="pay_guard",
        timestamp=now,
        event_type="payment.failed",
        currency="INR",
        amount_minor_units=1000,
        payment_status="failed",
        ingested_at=now,
    )


def test_guard_replaces_every_default_session_factory(default_session_guard):
    assert app_database.AsyncSessionLocal is default_session_guard
    assert live_pipeline.AsyncSessionLocal is default_session_guard


def test_get_db_session_is_blocked(default_session_guard):
    with pytest.raises(UnsafeDatabaseError):
        app_database.AsyncSessionLocal()
    assert default_session_guard.attempts == 1
    default_session_guard.attempts = 0


@pytest.mark.asyncio
async def test_app_dependency_is_blocked(default_session_guard):
    with pytest.raises(UnsafeDatabaseError):
        await app_database.get_db_session().__anext__()
    assert default_session_guard.attempts == 1
    default_session_guard.attempts = 0


@pytest.mark.asyncio
async def test_live_pipeline_without_session_is_blocked(default_session_guard):
    with pytest.raises(UnsafeDatabaseError):
        await live_pipeline.run_live_pipeline(_payment_event())
    assert default_session_guard.attempts == 1
    default_session_guard.attempts = 0


def test_unpatched_ingest_background_pipeline_is_caught(default_session_guard):
    """The original leak: an ingest request whose background pipeline is not mocked."""
    mock_service = AsyncMock()
    mock_service.last_event_is_duplicate = False
    mock_service.ingest_razorpay_webhook.return_value = _payment_event()
    previous = app.dependency_overrides.get(get_ingestion_service)
    app.dependency_overrides[get_ingestion_service] = lambda: mock_service
    try:
        try:
            TestClient(app).post(
                "/ingest/razorpay",
                data=b"{}",
                headers={"X-Razorpay-Signature": "sig", "X-Razorpay-Event-Id": "evt_guard"},
            )
        except UnsafeDatabaseError:
            pass
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_ingestion_service, None)
        else:
            app.dependency_overrides[get_ingestion_service] = previous
    assert default_session_guard.attempts == 1
    default_session_guard.attempts = 0
