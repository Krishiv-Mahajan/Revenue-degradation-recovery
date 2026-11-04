"""
Database-backed tests for Phase 3 Revision G (d0e1f2a3b4c5, payment method switch offers), run only with
RUN_MIGRATION_TESTS=1 against localhost:5434/payment_recovery_test. Exact F <-> G round trip, offer
constraints enforced by the database, and a downgrade that refuses (changing nothing) while offers exist.
"""
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from tests.migration.conftest import REVISION_F, REVISION_G
from tests.migration.test_revision_b import (
    T0, columns_by_table, data_hashes, insert, scalar, schema_snapshot, seed_full_trace, version,
)


def offer_values(trace, key, **overrides):
    values = dict(
        offer_id=uuid.uuid4(), command_id=trace["cmd_id"], decision_id=trace["dec_id"],
        execution_attempt_id=trace["attempt_id"], payment_attempt_id=key, source_system="razorpay",
        alternative_provider="paypal", environment="sandbox", checkout_order_id=f"ORDER{uuid.uuid4().hex[:10].upper()}",
        approve_url="https://www.sandbox.paypal.com/checkoutnow?token=X", amount_minor_units=2000, currency="USD",
        status="OFFERED", offered_at=T0, expires_at=T0 + timedelta(minutes=30), presentation=json.dumps({}),
        created_at=T0, updated_at=T0,
    )
    values.update(overrides)
    return values


async def seed_offer(conn, key="pay_revg_rzp001", **overrides):
    trace = await seed_full_trace(conn, source="razorpay", key=key)
    values = offer_values(trace, key, **overrides)
    await insert(conn, "payment_method_switch_offers", **values)
    await insert(
        conn, "payment_method_switch_offer_events",
        offer_event_id=uuid.uuid4(), offer_id=values["offer_id"], sequence=1, event_type="OFFER_CREATED",
        source="EXECUTOR", to_status="OFFERED", occurred_at=T0, recorded_at=T0, details=json.dumps({}),
    )
    return trace, values


async def test_upgrade_to_revision_g_and_downgrade_roundtrip(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_F)
    async with engine.begin() as conn:
        await seed_full_trace(conn, source="razorpay", key="pay_revg_rzp001")

    schema_f = await schema_snapshot(engine)
    hashes_f = await data_hashes(engine, columns_by_table(schema_f))

    await alembic.ok("upgrade", REVISION_G)
    assert await version(engine) == REVISION_G
    assert await data_hashes(engine, columns_by_table(schema_f)) == hashes_f
    assert await scalar(engine, "SELECT count(*) FROM payment_method_switch_offers") == 0

    await alembic.ok("downgrade", REVISION_F)
    assert await schema_snapshot(engine) == schema_f
    assert await data_hashes(engine, columns_by_table(schema_f)) == hashes_f

    await alembic.ok("upgrade", REVISION_G)
    assert await version(engine) == REVISION_G


@pytest.mark.parametrize("overrides, constraint", [
    ({"environment": "live"}, "ck_switch_offer_sandbox_only"),
    ({"alternative_provider": "razorpay"}, "ck_switch_offer_alternative_provider"),
    ({"amount_minor_units": 0}, "ck_switch_offer_amount_positive"),
    ({"status": "CAPTURED_BY_ENGINE"}, "ck_switch_offer_status"),
    ({"status": "ACCEPTED"}, "ck_switch_offer_acceptance"),
    ({"status": "COMPLETED", "accepted_at": T0, "completion_deadline": T0 + timedelta(hours=1), "resolved_at": T0},
     "ck_switch_offer_completion_event"),
    ({"status": "DECLINED"}, "ck_switch_offer_resolution"),
    ({"expires_at": T0}, "ck_switch_offer_expiry_after_offer"),
])
async def test_offer_invariants_are_enforced_by_the_database(migration_db, overrides, constraint):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_G)
    with pytest.raises(IntegrityError, match=constraint):
        async with engine.begin() as conn:
            await seed_offer(conn, **overrides)


async def test_an_offer_must_change_provider_and_belong_to_its_own_attempt(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_G)
    async with engine.begin() as conn:
        paypal_trace = await seed_full_trace(conn, source="paypal", key="paypal-order:REVGPAYPAL1", reference="REVGPAYPAL1")
        other_trace = await seed_full_trace(conn, source="razorpay", key="pay_revg_other")
    with pytest.raises(IntegrityError, match="ck_switch_offer_changes_provider"):
        async with engine.begin() as conn:
            await insert(conn, "payment_method_switch_offers", **offer_values(
                paypal_trace, "paypal-order:REVGPAYPAL1", source_system="paypal",
            ))
    with pytest.raises(IntegrityError, match="fk_switch_offer_command_attempt"):
        async with engine.begin() as conn:
            trace = await seed_full_trace(conn, source="razorpay", key="pay_revg_mine")
            await insert(conn, "payment_method_switch_offers", **offer_values(
                {**trace, "cmd_id": other_trace["cmd_id"]}, "pay_revg_mine",
            ))


async def test_one_offer_per_command_and_per_checkout_order(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_G)
    async with engine.begin() as conn:
        trace, values = await seed_offer(conn)
    with pytest.raises(IntegrityError, match="uq_switch_offer_command"):
        async with engine.begin() as conn:
            await insert(conn, "payment_method_switch_offers", **offer_values(trace, "pay_revg_rzp001"))
    with pytest.raises(IntegrityError, match="uq_switch_offer_checkout_order"):
        async with engine.begin() as conn:
            other = await seed_full_trace(conn, source="razorpay", key="pay_revg_rzp002")
            await insert(conn, "payment_method_switch_offers", **offer_values(
                other, "pay_revg_rzp002", checkout_order_id=values["checkout_order_id"],
            ))


async def test_downgrade_is_refused_while_offers_exist(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_G)
    async with engine.begin() as conn:
        await seed_offer(conn)
    schema_g = await schema_snapshot(engine)
    hashes_g = await data_hashes(engine, columns_by_table(schema_g))

    result = await alembic.run("downgrade", REVISION_F)

    assert result.returncode != 0
    assert "payment method switch offer downgrade refused: 2 row(s)" in result.stderr
    assert await version(engine) == REVISION_G
    assert await schema_snapshot(engine) == schema_g
    assert await data_hashes(engine, columns_by_table(schema_g)) == hashes_g
