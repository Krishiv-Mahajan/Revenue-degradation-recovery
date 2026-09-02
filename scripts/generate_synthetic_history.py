import asyncio
import uuid
import random
import logging
import os
from datetime import datetime, timezone, timedelta

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from src.infrastructure.models import Base, RawIngestionRecordModel, PaymentEventModel
from src.core.normalizers.razorpay import generate_deterministic_hash

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def generate_dataset(engine_url=DATABASE_URL, num_days=21, events_per_day=500):
    """
    Generates a deterministic synthetic dataset for pipeline validation.
    DO NOT USE FOR EMPIRICAL TRAINING.
    """
    logger.info("SYNTHETIC / DEVELOPMENT - Starting generation of synthetic payment lifecycles")
    random.seed(42) # Deterministic seeded generation

    engine = create_async_engine(engine_url, echo=False)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    
    # Establish time bounds - fix the end date so it is deterministic across tests
    end_date = datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc)
    start_date = end_date - timedelta(days=num_days)
    
    payment_methods = ["UPI", "card", "netbanking", "wallet"]
    currencies = ["INR"]
    
    # We will generate a series of lifecycles
    async with Session() as session:
        batch = []
        for day in range(num_days):
            current_day_start = start_date + timedelta(days=day)
            
            for i in range(events_per_day):
                # Randomize time within the day
                offset_seconds = random.randint(0, 86399)
                event_time = current_day_start + timedelta(seconds=offset_seconds)
                ingested_at = event_time + timedelta(seconds=random.randint(1, 5))
                
                payment_id = f"pay_syn_{uuid.uuid4().hex[:8]}"
                pm = random.choice(payment_methods)
                curr = random.choice(currencies)
                amount = random.randint(1000, 50000)
                
                # 1. Authorized event
                auth_id = f"evt_syn_auth_{uuid.uuid4().hex[:8]}"
                auth_payload = {"event": "payment.authorized", "payload": {"payment": {"entity": {"id": payment_id}}}}
                
                raw_auth = RawIngestionRecordModel(
                    source_system="synthetic",
                    source_event_id=auth_id,
                    received_at=ingested_at,
                    raw_payload=auth_payload,
                    payload_hash=generate_deterministic_hash(auth_payload)
                )
                
                evt_auth = PaymentEventModel(
                    event_id=uuid.uuid4(),
                    source_system="synthetic",
                    source_event_id=auth_id,
                    payment_id=payment_id,
                    timestamp=event_time,
                    event_type="payment.authorized",
                    currency=curr,
                    amount_minor_units=amount,
                    payment_status="authorized",
                    payment_method=pm,
                    ingested_at=ingested_at
                )
                
                batch.extend([raw_auth, evt_auth])
                
                # Terminal outcome (80% capture, 20% fail)
                # Ensure terminal time is strictly after auth time
                terminal_offset = timedelta(seconds=random.randint(10, 1800))
                term_event_time = event_time + terminal_offset
                term_ingested_at = term_event_time + timedelta(seconds=random.randint(1, 5))
                
                is_fail = random.random() < 0.2
                term_type = "payment.failed" if is_fail else "payment.captured"
                term_status = "failed" if is_fail else "captured"
                
                term_id = f"evt_syn_term_{uuid.uuid4().hex[:8]}"
                term_payload = {"event": term_type, "payload": {"payment": {"entity": {"id": payment_id}}}}
                
                raw_term = RawIngestionRecordModel(
                    source_system="synthetic",
                    source_event_id=term_id,
                    received_at=term_ingested_at,
                    raw_payload=term_payload,
                    payload_hash=generate_deterministic_hash(term_payload)
                )
                
                evt_term = PaymentEventModel(
                    event_id=uuid.uuid4(),
                    source_system="synthetic",
                    source_event_id=term_id,
                    payment_id=payment_id,
                    timestamp=term_event_time,
                    event_type=term_type,
                    currency=curr,
                    amount_minor_units=amount,
                    payment_status=term_status,
                    payment_method=pm,
                    ingested_at=term_ingested_at
                )
                
                batch.extend([raw_term, evt_term])
                
                if len(batch) >= 2000:
                    session.add_all(batch)
                    await session.commit()
                    batch = []
                    
        if batch:
            session.add_all(batch)
            await session.commit()
            
    await engine.dispose()
    logger.info("SYNTHETIC / DEVELOPMENT - Finished generation.")

if __name__ == "__main__":
    asyncio.run(generate_dataset())
