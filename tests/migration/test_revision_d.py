"""
Database-backed tests for Phase 3 Revision D (a7b8c9d0e1f2, pipeline_runs outbox), run only with
RUN_MIGRATION_TESTS=1 against localhost:5434/payment_recovery_test. Validates an exact upgrade from
Revision C and downgrade back to it, with existing data untouched.
"""
from sqlalchemy import text

from tests.migration.conftest import REVISION_C, REVISION_D
from tests.migration.test_revision_b import (
    columns_by_table,
    data_hashes,
    schema_snapshot,
    seed_full_trace,
    version,
)


async def _has_table(engine, name):
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT to_regclass(:name)"), {"name": f"public.{name}"})).scalar() is not None


async def test_upgrade_to_revision_d_and_downgrade_roundtrip(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_C)
    async with engine.begin() as conn:
        await seed_full_trace(conn, source="razorpay", key="pay_revd_rzp001")
        await seed_full_trace(conn, source="paypal", key="paypal-order:REVD001", reference="REVD001")

    schema_c = await schema_snapshot(engine)
    hashes_c = await data_hashes(engine, columns_by_table(schema_c))
    assert not await _has_table(engine, "pipeline_runs")

    await alembic.ok("upgrade", REVISION_D)
    assert await version(engine) == REVISION_D
    assert await _has_table(engine, "pipeline_runs")
    async with engine.connect() as conn:
        constraints = set((await conn.execute(text(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'public.pipeline_runs'::regclass"
        ))).scalars())
        # No backfill: existing events get no pipeline run.
        assert (await conn.execute(text("SELECT count(*) FROM pipeline_runs"))).scalar_one() == 0
    assert constraints == {
        "pipeline_runs_pkey", "ck_pipeline_run_status", "ck_pipeline_run_attempts", "fk_pipeline_run_event",
    }
    schema_d = await schema_snapshot(engine)
    assert await data_hashes(engine, columns_by_table(schema_c)) == hashes_c

    await alembic.ok("downgrade", REVISION_C)
    assert await version(engine) == REVISION_C
    assert await schema_snapshot(engine) == schema_c
    assert await data_hashes(engine, columns_by_table(schema_c)) == hashes_c

    await alembic.ok("upgrade", "head")
    assert await version(engine) == REVISION_D
    assert await schema_snapshot(engine) == schema_d
