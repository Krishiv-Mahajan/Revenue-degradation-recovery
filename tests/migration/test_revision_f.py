"""
Database-backed tests for Phase 3 Revision F (c9d0e1f2a3b4, Stage 2-4 provider_scope), run only with
RUN_MIGRATION_TESTS=1 against localhost:5434/payment_recovery_test. Exact E <-> F round trip, scoped
identity constraints, and a downgrade that refuses (changing nothing) while PayPal-scope rows exist.
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.migration.conftest import REVISION_E, REVISION_F
from tests.migration.test_revision_b import (
    T0, columns_by_table, data_hashes, insert, scalar, schema_snapshot, seed_full_trace, version,
)

SCOPED_TABLES = ("payment_health_snapshots", "degradation_signals", "degradation_episodes", "episode_state_history")
WINDOW_END = T0 + timedelta(minutes=5)


async def seed_stage2_4(conn, *, segment_value="HDFC", **scope):
    """One snapshot, signal, episode and history row; `scope` is provider_scope=... once Revision F exists."""
    snapshot_id, episode_id = uuid.uuid4(), uuid.uuid4()
    await insert(
        conn, "payment_health_snapshots",
        snapshot_id=snapshot_id, window_start=T0, window_end=WINDOW_END, segment_dimension="bank",
        segment_value=segment_value, transaction_count=40, successful_transaction_count=20,
        failed_transaction_count=20, total_gmv_minor_units=400000, successful_gmv_minor_units=200000,
        failed_gmv_minor_units=200000, insufficient_volume=False, calculated_at=WINDOW_END, **scope,
    )
    await insert(
        conn, "degradation_signals",
        signal_id=uuid.uuid4(), snapshot_id=snapshot_id, segment_dimension="bank", segment_value=segment_value, window_start=T0,
        evaluation_version=1, signal_type="CRITICAL", evaluation_timestamp=WINDOW_END, **scope,
    )
    await insert(
        conn, "degradation_episodes",
        episode_id=episode_id, segment_dimension="bank", segment_value=segment_value, started_at_window=T0,
        status="ACTIVE", peak_absolute_drop=0.4, affected_window_count=1, severity="CRITICAL", **scope,
    )
    await insert(
        conn, "episode_state_history",
        history_id=uuid.uuid4(), reconciliation_run_id=uuid.uuid4(), episode_id=episode_id, segment_dimension="bank",
        segment_value=segment_value, status="ACTIVE", severity="CRITICAL", effective_start_window=T0,
        evaluation_timestamp=WINDOW_END, **scope,
    )


async def _scopes(engine):
    async with engine.connect() as conn:
        return {
            table: sorted(r[0] for r in (await conn.execute(text(f"SELECT provider_scope FROM {table}"))).all())
            for table in SCOPED_TABLES
        }


async def test_upgrade_to_revision_f_and_downgrade_roundtrip(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_E)
    async with engine.begin() as conn:
        await seed_full_trace(conn, source="razorpay", key="pay_revf_rzp001")
        await seed_stage2_4(conn)

    schema_e = await schema_snapshot(engine)
    hashes_e = await data_hashes(engine, columns_by_table(schema_e))

    await alembic.ok("upgrade", REVISION_F)
    assert await version(engine) == REVISION_F
    # Every existing Stage 2-4 row belongs to the original Razorpay/synthetic scope; nothing else changed.
    assert await _scopes(engine) == dict.fromkeys(SCOPED_TABLES, ["razorpay_pipeline"])
    assert await data_hashes(engine, columns_by_table(schema_e)) == hashes_e

    await alembic.ok("downgrade", REVISION_E)
    assert await schema_snapshot(engine) == schema_e
    assert await data_hashes(engine, columns_by_table(schema_e)) == hashes_e

    await alembic.ok("upgrade", REVISION_F)
    assert await version(engine) == REVISION_F


async def test_identity_constraints_are_unique_per_scope(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_F)
    async with engine.begin() as conn:
        await seed_stage2_4(conn)
        # The same segment and window in the PayPal scope is a different snapshot and signal.
        await seed_stage2_4(conn, provider_scope="paypal")
    assert await _scopes(engine) == dict.fromkeys(SCOPED_TABLES, ["paypal", "razorpay_pipeline"])

    for scope in ("razorpay_pipeline", "paypal"):
        with pytest.raises(IntegrityError, match="uq_snapshot_identity"):
            async with engine.begin() as conn:
                await seed_stage2_4(conn, provider_scope=scope)


async def test_unknown_scope_is_rejected(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_F)
    with pytest.raises(IntegrityError, match="ck_payment_health_snapshots_provider_scope"):
        async with engine.begin() as conn:
            await seed_stage2_4(conn, provider_scope="stripe")


async def test_downgrade_is_refused_while_paypal_scope_rows_exist(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_F)
    async with engine.begin() as conn:
        await seed_stage2_4(conn)
        await seed_stage2_4(conn, provider_scope="paypal")
    schema_f = await schema_snapshot(engine)
    hashes_f = await data_hashes(engine, columns_by_table(schema_f))

    result = await alembic.run("downgrade", REVISION_E)

    assert result.returncode != 0
    assert "provider_scope downgrade refused: 4 row(s)" in result.stderr
    assert await version(engine) == REVISION_F
    assert await schema_snapshot(engine) == schema_f
    assert await data_hashes(engine, columns_by_table(schema_f)) == hashes_f
    assert await scalar(engine, "SELECT count(*) FROM degradation_episodes WHERE provider_scope = 'paypal'") == 1
