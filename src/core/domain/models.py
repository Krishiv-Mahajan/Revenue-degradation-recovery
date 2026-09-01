from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from typing import Optional


class RawIngestionRecord(BaseModel):
    """
    Immutable source evidence representing the raw webhook/event received.
    """
    model_config = ConfigDict(frozen=True)

    source_system: str
    source_event_id: str
    received_at: datetime
    raw_payload: dict
    payload_hash: str


class PaymentEvent(BaseModel):
    """
    Normalized canonical payment fact.
    """
    model_config = ConfigDict(frozen=True)

    event_id: UUID
    source_system: str
    source_event_id: str
    
    payment_id: str
    order_id: Optional[str] = None
    
    timestamp: datetime
    event_type: str
    
    currency: str
    amount_minor_units: int
    
    payment_status: str
    
    payment_method: Optional[str] = None
    bank: Optional[str] = None
    wallet: Optional[str] = None
    
    error_code: Optional[str] = None
    error_description: Optional[str] = None
    error_source: Optional[str] = None
    error_step: Optional[str] = None
    error_reason: Optional[str] = None
    
    ingested_at: datetime
