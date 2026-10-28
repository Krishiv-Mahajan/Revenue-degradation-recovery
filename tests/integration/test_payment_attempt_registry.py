"""
Database-backed tests for the payment_attempts registry (PaymentAttemptRepository) and the live
pipeline's registration step, against the disposable test database only.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.domain.models import PaymentEvent
from src.core.domain.payment_attempt_exceptions import (
    AttemptNotRegisteredError,
    AttemptProviderConflictError,
    UnsupportedProviderError,
)
from src.core.domain.payment_attempts import AttemptRef
from src.core.services import live_pipeline
from src.infrastructure.models import FailurePredictionModel, PaymentAttemptModel, PaymentEventModel
from src.infrastructure.payment_attempt_repository import PaymentAttemptRepository

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def _event(source_system, event_type, payment_id, at=T0, order_id=None):
    return PaymentEvent(
        event_id=uuid.uuid4(), source_system=source_system, source_event_id=f"evt_{uuid.uuid4().hex}",
        payment_id=payment_id, order_id=order_id, timestamp=at, event_type=event_type, currency="INR",
        amount_minor_units=50000, payment_status=event_type.split(".")[-1], ingested_at=at,
    )


async def _store(session, event: PaymentEvent) -> PaymentEvent:
    session.add(PaymentEventModel(**event.model_dump()))
    await session.flush()
    return event


async def _registry_rows(session):
    return (await session.execute(select(PaymentAttemptModel))).scalars().all()


async def test_registration_is_idempotent_and_keeps_the_first_trigger(db_session: AsyncSession):
    first = await _store(db_session, _event("razorpay", "payment.authorized", "pay_Reg000000001"))
    later = await _store(db_session, _event("razorpay", "payment.authorized", "pay_Reg000000001", at=T0 + timedelta(minutes=1)))
    repo = PaymentAttemptRepository(db_session)

    registered = await repo.register(first)
    again = await repo.register(later)

    assert registered.ref == again.ref == AttemptRef("razorpay", "pay_Reg000000001", "pay_Reg000000001")
    assert registered.trigger_event_id == again.trigger_event_id == first.event_id
    [row] = await _registry_rows(db_session)
    assert (row.source_system, row.provider_reference, row.trigger_event_id) == ("razorpay", "pay_Reg000000001", first.event_id)


async def test_a_duplicate_paypal_approval_leaves_one_row(db_session: AsyncSession):
    first = await _store(db_session, _event("paypal", "order.approved", "ORDERREG0001"))
    duplicate = await _store(db_session, _event("paypal", "order.approved", "ORDERREG0001", at=T0 + timedelta(seconds=5)))
    repo = PaymentAttemptRepository(db_session)

    await repo.register(first)
    registered = await repo.register(duplicate)

    assert registered.ref == AttemptRef("paypal", "paypal-order:ORDERREG0001", "ORDERREG0001")
    assert registered.trigger_event_id == first.event_id
    assert len(await _registry_rows(db_session)) == 1


async def test_a_key_registered_under_another_provider_is_refused_and_left_untouched(db_session: AsyncSession):
    razorpay = await _store(db_session, _event("razorpay", "payment.authorized", "pay_Reg000000002"))
    synthetic = await _store(db_session, _event("synthetic", "payment.authorized", "pay_Reg000000002"))
    repo = PaymentAttemptRepository(db_session)
    await repo.register(razorpay)

    with pytest.raises(AttemptProviderConflictError, match="registered as razorpay"):
        await repo.register(synthetic)
    with pytest.raises(AttemptProviderConflictError):
        await repo.require(AttemptRef("synthetic", "pay_Reg000000002", "pay_Reg000000002"))

    [row] = await _registry_rows(db_session)
    assert (row.source_system, row.trigger_event_id) == ("razorpay", razorpay.event_id)


async def test_unsupported_providers_are_refused_before_any_insert(db_session: AsyncSession):
    event = await _store(db_session, _event("test_system", "payment.authorized", "pay_Reg000000003"))
    with pytest.raises(UnsupportedProviderError):
        await PaymentAttemptRepository(db_session).register(event)
    assert await _registry_rows(db_session) == []


async def test_require_reports_a_missing_attempt(db_session: AsyncSession):
    with pytest.raises(AttemptNotRegisteredError, match="pay_Reg000000004"):
        await PaymentAttemptRepository(db_session).require(AttemptRef("razorpay", "pay_Reg000000004", "pay_Reg000000004"))


async def test_the_database_refuses_a_trigger_event_that_was_never_stored(db_session: AsyncSession):
    never_stored = _event("razorpay", "payment.authorized", "pay_Reg000000005")
    with pytest.raises(IntegrityError, match="fk_payment_attempt_trigger"):
        await PaymentAttemptRepository(db_session).register(never_stored)


async def test_concurrent_registrations_produce_one_row(db_engine):
    Session = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        trigger = await _store(session, _event("synthetic", "payment.authorized", "pay_Reg000000006"))
        await session.commit()

    async def register():
        async with Session() as session:
            registered = await PaymentAttemptRepository(session).register(trigger)
            await session.commit()
            return registered

    first, second = await asyncio.gather(register(), register())

    assert first.ref == second.ref
    async with Session() as session:
        assert len(await _registry_rows(session)) == 1


# --- live pipeline registration --------------------------------------------------------------------

def _use_session(monkeypatch, db_engine):
    monkeypatch.setattr(
        live_pipeline, "AsyncSessionLocal", async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    )


async def _count(db_engine, model):
    async with async_sessionmaker(bind=db_engine, class_=AsyncSession)() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_registration_rolls_back_when_stage_5_fails(db_engine, monkeypatch):
    _use_session(monkeypatch, db_engine)
    Session = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        trigger = await _store(session, _event("razorpay", "payment.authorized", "pay_Reg000000007"))
        await session.commit()

    async def fail(self, attempt, T):
        raise RuntimeError("stage 5 failure after registration")

    monkeypatch.setattr(live_pipeline.FailurePredictionService, "orchestrate_prediction", fail)
    outcome = await live_pipeline.run_live_pipeline(trigger)

    assert (outcome["status"], outcome["failed_stage"]) == ("failed", "STAGE_5_PREDICTION")
    assert await _count(db_engine, PaymentAttemptModel) == 0
    assert await _count(db_engine, PaymentEventModel) == 1


async def test_a_conflicting_trigger_is_refused_and_stage_1_data_stays_committed(db_engine, monkeypatch):
    _use_session(monkeypatch, db_engine)
    Session = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        razorpay = await _store(session, _event("razorpay", "payment.authorized", "pay_Reg000000008"))
        synthetic = await _store(session, _event("synthetic", "payment.authorized", "pay_Reg000000008"))
        await PaymentAttemptRepository(session).register(razorpay)
        await session.commit()

    outcome = await live_pipeline.run_live_pipeline(synthetic)

    assert (outcome["status"], outcome["payment_attempt_id"], outcome["prediction"]) == ("success", None, None)
    assert await _count(db_engine, FailurePredictionModel) == 0
    assert await _count(db_engine, PaymentEventModel) == 2
    async with Session() as session:
        [row] = await _registry_rows(session)
    assert (row.source_system, row.trigger_event_id) == ("razorpay", razorpay.event_id)
