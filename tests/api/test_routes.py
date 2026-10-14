from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from src.main import app
from src.api.dependencies import get_ingestion_service
from src.infrastructure.database import get_db_session

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
