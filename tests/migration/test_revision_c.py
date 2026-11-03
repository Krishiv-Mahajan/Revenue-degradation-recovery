"""
Database-backed tests for Phase 3 Revision C (f4a5b6c7d8e9), run only with RUN_MIGRATION_TESTS=1
against localhost:5434/payment_recovery_test. Validates upgrade from Revision B, an exact downgrade
back to Revision B, and that every preflight check refuses inconsistent links without changing
anything. Statement-level behaviour at head is covered by
tests/integration/test_stage5_8_same_attempt_constraints.py.
"""
import uuid

import pytest
from sqlalchemy import text

from tests.conftest import verify_and_truncate
from tests.migration.conftest import REVISION_B, REVISION_C
from tests.migration.test_revision_b import (
    columns_by_table,
    data_hashes,
    insert,
    make_attribution_values,
    make_command_values,
    make_decision_values,
    make_observation_values,
    schema_snapshot,
    seed_full_trace,
    version,
)

REVISION_C_CONSTRAINTS = (
    "uq_failure_prediction_attempt_link",
    "uq_intervention_decision_attempt_link",
    "uq_cmd_attempt_link",
    "fk_decision_prediction",
    "fk_decision_prediction_attempt",
    "fk_command_decision_attempt",
    "fk_observation_command_attempt",
    "fk_attribution_command_attempt",
    "fk_attribution_decision_attempt",
)


async def _constraint_names(engine):
    async with engine.connect() as conn:
        return set((await conn.execute(text(
            "SELECT conname FROM pg_constraint WHERE conname = ANY(:names)"
        ), {"names": list(REVISION_C_CONSTRAINTS)})).scalars())


async def test_upgrade_to_revision_c_and_downgrade_roundtrip(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_B)
    async with engine.begin() as conn:
        await seed_full_trace(conn, source="razorpay", key="pay_revc_rzp001")
        await seed_full_trace(conn, source="synthetic", key="pay_revc_syn001")
        await seed_full_trace(conn, source="paypal", key="paypal-order:REVC001", reference="REVC001")

    schema_b = await schema_snapshot(engine)
    hashes_b = await data_hashes(engine, columns_by_table(schema_b))
    assert await _constraint_names(engine) == set()

    await alembic.ok("upgrade", REVISION_C)
    assert await version(engine) == REVISION_C
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT conname, convalidated, condeferrable FROM pg_constraint WHERE conname = ANY(:names)"
        ), {"names": list(REVISION_C_CONSTRAINTS)})).all()
    assert {r.conname for r in rows} == set(REVISION_C_CONSTRAINTS)
    assert all(r.convalidated and not r.condeferrable for r in rows)
    schema_c = await schema_snapshot(engine)
    assert await data_hashes(engine, columns_by_table(schema_c)) == hashes_b

    await alembic.ok("downgrade", REVISION_B)
    assert await version(engine) == REVISION_B
    assert await schema_snapshot(engine) == schema_b
    assert await data_hashes(engine, columns_by_table(schema_b)) == hashes_b

    await alembic.ok("upgrade", REVISION_C)
    assert await version(engine) == REVISION_C
    assert await schema_snapshot(engine) == schema_c
    assert await data_hashes(engine, columns_by_table(schema_c)) == hashes_b


async def _nonexistent_prediction(conn, a, b, s):
    await insert(conn, "intervention_decisions",
                 **make_decision_values(a["key"], "razorpay", version=2, pred_id=uuid.uuid4()))


async def _other_providers_prediction(conn, a, b, s):
    await insert(conn, "intervention_decisions",
                 **make_decision_values(a["key"], "razorpay", version=2, pred_id=s["pred_id"]))


async def _other_attempts_prediction(conn, a, b, s):
    await insert(conn, "intervention_decisions",
                 **make_decision_values(a["key"], "razorpay", version=2, pred_id=b["pred_id"]))


async def _command_on_other_attempts_decision(conn, a, b, s):
    dec_b2 = uuid.uuid4()
    await insert(conn, "intervention_decisions",
                 **make_decision_values(b["key"], "razorpay", version=2, dec_id=dec_b2, pred_id=b["pred_id"]))
    await insert(conn, "intervention_commands", **make_command_values(a["key"], "razorpay", dec_b2, version=2))


async def _observation_on_other_attempts_command(conn, a, b, s):
    await insert(conn, "payment_outcome_observations",
                 **make_observation_values(a["key"], "razorpay", b["cmd_id"], version=2))


async def _attribution_on_other_attempts_command(conn, a, b, s):
    await insert(conn, "counterfactual_attributions",
                 **make_attribution_values(a["key"], "razorpay", b["cmd_id"], a["dec_id"], version=2))


async def _attribution_on_other_attempts_decision(conn, a, b, s):
    await insert(conn, "counterfactual_attributions",
                 **make_attribution_values(a["key"], "razorpay", a["cmd_id"], b["dec_id"], version=2))


PREFLIGHT_CASES = [
    ("PF-C01", "every decision's stage5_prediction_id must reference an existing prediction", _nonexistent_prediction),
    ("PF-C02", "every decision's prediction must belong to the decision's provider", _other_providers_prediction),
    ("PF-C03", "every decision's prediction must belong to the decision's payment attempt", _other_attempts_prediction),
    ("PF-C04", "every command must belong to its decision's payment attempt", _command_on_other_attempts_decision),
    ("PF-C05", "every observation must belong to its command's payment attempt", _observation_on_other_attempts_command),
    ("PF-C06", "every attribution must belong to its command's payment attempt", _attribution_on_other_attempts_command),
    ("PF-C07", "every attribution must belong to its decision's payment attempt", _attribution_on_other_attempts_decision),
]


async def _trace(conn, source, key):
    return {**await seed_full_trace(conn, source=source, key=key), "key": key}


@pytest.mark.parametrize(
    "code, description, write_violation", [pytest.param(*case, id=case[0]) for case in PREFLIGHT_CASES]
)
async def test_revision_c_preflight_refuses_inconsistent_links_and_changes_nothing(
    migration_db, migration_target, code, description, write_violation
):
    """Revision B accepts each inconsistent link; Revision C refuses to upgrade over it and leaves Revision B intact."""
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_B)
    try:
        async with engine.begin() as conn:
            a = await _trace(conn, "razorpay", "pay_pfc_a")
            b = await _trace(conn, "razorpay", "pay_pfc_b")
            s = await _trace(conn, "synthetic", "pay_pfc_s")
            await write_violation(conn, a, b, s)

        schema_before = await schema_snapshot(engine)
        hashes_before = await data_hashes(engine, columns_by_table(schema_before))

        result = await alembic.run("upgrade", REVISION_C)
        assert result.returncode != 0
        assert code in result.stderr
        assert description in result.stderr
        assert "Nothing was changed" in result.stderr
        assert await version(engine) == REVISION_B
        assert await schema_snapshot(engine) == schema_before
        assert await data_hashes(engine, columns_by_table(schema_before)) == hashes_before
    finally:
        async with engine.begin() as conn:
            await verify_and_truncate(conn, migration_target, existing_tables_only=True)
