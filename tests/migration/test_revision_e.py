"""
Database-backed tests for Phase 3 Revision E (b8c9d0e1f2a3, Z-score evidence columns), run only with
RUN_MIGRATION_TESTS=1 against localhost:5434/payment_recovery_test. Exact D <-> E round trip.
"""
from sqlalchemy import text

from tests.migration.conftest import REVISION_D, REVISION_E
from tests.migration.test_revision_b import columns_by_table, data_hashes, schema_snapshot, seed_full_trace, version

NEW_COLUMNS = {
    ("payment_health_snapshots", "baseline_mean_success_rate"),
    ("payment_health_snapshots", "baseline_stddev_success_rate"),
    ("payment_health_snapshots", "baseline_sample_count"),
    ("degradation_signals", "z_score"),
    ("degradation_signals", "z_score_status"),
}


async def _new_columns(engine):
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name IN "
            "('baseline_mean_success_rate', 'baseline_stddev_success_rate', 'baseline_sample_count', 'z_score', 'z_score_status')"
        ))).all()
    return {(t, c): nullable for t, c, nullable in rows}


async def test_upgrade_to_revision_e_and_downgrade_roundtrip(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_D)
    async with engine.begin() as conn:
        await seed_full_trace(conn, source="razorpay", key="pay_reve_rzp001")

    schema_d = await schema_snapshot(engine)
    hashes_d = await data_hashes(engine, columns_by_table(schema_d))
    assert await _new_columns(engine) == {}

    await alembic.ok("upgrade", REVISION_E)
    assert await version(engine) == REVISION_E
    assert await _new_columns(engine) == dict.fromkeys(NEW_COLUMNS, "YES")
    assert await data_hashes(engine, columns_by_table(schema_d)) == hashes_d

    await alembic.ok("downgrade", REVISION_D)
    assert await schema_snapshot(engine) == schema_d
    assert await data_hashes(engine, columns_by_table(schema_d)) == hashes_d

    await alembic.ok("upgrade", "head")
    assert await version(engine) == REVISION_E
