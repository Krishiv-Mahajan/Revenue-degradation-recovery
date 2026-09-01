import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.api.dependencies import get_ingestion_service

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

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_ingest_missing_event_id():
    response = client.post("/ingest/razorpay", data=b'{}', headers={"X-Razorpay-Signature": "sig"})
    assert response.status_code == 400
    assert "Missing X-Razorpay-Event-Id header" in response.json()["detail"]

def test_ingest_success():
    response = client.post(
        "/ingest/razorpay", 
        data=b'{"some":"payload"}',
        headers={"X-Razorpay-Signature": "sig", "X-Razorpay-Event-Id": "evt_123"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert "event_id" in response.json()
