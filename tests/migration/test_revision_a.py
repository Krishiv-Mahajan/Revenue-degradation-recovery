"""
Database-backed tests for Phase 3 Revision A (9d2e4f6a8b1c), run only with RUN_MIGRATION_TESTS=1
against localhost:5434/payment_recovery_test. Rows are seeded with raw SQL at the pre-Phase-3
revision (8b9c0d1e2f3a), then the real migration runs through an Alembic subprocess.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.infrastructure.models import PAYPAL_IDENTIFIER_PATTERN
from tests.migration.conftest import BASE_REVISION, REVISION_A
from tests.unit.test_migration_sql import PAYPAL_ID_CORPUS, REVISION_A as MIGRATION

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
NEW_CONSTRAINTS = (
    "uq_payment_event_provider",
    "uq_payment_attempt_provider",
    "uq_payment_attempt_reference",
    "ck_payment_attempt_source_system",
    "ck_payment_attempt_namespace",
    "fk_payment_attempt_trigger",
)


# --- raw-SQL seeding at the pre-Phase-3 revision -------------------------------------------------

async def insert(conn, table, **values):
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    await conn.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({params})"), values)


async def event(conn, *, source, payment_id, event_type, order_id=None, at=T0, event_id=None):
    event_id = event_id or uuid.uuid4()
    await insert(
        conn, "payment_events",
        event_id=event_id, source_system=source, source_event_id=f"evt_{event_id}", payment_id=payment_id,
        order_id=order_id, timestamp=at, event_type=event_type, currency="INR", amount_minor_units=50000,
        payment_status=event_type.split(".")[-1], ingested_at=at,
    )
    return event_id


async def prediction(conn, key, *, at=T0, created_at=T0):
    prediction_id = uuid.uuid4()
    await insert(
        conn, "failure_predictions",
        prediction_id=prediction_id, payment_attempt_id=key, prediction_version=1, predicted_at=at,
        prediction_horizon="30m", failure_probability=0.9, risk_band="HIGH", prediction_status="PREDICTED",
        model_name="m", model_version="1", feature_schema_version="1", feature_snapshot=json.dumps({}),
        input_fingerprint=f"sha256:{prediction_id.hex}", created_at=created_at,
    )
    return prediction_id


async def decision(conn, key, *, prediction_id=None, version=1, at=T0, created_at=T0):
    decision_id = uuid.uuid4()
    await insert(
        conn, "intervention_decisions",
        decision_id=decision_id, payment_attempt_id=key, decision_version=version, decided_at=at,
        decision_type="ACT", selected_route_id="DYNAMIC_RETRY_BACKOFF", failure_probability=0.9,
        stage5_prediction_id=prediction_id, decision_confidence=0.9, gate_verdict="PASSED", policy_id="p",
        policy_version="1.0.0", input_fingerprint=f"sha256:{decision_id.hex}",
        evaluation_audit_payload=json.dumps({}), created_at=created_at,
    )
    return decision_id


async def command(conn, key, decision_id, *, version=1, created_at=T0):
    command_id = uuid.uuid4()
    await insert(
        conn, "intervention_commands",
        command_id=command_id, decision_id=decision_id, payment_attempt_id=key, decision_version=version,
        route_key="DYNAMIC_RETRY_BACKOFF", policy_id="p", policy_version="1.0.0", command_status="SUCCEEDED",
        idempotency_key=f"cmd_{command_id}", created_at=created_at, expires_at=created_at + timedelta(minutes=5),
        updated_at=created_at,
    )
    return command_id


async def execution_attempt(conn, command_id):
    attempt_id = uuid.uuid4()
    await insert(
        conn, "intervention_execution_attempts",
        attempt_id=attempt_id, command_id=command_id, attempt_number=1, started_at=T0, completed_at=T0,
        duration_ms=1, execution_status="SUCCESS", executor_name="simulator", is_simulation=True,
        provider_action_type="SIMULATED", provider_response_payload=json.dumps({}), created_at=T0,
    )
    return attempt_id


async def observation(conn, key, command_id, *, terminal_event_id=None):
    observation_id = uuid.uuid4()
    await insert(
        conn, "payment_outcome_observations",
        observation_id=observation_id, command_id=command_id, payment_attempt_id=key, observation_version=1,
        observed_at=T0, as_of_timestamp=T0, payment_outcome="CAPTURED" if terminal_event_id else "UNKNOWN_IN_FLIGHT",
        terminal_event_id=terminal_event_id, observation_audit_payload=json.dumps({}), created_at=T0,
    )
    return observation_id


async def attribution(conn, key, command_id, decision_id):
    attribution_id = uuid.uuid4()
    await insert(
        conn, "counterfactual_attributions",
        attribution_id=attribution_id, command_id=command_id, payment_attempt_id=key, decision_id=decision_id,
        attribution_version=1, attributed_at=T0, as_of_timestamp=T0, attribution_status="ATTRIBUTED",
        methodology_name="m", methodology_version="1", treatment_status="TREATED", observed_payment_outcome="CAPTURED",
        payment_amount_minor_units=50000, counterfactual_failure_probability=Decimal("0.9000"),
        counterfactual_loss_exposure_minor_units=45000, counterfactual_natural_success_gmv_minor_units=5000,
        attributed_protected_gmv_minor_units=45000, attribution_confidence=Decimal("0.5000"),
        is_synthetic_baseline=True, is_simulated_execution=True, attribution_audit_payload=json.dumps({}),
        created_at=T0,
    )
    return attribution_id


async def authorized(conn, key, source="razorpay", at=T0):
    return await event(conn, source=source, payment_id=key, event_type="payment.authorized", at=at)


# --- schema and data inspection ------------------------------------------------------------------

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
    """md5 of each table's rows over the given columns (the pre-Phase-3 columns)."""
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


async def scalar(engine, sql, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def version(engine):
    return await scalar(engine, "SELECT version_num FROM alembic_version")


async def assert_unchanged_at_base(engine, before_schema, before_hashes):
    assert await version(engine) == BASE_REVISION
    assert await scalar(engine, "SELECT to_regclass('public.payment_attempts') IS NULL") is True
    assert await schema_snapshot(engine) == before_schema
    assert await data_hashes(engine, columns_by_table(before_schema)) == before_hashes


# --- backfill --------------------------------------------------------------------------------------

async def test_upgrade_registers_each_provider_from_its_trigger_and_backfills_every_table(migration_db):
    engine, alembic = migration_db
    early, late = T0 - timedelta(minutes=5), T0 + timedelta(minutes=5)
    lower_id, higher_id = uuid.UUID(int=1), uuid.UUID(int=2)
    async with engine.begin() as conn:
        # Razorpay: full chain with an owned terminal event.
        rzp_trigger = await authorized(conn, "pay_rzp00000001", at=early)
        await authorized(conn, "pay_rzp00000001", at=late)
        rzp_capture = await event(conn, source="razorpay", payment_id="pay_rzp00000001", event_type="payment.captured", at=late)
        # Prediction, decision and command are created after the observation and attribution.
        rzp_pred = await prediction(conn, "pay_rzp00000001", created_at=T0 + timedelta(seconds=3))
        rzp_dec = await decision(conn, "pay_rzp00000001", prediction_id=rzp_pred, created_at=T0 + timedelta(seconds=1))
        rzp_cmd = await command(conn, "pay_rzp00000001", rzp_dec, created_at=T0 + timedelta(seconds=2))
        await execution_attempt(conn, rzp_cmd)
        await observation(conn, "pay_rzp00000001", rzp_cmd, terminal_event_id=rzp_capture)
        await attribution(conn, "pay_rzp00000001", rzp_cmd, rzp_dec)
        # Synthetic: prediction only.
        syn_trigger = await authorized(conn, "pay_syn00000001", source="synthetic")
        await prediction(conn, "pay_syn00000001")
        # PayPal: two approvals at the same instant (lower event_id wins), decision without prediction.
        await event(conn, source="paypal", payment_id="ORDER0001", event_type="order.approved", event_id=higher_id)
        await event(conn, source="paypal", payment_id="ORDER0001", event_type="order.approved", event_id=lower_id)
        pp_capture = await event(conn, source="paypal", payment_id="CAPTURE0001", order_id="ORDER0001", event_type="payment.captured", at=late)
        pp_dec = await decision(conn, "paypal-order:ORDER0001")
        pp_cmd = await command(conn, "paypal-order:ORDER0001", pp_dec)
        await execution_attempt(conn, pp_cmd)
        await observation(conn, "paypal-order:ORDER0001", pp_cmd, terminal_event_id=pp_capture)
    base_columns = columns_by_table(await schema_snapshot(engine))
    before_hashes = await data_hashes(engine, base_columns)

    result = await alembic.ok("upgrade", "head")

    assert await version(engine) == REVISION_A
    assert "PT-01" not in result.stderr
    async with engine.connect() as conn:
        registry = {
            r.payment_attempt_id: r
            for r in (await conn.execute(text("SELECT * FROM payment_attempts"))).all()
        }
        assert set(registry) == {"pay_rzp00000001", "pay_syn00000001", "paypal-order:ORDER0001"}
        rzp, syn, pp = registry["pay_rzp00000001"], registry["pay_syn00000001"], registry["paypal-order:ORDER0001"]
        assert (rzp.source_system, rzp.provider_reference, rzp.trigger_event_id) == ("razorpay", "pay_rzp00000001", rzp_trigger)
        # Earliest created_at across all five keyed tables: the observation and attribution (T0).
        assert rzp.registered_at == T0
        assert (syn.source_system, syn.provider_reference, syn.trigger_event_id) == ("synthetic", "pay_syn00000001", syn_trigger)
        assert (pp.source_system, pp.provider_reference, pp.trigger_event_id) == ("paypal", "ORDER0001", lower_id)
        for table, _ in MIGRATION.KEYED_TABLES:
            rows = (await conn.execute(text(f"SELECT payment_attempt_id, source_system FROM {table}"))).all()
            assert all(registry[key].source_system == source for key, source in rows), table
        attempts = (await conn.execute(text(
            "SELECT ea.source_system, c.source_system FROM intervention_execution_attempts ea "
            "JOIN intervention_commands c USING (command_id)"
        ))).all()
        assert len(attempts) == 2 and all(own == parent for own, parent in attempts)
    # Every pre-existing column of every pre-existing table is unchanged.
    assert await data_hashes(engine, base_columns) == before_hashes


# --- preflight aborts -------------------------------------------------------------------------------

async def seed_pf01(conn):
    await authorized(conn, "pay_unknown_src1", source="test_system")
    await prediction(conn, "pay_unknown_src1")


async def seed_pf02(conn):
    await prediction(conn, "paypal-order:BAD:ID")


async def seed_pf02_empty(conn):
    await prediction(conn, "")


async def seed_pf03(conn):
    await event(conn, source="razorpay", payment_id="paypal-order:LOOKALIKE", event_type="payment.authorized")


async def seed_pf04(conn):
    await prediction(conn, "pay_no_trigger01")


async def seed_pf05(conn):
    await authorized(conn, "pay_ambiguous001", source="razorpay")
    await authorized(conn, "pay_ambiguous001", source="synthetic")
    await prediction(conn, "pay_ambiguous001")


async def seed_pf06(conn):
    await authorized(conn, "pay_obs_only0001")
    await authorized(conn, "pay_other0000001")
    other_cmd = await command(conn, "pay_other0000001", await decision(conn, "pay_other0000001"))
    await observation(conn, "pay_obs_only0001", other_cmd)


async def seed_pf07(conn):
    await authorized(conn, "pay_missing_pred")
    await decision(conn, "pay_missing_pred", prediction_id=uuid.uuid4())


async def seed_pf08(conn):
    await authorized(conn, "pay_dec_key00001")
    await authorized(conn, "pay_pred_key0001")
    await decision(conn, "pay_dec_key00001", prediction_id=await prediction(conn, "pay_pred_key0001"))


async def seed_pf09(conn):
    await authorized(conn, "pay_orphan_cmd01")
    await command(conn, "pay_orphan_cmd01", uuid.uuid4())


async def seed_pf10(conn):
    await authorized(conn, "pay_version00001")
    await command(conn, "pay_version00001", await decision(conn, "pay_version00001", version=1), version=2)


async def seed_pf11(conn):
    await execution_attempt(conn, uuid.uuid4())


async def seed_pf12(conn):
    for key in ("pay_obs_key00001", "pay_cmd_key00001"):
        await authorized(conn, key)
    await command(conn, "pay_obs_key00001", await decision(conn, "pay_obs_key00001"))
    other_cmd = await command(conn, "pay_cmd_key00001", await decision(conn, "pay_cmd_key00001"))
    await observation(conn, "pay_obs_key00001", other_cmd)


async def seed_pf13(conn):
    await authorized(conn, "pay_attr_dec0001")
    first = await decision(conn, "pay_attr_dec0001", version=1)
    second = await decision(conn, "pay_attr_dec0001", version=2)
    cmd = await command(conn, "pay_attr_dec0001", first, version=1)
    await attribution(conn, "pay_attr_dec0001", cmd, second)


async def _observed_terminal(conn, terminal_event_id):
    await authorized(conn, "pay_terminal0001")
    cmd = await command(conn, "pay_terminal0001", await decision(conn, "pay_terminal0001"))
    await observation(conn, "pay_terminal0001", cmd, terminal_event_id=terminal_event_id)


async def seed_pf15_missing(conn):
    await _observed_terminal(conn, uuid.uuid4())


async def seed_pf15_provider(conn):
    await _observed_terminal(conn, await event(conn, source="synthetic", payment_id="pay_terminal0001", event_type="payment.captured"))


async def seed_pf15_type(conn):
    await _observed_terminal(conn, await event(conn, source="razorpay", payment_id="pay_terminal0001", event_type="payment.refunded"))


async def seed_pf15_other_attempt(conn):
    await _observed_terminal(conn, await event(conn, source="razorpay", payment_id="pay_someone_else", event_type="payment.captured"))


PREFLIGHT_SEEDS = {
    "PF-01": seed_pf01,
    "PF-02": seed_pf02,
    "PF-02-empty": seed_pf02_empty,
    "PF-03": seed_pf03,
    "PF-04": seed_pf04,
    "PF-05": seed_pf05,
    "PF-06": seed_pf06,
    "PF-07": seed_pf07,
    "PF-08": seed_pf08,
    "PF-09": seed_pf09,
    "PF-10": seed_pf10,
    "PF-11": seed_pf11,
    "PF-12": seed_pf12,
    "PF-13": seed_pf13,
    "PF-15-missing": seed_pf15_missing,
    "PF-15-provider": seed_pf15_provider,
    "PF-15-type": seed_pf15_type,
    "PF-15-other-attempt": seed_pf15_other_attempt,
}


@pytest.mark.parametrize("case", sorted(PREFLIGHT_SEEDS))
async def test_each_preflight_failure_aborts_and_changes_nothing(migration_db, case):
    engine, alembic = migration_db
    async with engine.begin() as conn:
        await PREFLIGHT_SEEDS[case](conn)
    before_schema = await schema_snapshot(engine)
    before_hashes = await data_hashes(engine, columns_by_table(before_schema))

    result = await alembic.run("upgrade", "head")

    code = case[:5]
    assert result.returncode != 0
    assert f"provider_identity preflight {code} failed" in result.stderr
    assert "offending row(s); first 10:" in result.stderr
    await assert_unchanged_at_base(engine, before_schema, before_hashes)


async def test_pt01_reports_suspicious_trigger_timing_without_aborting(migration_db):
    engine, alembic = migration_db
    async with engine.begin() as conn:
        await authorized(conn, "pay_backdated0001", at=T0 + timedelta(hours=1))
        await prediction(conn, "pay_backdated0001", at=T0)

    result = await alembic.ok("upgrade", "head")

    assert "PT-01 (warning only): 1 attempt(s)" in result.stderr
    assert "pay_backdated0001" in result.stderr
    assert await version(engine) == REVISION_A


# --- SQL validity of every check, including defensive ones no consistent seed can reach -------

# The helper's `AS q(ref)` aliases only the first column and PostgreSQL accepts extra ones, so the
# one-text-column contract of every check is asserted here rather than by the helper.
TEXT_TYPES = {"text", "varchar"}


async def result_columns(conn, query):
    """(name, type) of each column the query returns, as described by PostgreSQL itself."""
    raw = await conn.get_raw_connection()
    statement = await raw.driver_connection.prepare(f"SELECT * FROM ({query}) AS q")
    return [(attribute.name, attribute.type.name) for attribute in statement.get_attributes()]


async def assert_one_text_column(conn, code, query):
    columns = await result_columns(conn, query)
    assert len(columns) == 1 and columns[0][1] in TEXT_TYPES, f"{code} returns {columns}"


async def test_every_preflight_query_and_the_helper_execute_on_postgresql(migration_db):
    engine, _ = migration_db
    async with engine.connect() as conn:
        transaction = await conn.begin()
        for statement in (MIGRATION.CREATE_REQUIRE_NONE, MIGRATION.CREATE_KEYS, MIGRATION.CREATE_CLASSIFIED,
                          MIGRATION.CREATE_CANDIDATES, MIGRATION.CREATE_RESOLVED):
            await conn.execute(text(statement))
        for code, _, query in MIGRATION.PREFLIGHT_BEFORE_RESOLUTION + MIGRATION.PREFLIGHT_AFTER_RESOLUTION:
            await assert_one_text_column(conn, code, query)
            assert (await conn.execute(text(f"SELECT count(*) FROM ({query}) q"))).scalar_one() == 0, code
            await conn.execute(text(MIGRATION._require_none(code, "probe", query)))
        assert (await conn.execute(text(MIGRATION.PT01_SUSPICIOUS_TRIGGER_TIMING))).all() == []
        await transaction.rollback()


async def test_registry_postconditions_execute_on_postgresql(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", "head")
    async with engine.connect() as conn:
        transaction = await conn.begin()
        for statement in (MIGRATION.CREATE_REQUIRE_NONE, MIGRATION.CREATE_KEYS):
            await conn.execute(text(statement))
        checks = [MIGRATION.PF18_EVERY_KEY_REGISTERED] + [MIGRATION._pf19(t, pk) for t, pk in MIGRATION.STAGE5_8_TABLES]
        for code, description, query in checks:
            await assert_one_text_column(conn, code, query)
            await conn.execute(text(MIGRATION._require_none(code, description, query)))
        await transaction.rollback()


def _sqlstate(exc) -> str:
    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(candidate, attribute, None)
            if value:
                return value
    raise AssertionError(f"no SQLSTATE found on {exc!r}")


async def test_helper_reports_offending_rows_with_the_documented_message(migration_db):
    engine, _ = migration_db
    with pytest.raises(DBAPIError) as exc_info:
        async with engine.begin() as conn:
            await conn.execute(text(MIGRATION.CREATE_REQUIRE_NONE))
            await conn.execute(text(MIGRATION._require_none(
                "PF-XX", "probe check", "SELECT unnest(ARRAY['b', 'a'])::text"
            )))
    message = str(exc_info.value)
    assert "provider_identity preflight PF-XX failed: probe check. 2 offending row(s); first 10: a, b" in message
    assert _sqlstate(exc_info.value) == "P0001"


async def test_column_contract_probe_detects_extra_and_non_text_columns(migration_db):
    engine, _ = migration_db
    async with engine.connect() as conn:
        for query in ("SELECT 'a'::text, 'b'::text WHERE false", "SELECT gen_random_uuid() WHERE false"):
            with pytest.raises(AssertionError):
                await assert_one_text_column(conn, "PF-XX", query)
        await assert_one_text_column(conn, "PF-XX", "SELECT 'a'::text WHERE false")


# --- registry constraints ---------------------------------------------------------------------------

INVALID_REGISTRY_ROWS = {
    "unsupported-test_system": ("test_system", "pay_x000000001", "pay_x000000001", "ck_payment_attempt_source_system"),
    "unsupported-uppercase": ("RAZORPAY", "pay_x000000001", "pay_x000000001", "ck_payment_attempt_source_system"),
    "unsupported-empty": ("", "pay_x000000001", "pay_x000000001", "ck_payment_attempt_source_system"),
    "paypal-no-prefix": ("paypal", "ORDER0001", "ORDER0001", "ck_payment_attempt_namespace"),
    "paypal-reference-mismatch": ("paypal", "paypal-order:ORDER0001", "ORDER0002", "ck_payment_attempt_namespace"),
    "paypal-empty-reference": ("paypal", "paypal-order:", "", "ck_payment_attempt_namespace"),
    "paypal-colon": ("paypal", "paypal-order:AB:CD", "AB:CD", "ck_payment_attempt_namespace"),
    "paypal-space": ("paypal", "paypal-order:AB CD", "AB CD", "ck_payment_attempt_namespace"),
    "paypal-leading-dash": ("paypal", "paypal-order:-ABC", "-ABC", "ck_payment_attempt_namespace"),
    "paypal-leading-underscore": ("paypal", "paypal-order:_ABC", "_ABC", "ck_payment_attempt_namespace"),
    "paypal-129-chars": ("paypal", "paypal-order:" + "A" * 129, "A" * 129, "ck_payment_attempt_namespace"),
    "razorpay-prefixed": ("razorpay", "paypal-order:ORDER0001", "paypal-order:ORDER0001", "ck_payment_attempt_namespace"),
    "razorpay-key-mismatch": ("razorpay", "pay_x000000001", "pay_y000000001", "ck_payment_attempt_namespace"),
    "razorpay-empty": ("razorpay", "", "", "ck_payment_attempt_namespace"),
    "synthetic-prefixed": ("synthetic", "paypal-order:ORDER0001", "paypal-order:ORDER0001", "ck_payment_attempt_namespace"),
    "synthetic-key-mismatch": ("synthetic", "pay_syn_1", "pay_syn_2", "ck_payment_attempt_namespace"),
}


async def _registry_insert(conn, source, key, reference, trigger_event_id):
    await insert(conn, "payment_attempts", payment_attempt_id=key, source_system=source,
                 provider_reference=reference, trigger_event_id=trigger_event_id, registered_at=T0)


@pytest.mark.parametrize("case", sorted(INVALID_REGISTRY_ROWS))
async def test_registry_rejects_invalid_rows_at_the_statement(migration_db, case):
    engine, alembic = migration_db
    await alembic.ok("upgrade", "head")
    source, key, reference, constraint = INVALID_REGISTRY_ROWS[case]
    async with engine.begin() as conn:
        trigger = await event(conn, source="razorpay", payment_id="pay_trigger0001", event_type="payment.authorized")
    with pytest.raises(IntegrityError) as exc_info:
        async with engine.begin() as conn:
            await _registry_insert(conn, source, key, reference, trigger)
    assert constraint in str(exc_info.value)
    assert await scalar(engine, "SELECT count(*) FROM payment_attempts") == 0


@pytest.mark.parametrize(
    "source, key, reference",
    [
        ("razorpay", "pay_valid000001", "pay_valid000001"),
        ("synthetic", "pay_syn_valid01", "pay_syn_valid01"),
        ("paypal", "paypal-order:A", "A"),
        ("paypal", "paypal-order:" + "Z" * 128, "Z" * 128),
    ],
)
async def test_registry_accepts_valid_rows(migration_db, source, key, reference):
    engine, alembic = migration_db
    await alembic.ok("upgrade", "head")
    async with engine.begin() as conn:
        trigger = await event(conn, source=source, payment_id=reference, event_type="payment.authorized")
        await _registry_insert(conn, source, key, reference, trigger)
    assert await scalar(engine, "SELECT count(*) FROM payment_attempts") == 1


async def test_registry_trigger_must_belong_to_the_same_provider(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", "head")
    async with engine.begin() as conn:
        trigger = await event(conn, source="synthetic", payment_id="pay_cross000001", event_type="payment.authorized")
    with pytest.raises(IntegrityError, match="fk_payment_attempt_trigger"):
        async with engine.begin() as conn:
            await _registry_insert(conn, "razorpay", "pay_cross000001", "pay_cross000001", trigger)


async def test_new_constraints_are_validated_and_not_deferrable(migration_db):
    engine, alembic = migration_db
    await alembic.ok("upgrade", "head")
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT conname, convalidated, condeferrable FROM pg_constraint WHERE conname = ANY(:names)"
        ), {"names": list(NEW_CONSTRAINTS)})).all()
    assert {r.conname for r in rows} == set(NEW_CONSTRAINTS)
    assert all(r.convalidated and not r.condeferrable for r in rows)


@pytest.mark.parametrize("identifier, accepted", PAYPAL_ID_CORPUS)
async def test_postgresql_and_the_parser_accept_the_same_paypal_identifiers(migration_db, identifier, accepted):
    engine, _ = migration_db
    matched = await scalar(engine, "SELECT CAST(:value AS text) ~ CAST(:pattern AS text)",
                           value=identifier, pattern=PAYPAL_IDENTIFIER_PATTERN)
    assert matched is accepted


# --- downgrade exactness and transactional behaviour -----------------------------------------------

async def test_downgrade_restores_the_prior_schema_and_data_exactly(migration_db):
    engine, alembic = migration_db
    async with engine.begin() as conn:
        await authorized(conn, "pay_roundtrip001")
        await prediction(conn, "pay_roundtrip001")
    base_schema = await schema_snapshot(engine)
    base_hashes = await data_hashes(engine, columns_by_table(base_schema))

    await alembic.ok("upgrade", "head")
    head_schema = await schema_snapshot(engine)
    await alembic.ok("downgrade", BASE_REVISION)
    await assert_unchanged_at_base(engine, base_schema, base_hashes)

    await alembic.ok("upgrade", "head")
    assert await schema_snapshot(engine) == head_schema
    fk = {r[1]: r[3] for r in base_schema["constraints"] if r[1] in ("fk_attribution_command", "fk_attribution_decision")}
    assert fk == {
        "fk_attribution_command": "FOREIGN KEY (command_id) REFERENCES intervention_commands(command_id)",
        "fk_attribution_decision": "FOREIGN KEY (decision_id) REFERENCES intervention_decisions(decision_id)",
    }


ALEMBIC_WITH_PATCH = """
import sys
import src.infrastructure.db_safety as safety

def _fail(*args, **kwargs):
    raise RuntimeError("injected {where}")

safety.{name} = _fail
from alembic.config import main
main(argv=sys.argv[1:], prog="alembic")
"""


async def test_a_failure_after_the_migrations_ran_rolls_back_the_whole_run(migration_db):
    engine, alembic = migration_db
    async with engine.begin() as conn:
        await authorized(conn, "pay_rollback0001")
        await prediction(conn, "pay_rollback0001")
    before_schema = await schema_snapshot(engine)
    before_hashes = await data_hashes(engine, columns_by_table(before_schema))

    result = await alembic.run("upgrade", "head", bootstrap=ALEMBIC_WITH_PATCH.format(
        where="failure after run_migrations", name="final_state_requires_assertion"))

    assert result.returncode != 0
    assert f"Running upgrade {BASE_REVISION} -> {REVISION_A}" in result.stderr
    assert "injected failure after run_migrations" in result.stderr
    await assert_unchanged_at_base(engine, before_schema, before_hashes)


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

ASSERTION_EXPECTING_ANOTHER_DATABASE = """
import sys
import src.infrastructure.db_safety as safety

safety.INTERMEDIATE_REVISION_ASSERTION_SQL = safety.INTERMEDIATE_REVISION_ASSERTION_SQL.replace(
    "'payment_recovery_test'", "'some_persistent_database'", 1
)
from alembic.config import main
main(argv=sys.argv[1:], prog="alembic")
"""


async def test_an_intermediate_end_state_is_refused_online_for_a_non_test_target_and_rolled_back(migration_db):
    engine, alembic = migration_db
    before_schema = await schema_snapshot(engine)
    before_hashes = await data_hashes(engine, columns_by_table(before_schema))

    result = await alembic.run("upgrade", "head", bootstrap=AS_IF_NON_TEST_TARGET)

    assert result.returncode != 0
    assert f"Running upgrade {BASE_REVISION} -> {REVISION_A}" in result.stderr
    assert f"at intermediate revision(s) {REVISION_A}" in result.stderr
    await assert_unchanged_at_base(engine, before_schema, before_hashes)


async def test_the_intermediate_state_assertion_aborts_the_run_on_any_other_database(migration_db):
    engine, alembic = migration_db
    before_schema = await schema_snapshot(engine)
    before_hashes = await data_hashes(engine, columns_by_table(before_schema))

    result = await alembic.run("upgrade", "head", bootstrap=ASSERTION_EXPECTING_ANOTHER_DATABASE)

    assert result.returncode != 0
    assert f"Running upgrade {BASE_REVISION} -> {REVISION_A}" in result.stderr
    assert "Phase 3 Revision A may only remain applied on payment_recovery_test" in result.stderr
    await assert_unchanged_at_base(engine, before_schema, before_hashes)


async def test_the_connected_database_is_verified_before_any_migration_runs(migration_db):
    engine, alembic = migration_db
    before_schema = await schema_snapshot(engine)
    before_hashes = await data_hashes(engine, columns_by_table(before_schema))

    result = await alembic.run("upgrade", "head", bootstrap=ALEMBIC_WITH_PATCH.format(
        where="connected-database mismatch", name="assert_connected_database"))

    assert result.returncode != 0
    assert "injected connected-database mismatch" in result.stderr
    assert "Running upgrade" not in result.stderr
    await assert_unchanged_at_base(engine, before_schema, before_hashes)
