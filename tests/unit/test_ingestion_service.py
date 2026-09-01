import pytest
import json
from unittest.mock import AsyncMock, patch
from src.core.services.ingestion_service import IngestionService
from src.core.domain.models import RawIngestionRecord
from src.core.domain.exceptions import DuplicateEventConflictError

@pytest.fixture
def repo_mock():
    repo = AsyncMock()
    repo.get_raw_record.return_value = None
    repo.save_ingestion.return_value = None
    return repo

@pytest.fixture
def service(repo_mock):
    return IngestionService(repo_mock, "secret")

@pytest.mark.asyncio
@patch("src.core.services.ingestion_service.verify_razorpay_signature")
async def test_ingest_new_event(mock_verify, service, repo_mock):
    mock_verify.return_value = None
    payload = b'{"entity": "event", "account_id": "acc_123", "event": "payment.failed", "contains": ["payment"], "payload": {"payment": {"entity": {"id": "pay_123", "entity": "payment", "amount": 100, "currency": "INR", "status": "failed", "created_at": 1600000000}}}, "created_at": 1600000000}'
    
    event = await service.ingest_razorpay_webhook(payload, "sig", "evt_123")
    
    assert event.event_type == "payment.failed"
    repo_mock.get_raw_record.assert_called_once_with(source_system="razorpay", source_event_id="evt_123")
    repo_mock.save_ingestion.assert_called_once()

@pytest.mark.asyncio
@patch("src.core.services.ingestion_service.verify_razorpay_signature")
async def test_ingest_idempotent_retry(mock_verify, service, repo_mock):
    mock_verify.return_value = None
    payload = b'{"entity": "event", "account_id": "acc_123", "event": "payment.failed", "contains": ["payment"], "payload": {"payment": {"entity": {"id": "pay_123", "entity": "payment", "amount": 100, "currency": "INR", "status": "failed", "created_at": 1600000000}}}, "created_at": 1600000000}'
    
    from src.core.normalizers.razorpay import generate_deterministic_hash
    hash_val = generate_deterministic_hash(json.loads(payload))
    
    # Mock existing record with SAME hash
    existing_record = RawIngestionRecord(
        source_system="razorpay",
        source_event_id="evt_123",
        received_at="2023-01-01T00:00:00Z",
        raw_payload={},
        payload_hash=hash_val
    )
    repo_mock.get_raw_record.return_value = existing_record
    
    event = await service.ingest_razorpay_webhook(payload, "sig", "evt_123")
    assert event.event_type == "payment.failed"
    repo_mock.save_ingestion.assert_not_called()

@pytest.mark.asyncio
@patch("src.core.services.ingestion_service.verify_razorpay_signature")
async def test_ingest_conflict_retry(mock_verify, service, repo_mock):
    mock_verify.return_value = None
    payload = b'{"entity": "event", "account_id": "acc_123", "event": "payment.failed", "contains": ["payment"], "payload": {"payment": {"entity": {"id": "pay_123", "entity": "payment", "amount": 100, "currency": "INR", "status": "failed", "created_at": 1600000000}}}, "created_at": 1600000000}'
    
    # Mock existing record with DIFFERENT hash
    existing_record = RawIngestionRecord(
        source_system="razorpay",
        source_event_id="evt_123",
        received_at="2023-01-01T00:00:00Z",
        raw_payload={},
        payload_hash="different_hash"
    )
    repo_mock.get_raw_record.return_value = existing_record
    
    with pytest.raises(DuplicateEventConflictError):
        await service.ingest_razorpay_webhook(payload, "sig", "evt_123")
