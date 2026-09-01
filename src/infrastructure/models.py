from sqlalchemy import Column, String, Integer, DateTime, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base
import uuid

Base = declarative_base()

class RawIngestionRecordModel(Base):
    __tablename__ = "raw_ingestion_records"

    # We use a synthetic UUID primary key for the table to keep clustering simple,
    # but the domain identity is (source_system, source_event_id).
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_system = Column(String(50), nullable=False)
    source_event_id = Column(String(255), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    raw_payload = Column(JSON, nullable=False)
    payload_hash = Column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint('source_system', 'source_event_id', name='uq_raw_ingestion_source_identity'),
    )


class PaymentEventModel(Base):
    __tablename__ = "payment_events"

    # The canonical domain event_id
    event_id = Column(UUID(as_uuid=True), primary_key=True)
    
    # Domain identity mapped from source
    source_system = Column(String(50), nullable=False)
    source_event_id = Column(String(255), nullable=False)
    
    # Core payment facts
    payment_id = Column(String(255), nullable=False)
    order_id = Column(String(255), nullable=True)
    
    timestamp = Column(DateTime(timezone=True), nullable=False)
    event_type = Column(String(100), nullable=False)
    
    currency = Column(String(3), nullable=False)
    amount_minor_units = Column(Integer, nullable=False)
    
    payment_status = Column(String(50), nullable=False)
    
    # Razorpay-specific / Provider error details, normalized loosely
    error_code = Column(String(255), nullable=True)
    error_description = Column(String(1024), nullable=True)
    error_source = Column(String(255), nullable=True)
    error_step = Column(String(255), nullable=True)
    error_reason = Column(String(255), nullable=True)
    
    ingested_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint('source_system', 'source_event_id', name='uq_payment_event_source_identity'),
    )
