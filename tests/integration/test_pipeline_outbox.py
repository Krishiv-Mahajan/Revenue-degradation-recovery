"""
Durable pipeline runs against PostgreSQL: the outbox row is written with the event, a lost background
task is replayed exactly once by the reconciler, failures are retried up to the attempt limit, and an
abandoned claim is reclaimed after its lease. The real ingestion repository, live pipeline and
reconciler run; only live_pipeline's session factory is bound to the test database.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.domain.models import PaymentEvent, RawIngestionRecord
from src.core.domain.pipeline_runs import MAX_PIPELINE_ATTEMPTS, PENDING_GRACE, RUNNING_LEASE
from src.core.services import live_pipeline
from src.core.services.pipeline_reconciler import reconcile_pipeline_runs
from src.infrastructure.models import FailurePredictionModel, InterventionDecisionModel, PipelineRunModel
from src.infrastructure.repository import SQLIngestionRepository


@pytest.fixture
def Session(db_engine, monkeypatch):
    factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(live_pipeline, "AsyncSessionLocal", factory)
    return factory


def _event(source="razorpay", event_type="payment.authorized", payment_id=None, order_id=None, at=None):
    at = at or datetime.now(timezone.utc)
    return PaymentEvent(
        event_id=uuid.uuid4(), source_system=source, source_event_id=f"evt_{uuid.uuid4().hex}",
        payment_id=payment_id or f"pay_outbox_{uuid.uuid4().hex[:10]}", order_id=order_id, timestamp=at,
        event_type=event_type, currency="INR", amount_minor_units=50000,
        payment_status=event_type.split(".")[-1], payment_method="card", ingested_at=at,
    )


async def _ingest(Session, event):
    raw = RawIngestionRecord(
        source_system=event.source_system, source_event_id=event.source_event_id, received_at=event.ingested_at,
        raw_payload={"id": event.source_event_id}, payload_hash=uuid.uuid4().hex,
    )
    async with Session() as session:
        await SQLIngestionRepository(session).save_ingestion(raw, event)


async def _run(Session, event_id):
    async with Session() as session:
        return await session.get(PipelineRunModel, event_id)


async def _count(Session, model, *conditions):
    async with Session() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions))


async def test_the_pending_run_is_stored_with_the_event(Session):
    event = _event()
    await _ingest(Session, event)

    run = await _run(Session, event.event_id)
    assert (run.source_system, run.status, run.attempts, run.created_at, run.completed_at) == (
        "razorpay", "PENDING", 0, event.ingested_at, None,
    )


async def test_events_that_need_no_pipeline_get_no_run(Session):
    refund = _event(source="paypal", event_type="payment.refunded", payment_id="CAPTUREOUTBOX1", order_id="ORDEROUTBOX1")
    await _ingest(Session, refund)
    assert await _run(Session, refund.event_id) is None


async def test_a_lost_background_task_is_replayed_exactly_once(Session):
    event = _event()
    await _ingest(Session, event)  # the background task never runs (process crashed)

    # Within the grace period the run is left to the background task.
    assert (await reconcile_pipeline_runs(now=event.ingested_at + PENDING_GRACE / 2))["due"] == 0

    first = await reconcile_pipeline_runs(now=event.ingested_at + PENDING_GRACE + timedelta(seconds=1))
    assert first == {"due": 1, "success": 1}
    run = await _run(Session, event.event_id)
    assert (run.status, run.attempts, run.completed_at is not None) == ("SUCCEEDED", 1, True)
    pid = event.payment_id
    assert await _count(Session, FailurePredictionModel, FailurePredictionModel.payment_attempt_id == pid) == 1
    assert await _count(Session, InterventionDecisionModel, InterventionDecisionModel.payment_attempt_id == pid) == 1

    # Nothing is due any more, and a late background task is skipped rather than run twice.
    assert (await reconcile_pipeline_runs(now=datetime.now(timezone.utc) + timedelta(hours=1)))["due"] == 0
    assert (await live_pipeline.run_live_pipeline(event))["status"] == "skipped"
    assert await _count(Session, FailurePredictionModel, FailurePredictionModel.payment_attempt_id == pid) == 1


async def test_a_failed_run_is_retried_and_records_only_the_error_type(Session, monkeypatch):
    event = _event()
    await _ingest(Session, event)
    real = live_pipeline._execute_live_pipeline
    calls = []

    async def fail_once(session, payment_event):
        calls.append(payment_event.event_id)
        if len(calls) == 1:
            await session.rollback()
            return {"status": "failed", "payment_id": payment_event.payment_id, "failed_stage": "STAGE_5_PREDICTION",
                    "error": "secret detail with SQL parameters", "error_type": "OperationalError"}
        return await real(session, payment_event)

    monkeypatch.setattr(live_pipeline, "_execute_live_pipeline", fail_once)

    assert (await live_pipeline.run_live_pipeline(event))["status"] == "failed"
    run = await _run(Session, event.event_id)
    assert (run.status, run.attempts, run.last_failed_stage, run.last_error_type) == (
        "FAILED", 1, "STAGE_5_PREDICTION", "OperationalError",
    )

    assert await reconcile_pipeline_runs(now=datetime.now(timezone.utc)) == {"due": 1, "success": 1}
    run = await _run(Session, event.event_id)
    assert (run.status, run.attempts) == ("SUCCEEDED", 2)


async def test_exhausted_runs_are_never_claimed_again(Session):
    event = _event()
    await _ingest(Session, event)
    async with Session() as session:
        await session.execute(
            update(PipelineRunModel).where(PipelineRunModel.event_id == event.event_id)
            .values(status="FAILED", attempts=MAX_PIPELINE_ATTEMPTS)
        )
        await session.commit()

    assert (await reconcile_pipeline_runs(now=datetime.now(timezone.utc) + timedelta(days=1)))["due"] == 0
    assert (await live_pipeline.run_live_pipeline(event))["status"] == "skipped"
    assert (await _run(Session, event.event_id)).attempts == MAX_PIPELINE_ATTEMPTS


async def test_an_abandoned_claim_is_reclaimed_only_after_its_lease(Session):
    event = _event()
    await _ingest(Session, event)

    async def claimed(age):
        async with Session() as session:
            await session.execute(
                update(PipelineRunModel).where(PipelineRunModel.event_id == event.event_id)
                .values(status="RUNNING", attempts=1, updated_at=datetime.now(timezone.utc) - age)
            )
            await session.commit()

    # A worker still inside its lease keeps the run: neither the reconciler nor a second task takes it.
    await claimed(RUNNING_LEASE / 2)
    assert (await reconcile_pipeline_runs())["due"] == 0
    assert (await live_pipeline.run_live_pipeline(event))["status"] == "skipped"

    # The worker died: once the lease has passed the run is reclaimed and completed.
    await claimed(RUNNING_LEASE + timedelta(seconds=1))
    assert await reconcile_pipeline_runs() == {"due": 1, "success": 1}
    run = await _run(Session, event.event_id)
    assert (run.status, run.attempts) == ("SUCCEEDED", 2)


async def test_an_event_stored_before_the_outbox_runs_untracked(Session):
    event = _event()
    await _ingest(Session, event)
    async with Session() as session:
        await session.delete(await session.get(PipelineRunModel, event.event_id))
        await session.commit()

    assert (await live_pipeline.run_live_pipeline(event))["status"] == "success"
    assert await _run(Session, event.event_id) is None


async def test_the_outbox_row_requires_its_event(Session):
    from sqlalchemy.exc import IntegrityError

    from src.infrastructure.pipeline_run_repository import new_pending_run

    async with Session() as session:
        session.add(new_pending_run(uuid.uuid4(), "razorpay", datetime.now(timezone.utc)))
        with pytest.raises(IntegrityError) as excinfo:
            await session.commit()
    assert "fk_pipeline_run_event" in str(excinfo.value)
