"""
Database-backed tests for Phase 3 Revision B (e2d3c4b5a6f7), run only with RUN_MIGRATION_TESTS=1
against localhost:5434/payment_recovery_test. Validates upgrade from Revision A, downgrade back to
Revision A, preflight enforcement, and statement-level NOT NULL and composite foreign key constraints.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.conftest import verify_and_truncate
from tests.migration.conftest import BASE_REVISION, REVISION_A, REVISION_B

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)

REVISION_B_CONSTRAINTS = (
    "uq_failure_prediction_version",
    "uq_intervention_decision_version",
    "uq_cmd_attempt_version",
    "uq_failure_prediction_provider",
    "uq_intervention_decision_provider",
    "uq_cmd_provider",
    "fk_failure_predictions_payment_attempt",
    "fk_intervention_decisions_payment_attempt",
    "fk_intervention_commands_payment_attempt",
    "fk_payment_outcome_observations_payment_attempt",
    "fk_counterfactual_attributions_payment_attempt",
    "fk_attribution_command",
    "fk_attribution_decision",
    "fk_command_decision",
    "fk_exec_attempt_command",
    "fk_observation_command",
)

STAGE5_8_TABLES = (
    "failure_predictions",
    "intervention_decisions",
    "intervention_commands",
    "intervention_execution_attempts",
    "payment_outcome_observations",
    "counterfactual_attributions",
)

SNAPSHOT_QUERIES = {
    "tables": "SELECT table_name FROM information_schema.tables "
              "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1",
    "columns": "SELECT table_name, column_name, data_type, is_nullable, column_default, character_maximum_length "
               "FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1, 2",
    "constraints": "SELECT conrelid::regclass::text, conname, contype, pg_get_constraintdef(oid), "
                   "condeferrable, condeferred, convalidated FROM pg_constraint "
                   "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2",
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1, 2",
}


async def schema_snapshot(engine):
    async with engine.connect() as conn:
        return {name: (await conn.execute(text(sql))).all() for name, sql in SNAPSHOT_QUERIES.items()}


async def data_hashes(engine, columns_by_table):
    hashes = {}
    async with engine.connect() as conn:
        for table, columns in columns_by_table.items():
            row = ", ".join(f'"{c}"' for c in columns)
            sql = (f"SELECT md5(coalesce(string_agg(r, '|' ORDER BY r), '')) "
                   f"FROM (SELECT row({row})::text AS r FROM {table}) s")
            hashes[table] = (await conn.execute(text(sql))).scalar_one()
    return hashes


def columns_by_table(snapshot):
    tables = {}
    for table, column, *_ in snapshot["columns"]:
        tables.setdefault(table, []).append(column)
    tables.pop("alembic_version", None)
    return tables


async def version(engine) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()


async def scalar(engine, query, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(query), params)).scalar_one()


async def insert(conn, table, **values):
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    await conn.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({params})"), values)


async def event(conn, *, source, payment_id, event_type="payment.authorized", order_id=None, at=T0, event_id=None):
    event_id = event_id or uuid.uuid4()
    await insert(
        conn, "payment_events",
        event_id=event_id, source_system=source, source_event_id=f"evt_{event_id}", payment_id=payment_id,
        order_id=order_id, timestamp=at, event_type=event_type, currency="INR", amount_minor_units=50000,
        payment_status=event_type.split(".")[-1], ingested_at=at,
    )
    return event_id


async def registry_attempt(conn, *, source, key, reference, trigger_id, at=T0):
    await insert(
        conn, "payment_attempts",
        payment_attempt_id=key, source_system=source, provider_reference=reference,
        trigger_event_id=trigger_id, registered_at=at,
    )


async def seed_full_trace(conn, *, source="razorpay", key="pay_revb_001", version_num=1, reference=None):
    ref = reference or key
    evt_type = "order.approved" if source == "paypal" else "payment.authorized"
    trigger = await event(conn, source=source, payment_id=ref, event_type=evt_type)
    await registry_attempt(conn, source=source, key=key, reference=ref, trigger_id=trigger)
    pred_id = uuid.uuid4()
    await insert(
        conn, "failure_predictions",
        prediction_id=pred_id, payment_attempt_id=key, source_system=source,
        prediction_version=version_num, predicted_at=T0, prediction_horizon="30m",
        failure_probability=0.8, risk_band="HIGH", prediction_status="PREDICTED",
        model_name="test_model", model_version="1.0", feature_schema_version="1.0",
        feature_snapshot=json.dumps({}), input_fingerprint=f"sha256:{pred_id.hex}",
        created_at=T0,
    )
    dec_id = uuid.uuid4()
    await insert(
        conn, "intervention_decisions",
        decision_id=dec_id, payment_attempt_id=key, source_system=source,
        decision_version=version_num, decided_at=T0, decision_type="ACT",
        selected_route_id="ROUTE_A", failure_probability=0.8, stage5_prediction_id=pred_id,
        diagnosis_confidence="STRONG", decision_confidence=0.9, gate_verdict="PASSED",
        policy_id="P1", policy_version="1.0", input_fingerprint=f"sha256:{dec_id.hex}",
        evaluation_audit_payload=json.dumps({}), created_at=T0,
    )
    cmd_id = uuid.uuid4()
    await insert(
        conn, "intervention_commands",
        command_id=cmd_id, decision_id=dec_id, payment_attempt_id=key, source_system=source,
        decision_version=version_num, route_key="ROUTE_A", policy_id="P1", policy_version="1.0",
        command_status="SUCCEEDED", idempotency_key=f"cmd_{cmd_id.hex}", created_at=T0,
        expires_at=T0 + timedelta(minutes=5), updated_at=T0,
    )
    attempt_id = uuid.uuid4()
    await insert(
        conn, "intervention_execution_attempts",
        attempt_id=attempt_id, command_id=cmd_id, source_system=source,
        attempt_number=1, started_at=T0, completed_at=T0, duration_ms=10,
        execution_status="SUCCESS", executor_name="sim", is_simulation=True,
        provider_action_type="ACTION", provider_response_payload=json.dumps({}),
        created_at=T0,
    )
    obs_id = uuid.uuid4()
    await insert(
        conn, "payment_outcome_observations",
        observation_id=obs_id, command_id=cmd_id, payment_attempt_id=key, source_system=source,
        observation_version=1, observed_at=T0, as_of_timestamp=T0,
        payment_outcome="CAPTURED", observation_audit_payload=json.dumps({}), created_at=T0,
    )
    attr_id = uuid.uuid4()
    await insert(
        conn, "counterfactual_attributions",
        attribution_id=attr_id, command_id=cmd_id, payment_attempt_id=key, source_system=source,
        decision_id=dec_id, attribution_version=1, attributed_at=T0, as_of_timestamp=T0,
        attribution_status="ATTRIBUTED", methodology_name="M1", methodology_version="1.0",
        treatment_status="TREATED", observed_payment_outcome="CAPTURED", payment_amount_minor_units=50000,
        counterfactual_failure_probability=0.8, counterfactual_loss_exposure_minor_units=1000,
        counterfactual_natural_success_gmv_minor_units=49000, attributed_protected_gmv_minor_units=1000,
        attribution_confidence=0.9, is_synthetic_baseline=False, is_simulated_execution=True,
        attribution_audit_payload=json.dumps({}), created_at=T0,
    )
    return {
        "trigger": trigger, "pred_id": pred_id, "dec_id": dec_id,
        "cmd_id": cmd_id, "attempt_id": attempt_id, "obs_id": obs_id, "attr_id": attr_id,
    }


# --- Upgrade & Downgrade Roundtrip -------------------------------------------------------------

async def test_upgrade_to_revision_b_and_downgrade_roundtrip(migration_db):
    engine, alembic = migration_db
    # 1. Bring DB to Revision A
    await alembic.ok("upgrade", REVISION_A)
    assert await version(engine) == REVISION_A

    # 2. Seed consistent data under Revision A for BOTH providers (Razorpay and PayPal)
    async with engine.begin() as conn:
        await seed_full_trace(conn, source="razorpay", key="pay_trace_rzp001")
        await seed_full_trace(conn, source="paypal", key="paypal-order:pay_trace_pp001", reference="pay_trace_pp001")

    schema_a_before = await schema_snapshot(engine)
    data_hashes_a_before = await data_hashes(engine, columns_by_table(schema_a_before))

    # 3. Upgrade to Revision B
    result = await alembic.ok("upgrade", REVISION_B)
    assert await version(engine) == REVISION_B

    # 4. Verify all constraints exist and are validated
    async with engine.connect() as conn:
        constraints = (await conn.execute(text(
            "SELECT conname, convalidated, condeferrable FROM pg_constraint WHERE conname = ANY(:names)"
        ), {"names": list(REVISION_B_CONSTRAINTS)})).all()
        assert {r.conname for r in constraints} == set(REVISION_B_CONSTRAINTS)
        assert all(r.convalidated and not r.condeferrable for r in constraints)

        # Verify NOT NULL on all 6 tables
        nullables = (await conn.execute(text(
            "SELECT table_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = ANY(:tables) AND column_name = 'source_system'"
        ), {"tables": list(STAGE5_8_TABLES)})).all()
        assert len(nullables) == 6
        assert all(r.is_nullable == "NO" for r in nullables)

    schema_b = await schema_snapshot(engine)
    data_hashes_b = await data_hashes(engine, columns_by_table(schema_b))

    # 5. Downgrade back to Revision A
    await alembic.ok("downgrade", REVISION_A)
    assert await version(engine) == REVISION_A

    # Exact Revision A schema and data hashes verified identical to before Revision B
    assert await schema_snapshot(engine) == schema_a_before
    assert await data_hashes(engine, columns_by_table(schema_a_before)) == data_hashes_a_before

    # Verify source_system is nullable again under Revision A
    async with engine.connect() as conn:
        nullables = (await conn.execute(text(
            "SELECT table_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = ANY(:tables) AND column_name = 'source_system'"
        ), {"tables": list(STAGE5_8_TABLES)})).all()
        assert len(nullables) == 6
        assert all(r.is_nullable == "YES" for r in nullables)

        # Attribution foreign keys restored to single-column
        fks = (await conn.execute(text(
            "SELECT conname, pg_get_constraintdef(oid) as cdef FROM pg_constraint "
            "WHERE conname IN ('fk_attribution_command', 'fk_attribution_decision')"
        ))).all()
        assert len(fks) == 2
        for r in fks:
            assert "source_system" not in r.cdef

    # 6. Re-upgrade to Revision B (head)
    await alembic.ok("upgrade", "head")
    assert await version(engine) == REVISION_B
    assert await schema_snapshot(engine) == schema_b
    assert await data_hashes(engine, columns_by_table(schema_b)) == data_hashes_b


AS_IF_NON_TEST_TARGET = """
import sys
from sqlalchemy.engine import make_url
import src.infrastructure.db_safety as safety

_real = safety.final_state_requires_assertion
_elsewhere = make_url("postgresql+asyncpg://nobody@not-the-test-database.invalid:5432/elsewhere")

def _as_if_elsewhere(final_heads, target, *, offline, **kwargs):
    return _real(final_heads, _elsewhere, offline=offline, **kwargs)

safety.final_state_requires_assertion = _as_if_elsewhere
from alembic.config import main
main(argv=sys.argv[1:], prog="alembic")
"""


async def test_downgrade_to_revision_a_is_refused_online_for_a_non_test_target(migration_db, migration_target):
    """
    Phase 3 Revision A is an intermediate revision. Online runs ending at Revision A
    on non-test databases are refused outright and rolled back safely.
    Revision B schema and data hashes are preserved identically.
    """
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_B)

    # Seed traces for both providers under Revision B
    async with engine.begin() as conn:
        await seed_full_trace(conn, source="razorpay", key="pay_refused_rzp001")
        await seed_full_trace(conn, source="paypal", key="paypal-order:pay_refused_pp001", reference="pay_refused_pp001")

    schema_b_before = await schema_snapshot(engine)
    data_hashes_b_before = await data_hashes(engine, columns_by_table(schema_b_before))

    result = await alembic.run("downgrade", REVISION_A, bootstrap=AS_IF_NON_TEST_TARGET)
    assert result.returncode != 0
    assert f"at intermediate revision(s) {REVISION_A}" in result.stderr
    assert await version(engine) == REVISION_B
    assert await schema_snapshot(engine) == schema_b_before
    assert await data_hashes(engine, columns_by_table(schema_b_before)) == data_hashes_b_before


# --- Preflight Failure Enforcement -------------------------------------------------------------

async def test_revision_b_preflight_fails_on_null_source_system(migration_db, migration_target):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_A)

    try:
        async with engine.begin() as conn:
            trigger = await event(conn, source="razorpay", payment_id="pay_null001")
            await registry_attempt(conn, source="razorpay", key="pay_null001", reference="pay_null001", trigger_id=trigger)
            pred_id = uuid.uuid4()
            # Insert with NULL source_system under Revision A
            await insert(
                conn, "failure_predictions",
                prediction_id=pred_id, payment_attempt_id="pay_null001", source_system=None,
                prediction_version=1, predicted_at=T0, prediction_horizon="30m",
                failure_probability=0.8, risk_band="HIGH", prediction_status="PREDICTED",
                model_name="m", model_version="1", feature_schema_version="1",
                feature_snapshot=json.dumps({}), input_fingerprint="sha256:0", created_at=T0,
            )

        result = await alembic.run("upgrade", REVISION_B)
        assert result.returncode != 0
        assert "PF-B01" in result.stderr
        assert "every failure_predictions row must have a source_system" in result.stderr
        assert await version(engine) == REVISION_A
    finally:
        async with engine.begin() as conn:
            await verify_and_truncate(conn, migration_target, existing_tables_only=True)


async def test_revision_b_preflight_fails_on_unregistered_attempt(migration_db, migration_target):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_A)

    try:
        async with engine.begin() as conn:
            pred_id = uuid.uuid4()
            # Insert with source_system but unregistered attempt key
            await insert(
                conn, "failure_predictions",
                prediction_id=pred_id, payment_attempt_id="pay_unreg001", source_system="razorpay",
                prediction_version=1, predicted_at=T0, prediction_horizon="30m",
                failure_probability=0.8, risk_band="HIGH", prediction_status="PREDICTED",
                model_name="m", model_version="1", feature_schema_version="1",
                feature_snapshot=json.dumps({}), input_fingerprint="sha256:0", created_at=T0,
            )

        result = await alembic.run("upgrade", REVISION_B)
        assert result.returncode != 0
        assert "PF-B07" in result.stderr
        assert "must exist in payment_attempts" in result.stderr
        assert await version(engine) == REVISION_A
    finally:
        async with engine.begin() as conn:
            await verify_and_truncate(conn, migration_target, existing_tables_only=True)


async def test_revision_b_preflight_fails_on_cross_provider_mismatch(migration_db, migration_target):
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_A)

    try:
        async with engine.begin() as conn:
            trig_rzp = await event(conn, source="razorpay", payment_id="pay_rzp_cross")
            await registry_attempt(conn, source="razorpay", key="pay_rzp_cross", reference="pay_rzp_cross", trigger_id=trig_rzp)
            trig_syn = await event(conn, source="synthetic", payment_id="pay_syn_cross")
            await registry_attempt(conn, source="synthetic", key="pay_syn_cross", reference="pay_syn_cross", trigger_id=trig_syn)

            dec_id = uuid.uuid4()
            await insert(
                conn, "intervention_decisions",
                decision_id=dec_id, payment_attempt_id="pay_rzp_cross", source_system="razorpay",
                decision_version=1, decided_at=T0, decision_type="ACT",
                selected_route_id="ROUTE_A", failure_probability=0.8,
                decision_confidence=0.9, gate_verdict="PASSED", policy_id="P1", policy_version="1.0",
                input_fingerprint="sha256:0", evaluation_audit_payload=json.dumps({}), created_at=T0,
            )

            cmd_id = uuid.uuid4()
            # Command specifies synthetic provider but decision was razorpay
            await insert(
                conn, "intervention_commands",
                command_id=cmd_id, decision_id=dec_id, payment_attempt_id="pay_syn_cross", source_system="synthetic",
                decision_version=1, route_key="ROUTE_A", policy_id="P1", policy_version="1.0",
                command_status="SUCCEEDED", idempotency_key="cmd_cross_1", created_at=T0,
                expires_at=T0 + timedelta(minutes=5), updated_at=T0,
            )

        result = await alembic.run("upgrade", REVISION_B)
        assert result.returncode != 0
        assert "PF-B12" in result.stderr
        assert "every command must match its decision provider" in result.stderr
        assert await version(engine) == REVISION_A
    finally:
        async with engine.begin() as conn:
            await verify_and_truncate(conn, migration_target, existing_tables_only=True)


# --- Statement-Level Constraints at Revision B --------------------------------------------------

def make_prediction_values(key, source, version=1, pred_id=None):
    pred_id = pred_id or uuid.uuid4()
    return {
        "prediction_id": pred_id, "payment_attempt_id": key, "source_system": source,
        "prediction_version": version, "predicted_at": T0, "prediction_horizon": "30m",
        "failure_probability": 0.8, "risk_band": "HIGH", "prediction_status": "PREDICTED",
        "model_name": "m", "model_version": "1", "feature_schema_version": "1",
        "feature_snapshot": json.dumps({}), "input_fingerprint": f"sha256:{pred_id.hex}", "created_at": T0,
    }


def make_decision_values(key, source, version=1, dec_id=None, pred_id=None):
    dec_id = dec_id or uuid.uuid4()
    return {
        "decision_id": dec_id, "payment_attempt_id": key, "source_system": source,
        "decision_version": version, "decided_at": T0, "decision_type": "ACT",
        "selected_route_id": "ROUTE_A", "failure_probability": 0.8, "stage5_prediction_id": pred_id,
        "diagnosis_confidence": "STRONG", "decision_confidence": 0.9, "gate_verdict": "PASSED",
        "policy_id": "P1", "policy_version": "1.0", "input_fingerprint": f"sha256:{dec_id.hex}",
        "evaluation_audit_payload": json.dumps({}), "created_at": T0,
    }


def make_command_values(key, source, dec_id, version=1, cmd_id=None):
    cmd_id = cmd_id or uuid.uuid4()
    return {
        "command_id": cmd_id, "decision_id": dec_id, "payment_attempt_id": key, "source_system": source,
        "decision_version": version, "route_key": "ROUTE_A", "policy_id": "P1", "policy_version": "1.0",
        "command_status": "SUCCEEDED", "idempotency_key": f"cmd_{cmd_id.hex}", "created_at": T0,
        "expires_at": T0 + timedelta(minutes=5), "updated_at": T0,
    }


def make_exec_attempt_values(cmd_id, source, attempt_id=None, attempt_num=1):
    attempt_id = attempt_id or uuid.uuid4()
    return {
        "attempt_id": attempt_id, "command_id": cmd_id, "source_system": source,
        "attempt_number": attempt_num, "started_at": T0, "completed_at": T0, "duration_ms": 10,
        "execution_status": "SUCCESS", "executor_name": "sim", "is_simulation": True,
        "provider_action_type": "ACTION", "provider_response_payload": json.dumps({}), "created_at": T0,
    }


def make_observation_values(key, source, cmd_id, version=1, obs_id=None, observed_at=None, as_of_timestamp=None):
    obs_id = obs_id or uuid.uuid4()
    observed = observed_at or T0
    as_of = as_of_timestamp or (T0 + timedelta(minutes=version, seconds=15))
    return {
        "observation_id": obs_id, "command_id": cmd_id, "payment_attempt_id": key, "source_system": source,
        "observation_version": version, "observed_at": observed, "as_of_timestamp": as_of,
        "payment_outcome": "CAPTURED", "observation_audit_payload": json.dumps({}), "created_at": observed,
    }


def make_attribution_values(key, source, cmd_id, dec_id, version=1, attr_id=None, attributed_at=None, as_of_timestamp=None):
    attr_id = attr_id or uuid.uuid4()
    attr_time = attributed_at or T0
    as_of = as_of_timestamp or (T0 + timedelta(minutes=version, seconds=15))
    return {
        "attribution_id": attr_id, "command_id": cmd_id, "payment_attempt_id": key, "source_system": source,
        "decision_id": dec_id, "attribution_version": version, "attributed_at": attr_time, "as_of_timestamp": as_of,
        "attribution_status": "ATTRIBUTED", "methodology_name": "M1", "methodology_version": "1.0",
        "treatment_status": "TREATED", "observed_payment_outcome": "CAPTURED", "payment_amount_minor_units": 50000,
        "counterfactual_failure_probability": 0.8, "counterfactual_loss_exposure_minor_units": 1000,
        "counterfactual_natural_success_gmv_minor_units": 49000, "attributed_protected_gmv_minor_units": 1000,
        "attribution_confidence": 0.9, "is_synthetic_baseline": False, "is_simulated_execution": True,
        "attribution_audit_payload": json.dumps({}), "created_at": attr_time,
    }


@pytest.mark.parametrize("table", STAGE5_8_TABLES)
async def test_not_null_rejected_for_every_stage5_8_table(migration_db, table):
    """Every Stage 5-8 table rejects rows with source_system IS NULL at statement level."""
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_B)

    key = f"pay_nn_{table[:10]}_{uuid.uuid4().hex[:6]}"
    async with engine.begin() as conn:
        trace = await seed_full_trace(conn, source="razorpay", key=key)

    # Prepare values for the target table with source_system = None
    table_values = {
        "failure_predictions": make_prediction_values(key, None),
        "intervention_decisions": make_decision_values(key, None, pred_id=trace["pred_id"]),
        "intervention_commands": make_command_values(key, None, trace["dec_id"]),
        "intervention_execution_attempts": make_exec_attempt_values(trace["cmd_id"], None, attempt_num=2),
        "payment_outcome_observations": make_observation_values(key, None, trace["cmd_id"], version=2),
        "counterfactual_attributions": make_attribution_values(key, None, trace["cmd_id"], trace["dec_id"], version=2),
    }[table]

    async with engine.begin() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            async with conn.begin_nested():
                await insert(conn, table, **table_values)
        assert "null value in column \"source_system\"" in str(exc_info.value) or "violates not-null constraint" in str(exc_info.value)


@pytest.mark.parametrize("table, fk_name", [
    ("failure_predictions", "fk_failure_predictions_payment_attempt"),
    ("intervention_decisions", "fk_intervention_decisions_payment_attempt"),
    ("intervention_commands", "fk_intervention_commands_payment_attempt"),
    ("payment_outcome_observations", "fk_payment_outcome_observations_payment_attempt"),
    ("counterfactual_attributions", "fk_counterfactual_attributions_payment_attempt"),
])
async def test_composite_fk_to_payment_attempts_rejected_for_every_keyed_table(migration_db, table, fk_name):
    """
    Every keyed Stage 5-8 table rejects orphaned attempt references and cross-provider mismatches
    via composite foreign key to payment_attempts(payment_attempt_id, source_system).
    """
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_B)

    suffix = uuid.uuid4().hex[:6]
    registered_key = f"pay_fk_rzp_{table[:8]}_{suffix}"
    unregistered_key = f"pay_fk_unreg_{table[:8]}_{suffix}"
    syn_registered_key = f"pay_fk_syn_{table[:8]}_{suffix}"

    async with engine.begin() as conn:
        trace_rzp = await seed_full_trace(conn, source="razorpay", key=registered_key)
        trace_syn = await seed_full_trace(conn, source="synthetic", key=syn_registered_key)
        # Create uncommanded decisions for command FK tests
        dec_rzp2 = uuid.uuid4()
        await insert(conn, "intervention_decisions", **make_decision_values(registered_key, "razorpay", version=2, dec_id=dec_rzp2, pred_id=trace_rzp["pred_id"]))
        dec_syn2 = uuid.uuid4()
        await insert(conn, "intervention_decisions", **make_decision_values(syn_registered_key, "synthetic", version=2, dec_id=dec_syn2, pred_id=trace_syn["pred_id"]))

    # 1. Unregistered attempt key
    unreg_values = {
        "failure_predictions": make_prediction_values(unregistered_key, "razorpay"),
        "intervention_decisions": make_decision_values(unregistered_key, "razorpay", pred_id=trace_rzp["pred_id"]),
        "intervention_commands": make_command_values(unregistered_key, "razorpay", dec_rzp2),
        "payment_outcome_observations": make_observation_values(unregistered_key, "razorpay", trace_rzp["cmd_id"], version=2),
        "counterfactual_attributions": make_attribution_values(unregistered_key, "razorpay", trace_rzp["cmd_id"], trace_rzp["dec_id"], version=2),
    }[table]

    async with engine.begin() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            async with conn.begin_nested():
                await insert(conn, table, **unreg_values)
        assert fk_name in str(exc_info.value)

    # 2. Key exists for razorpay, but row specifies synthetic (with synthetic parent where applicable)
    mismatch_values = {
        "failure_predictions": make_prediction_values(registered_key, "synthetic"),
        "intervention_decisions": make_decision_values(registered_key, "synthetic", pred_id=trace_syn["pred_id"]),
        "intervention_commands": make_command_values(registered_key, "synthetic", dec_syn2),
        "payment_outcome_observations": make_observation_values(registered_key, "synthetic", trace_syn["cmd_id"], version=2),
        "counterfactual_attributions": make_attribution_values(registered_key, "synthetic", trace_syn["cmd_id"], trace_syn["dec_id"], version=2),
    }[table]

    async with engine.begin() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            async with conn.begin_nested():
                await insert(conn, table, **mismatch_values)
        assert fk_name in str(exc_info.value)


@pytest.mark.parametrize("fk_name", [
    "fk_command_decision",
    "fk_exec_attempt_command",
    "fk_observation_command",
    "fk_attribution_command",
    "fk_attribution_decision",
])
async def test_relational_consistency_fks_rejected_at_statement(migration_db, fk_name):
    """
    Every inter-stage foreign key rejects parent-child provider inconsistency at the statement level.
    """
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_B)

    suffix = f"{fk_name[:8]}_{uuid.uuid4().hex[:6]}"
    key_rzp = f"pay_rzp_{suffix}"
    key_syn = f"pay_syn_{suffix}"
    dec_rzp2 = uuid.uuid4()
    async with engine.begin() as conn:
        trace_rzp = await seed_full_trace(conn, source="razorpay", key=key_rzp)
        trace_syn = await seed_full_trace(conn, source="synthetic", key=key_syn)
        await insert(conn, "intervention_decisions", **make_decision_values(key_rzp, "razorpay", version=2, dec_id=dec_rzp2, pred_id=trace_rzp["pred_id"]))

    # Target table, values to insert with cross-provider parent reference
    if fk_name == "fk_command_decision":
        # Command specifies synthetic provider but references razorpay decision (which has no existing command, version=2 avoids uq_cmd_attempt_version)
        values = make_command_values(key_syn, "synthetic", dec_rzp2, version=2)
        table = "intervention_commands"
    elif fk_name == "fk_exec_attempt_command":
        # Execution attempt specifies synthetic provider but references razorpay command (attempt_num=2 avoids uq_exec_attempt_number)
        values = make_exec_attempt_values(trace_rzp["cmd_id"], "synthetic", attempt_num=2)
        table = "intervention_execution_attempts"
    elif fk_name == "fk_observation_command":
        # Observation specifies synthetic provider but references razorpay command
        values = make_observation_values(key_syn, "synthetic", trace_rzp["cmd_id"], version=3)
        table = "payment_outcome_observations"
    elif fk_name == "fk_attribution_command":
        # Attribution specifies synthetic provider, synthetic decision, but razorpay command
        values = make_attribution_values(key_syn, "synthetic", trace_rzp["cmd_id"], trace_syn["dec_id"], version=3)
        table = "counterfactual_attributions"
    elif fk_name == "fk_attribution_decision":
        # Attribution specifies synthetic provider, synthetic command, but razorpay decision
        values = make_attribution_values(key_syn, "synthetic", trace_syn["cmd_id"], trace_rzp["dec_id"], version=3)
        table = "counterfactual_attributions"

    async with engine.begin() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            async with conn.begin_nested():
                await insert(conn, table, **values)
        assert fk_name in str(exc_info.value)


@pytest.mark.parametrize("table, uq_name", [
    ("failure_predictions", "uq_failure_prediction_version"),
    ("intervention_decisions", "uq_intervention_decision_version"),
    ("intervention_commands", "uq_cmd_attempt_version"),
])
async def test_composite_version_unique_constraints_rejected_at_statement(migration_db, table, uq_name):
    """
    Every versioned Stage 5-8 table rejects duplicate version numbers for the same (attempt_id, source_system).
    """
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_B)

    key = f"pay_uq_{table[:8]}_{uuid.uuid4().hex[:6]}"
    dec_id2 = uuid.uuid4()
    async with engine.begin() as conn:
        trace = await seed_full_trace(conn, source="razorpay", key=key, version_num=1)
        if table == "intervention_commands":
            await insert(conn, "intervention_decisions", **make_decision_values(key, "razorpay", version=2, dec_id=dec_id2, pred_id=trace["pred_id"]))

    duplicate_values = {
        "failure_predictions": make_prediction_values(key, "razorpay", version=1),
        "intervention_decisions": make_decision_values(key, "razorpay", version=1, pred_id=trace["pred_id"]),
        "intervention_commands": make_command_values(key, "razorpay", dec_id2, version=1),
    }[table]

    async with engine.begin() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            async with conn.begin_nested():
                await insert(conn, table, **duplicate_values)
        assert uq_name in str(exc_info.value)


async def test_identical_provider_reference_collision_and_cross_provider_isolation(migration_db):
    """
    Valid collision scenario per registry invariants:
    Razorpay and PayPal share the identical provider_reference (e.g. order ID 'COLLIDE_REF_001').
    Under canonical registry rules:
      - Razorpay registered key is 'COLLIDE_REF_001'
      - PayPal registered key is 'paypal-order:COLLIDE_REF_001'
    Both attempts coexist in payment_attempts and both insert full Stage 5-8 traces at version 1
    simultaneously without collision. Cross-provider key references are rejected via composite FKs.
    Savepoints isolate all expected integrity rejections so successful fixtures are preserved.
    """
    engine, alembic = migration_db
    await alembic.ok("upgrade", REVISION_B)

    shared_reference = f"COLLIDE_{uuid.uuid4().hex[:10]}"
    rzp_key = shared_reference
    paypal_key = f"paypal-order:{shared_reference}"

    async with engine.begin() as conn:
        # 1. Seed full Stage 5-8 trace for Razorpay (v1) with shared reference
        trace_rzp = await seed_full_trace(conn, source="razorpay", key=rzp_key, reference=shared_reference, version_num=1)

        # 2. Seed full Stage 5-8 trace for PayPal (v1) with identical shared reference
        trace_pp = await seed_full_trace(conn, source="paypal", key=paypal_key, reference=shared_reference, version_num=1)

        # Both traces exist simultaneously at version 1 without collision
        assert trace_rzp["pred_id"] != trace_pp["pred_id"]
        assert trace_rzp["dec_id"] != trace_pp["dec_id"]
        assert trace_rzp["cmd_id"] != trace_pp["cmd_id"]

        # 3. Isolation checks under savepoints (begin_nested)
        # Attempt to insert Razorpay prediction pointing to PayPal's attempt key
        with pytest.raises(IntegrityError) as exc_info:
            async with conn.begin_nested():
                await insert(
                    conn, "failure_predictions",
                    **make_prediction_values(paypal_key, "razorpay", version=2),
                )
        assert "fk_failure_predictions_payment_attempt" in str(exc_info.value)

        # Attempt to insert PayPal prediction pointing to Razorpay's attempt key
        with pytest.raises(IntegrityError) as exc_info:
            async with conn.begin_nested():
                await insert(
                    conn, "failure_predictions",
                    **make_prediction_values(rzp_key, "paypal", version=2),
                )
        assert "fk_failure_predictions_payment_attempt" in str(exc_info.value)

        # Duplicate version 1 for Razorpay fails uq_failure_prediction_version
        with pytest.raises(IntegrityError) as exc_info:
            async with conn.begin_nested():
                await insert(
                    conn, "failure_predictions",
                    **make_prediction_values(rzp_key, "razorpay", version=1),
                )
        assert "uq_failure_prediction_version" in str(exc_info.value)

        # Duplicate version 1 for PayPal fails uq_failure_prediction_version
        with pytest.raises(IntegrityError) as exc_info:
            async with conn.begin_nested():
                await insert(
                    conn, "failure_predictions",
                    **make_prediction_values(paypal_key, "paypal", version=1),
                )
        assert "uq_failure_prediction_version" in str(exc_info.value)

    # Verify data integrity after commit: exactly 2 predictions, 1 per provider
    async with engine.connect() as conn:
        preds = (await conn.execute(text(
            "SELECT source_system, payment_attempt_id, prediction_version FROM failure_predictions "
            "WHERE payment_attempt_id IN (:rzp, :pp) ORDER BY source_system"
        ), {"rzp": rzp_key, "pp": paypal_key})).all()
        assert len(preds) == 2
        assert preds[0] == ("paypal", paypal_key, 1)
        assert preds[1] == ("razorpay", rzp_key, 1)
