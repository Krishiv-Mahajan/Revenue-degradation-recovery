"""
Database-level proof (PostgreSQL, migrated to head) that linked Stage 5-8 records must belong to the
same payment attempt and provider. Rows are written with raw SQL, so no application validation runs:
every rejection here comes from a constraint added by Revision C (f4a5b6c7d8e9).

Where one bad row violates two foreign keys, PostgreSQL does not promise which it reports, so each
key is also proven alone by dropping the other inside a transaction that is always rolled back.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
FOREIGN_KEY_VIOLATION = "23503"

EXPECTED_DEFINITIONS = {
    "uq_failure_prediction_attempt_link": "UNIQUE (prediction_id, payment_attempt_id, source_system)",
    "uq_intervention_decision_attempt_link": "UNIQUE (decision_id, payment_attempt_id, source_system)",
    "uq_cmd_attempt_link": "UNIQUE (command_id, payment_attempt_id, source_system)",
    "fk_decision_prediction":
        "FOREIGN KEY (stage5_prediction_id, source_system) "
        "REFERENCES failure_predictions(prediction_id, source_system)",
    "fk_decision_prediction_attempt":
        "FOREIGN KEY (stage5_prediction_id, payment_attempt_id, source_system) "
        "REFERENCES failure_predictions(prediction_id, payment_attempt_id, source_system)",
    "fk_command_decision_attempt":
        "FOREIGN KEY (decision_id, payment_attempt_id, source_system) "
        "REFERENCES intervention_decisions(decision_id, payment_attempt_id, source_system)",
    "fk_observation_command_attempt":
        "FOREIGN KEY (command_id, payment_attempt_id, source_system) "
        "REFERENCES intervention_commands(command_id, payment_attempt_id, source_system)",
    "fk_attribution_command_attempt":
        "FOREIGN KEY (command_id, payment_attempt_id, source_system) "
        "REFERENCES intervention_commands(command_id, payment_attempt_id, source_system)",
    "fk_attribution_decision_attempt":
        "FOREIGN KEY (decision_id, payment_attempt_id, source_system) "
        "REFERENCES intervention_decisions(decision_id, payment_attempt_id, source_system)",
}


async def _insert(conn, table, **values):
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    await conn.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({params})"), values)


def _prediction(key, source, version=1):
    pred_id = uuid.uuid4()
    return {
        "prediction_id": pred_id, "payment_attempt_id": key, "source_system": source,
        "prediction_version": version, "predicted_at": T0, "prediction_horizon": "30m",
        "failure_probability": 0.8, "risk_band": "HIGH", "prediction_status": "PREDICTED",
        "model_name": "m", "model_version": "1", "feature_schema_version": "1",
        "feature_snapshot": json.dumps({}), "input_fingerprint": f"sha256:{pred_id.hex}", "created_at": T0,
    }


def _decision(key, source, pred_id, version=1):
    dec_id = uuid.uuid4()
    return {
        "decision_id": dec_id, "payment_attempt_id": key, "source_system": source,
        "decision_version": version, "decided_at": T0, "decision_type": "ACT",
        "selected_route_id": "ROUTE_A", "failure_probability": 0.8, "stage5_prediction_id": pred_id,
        "diagnosis_confidence": "STRONG", "decision_confidence": 0.9, "gate_verdict": "PASSED",
        "policy_id": "P1", "policy_version": "1.0", "input_fingerprint": f"sha256:{dec_id.hex}",
        "evaluation_audit_payload": json.dumps({}), "created_at": T0,
    }


def _command(key, source, dec_id, version=1):
    cmd_id = uuid.uuid4()
    return {
        "command_id": cmd_id, "decision_id": dec_id, "payment_attempt_id": key, "source_system": source,
        "decision_version": version, "route_key": "ROUTE_A", "policy_id": "P1", "policy_version": "1.0",
        "command_status": "SUCCEEDED", "idempotency_key": f"cmd_{cmd_id.hex}", "created_at": T0,
        "expires_at": T0 + timedelta(minutes=5), "updated_at": T0,
    }


def _observation(key, source, cmd_id, version=1):
    as_of = T0 + timedelta(minutes=version)
    return {
        "observation_id": uuid.uuid4(), "command_id": cmd_id, "payment_attempt_id": key, "source_system": source,
        "observation_version": version, "observed_at": as_of, "as_of_timestamp": as_of,
        "payment_outcome": "CAPTURED", "observation_audit_payload": json.dumps({}), "created_at": as_of,
    }


def _attribution(key, source, cmd_id, dec_id, version=1):
    as_of = T0 + timedelta(minutes=version)
    return {
        "attribution_id": uuid.uuid4(), "command_id": cmd_id, "payment_attempt_id": key, "source_system": source,
        "decision_id": dec_id, "attribution_version": version, "attributed_at": as_of, "as_of_timestamp": as_of,
        "attribution_status": "ATTRIBUTED", "methodology_name": "M1", "methodology_version": "1.0",
        "treatment_status": "TREATED", "observed_payment_outcome": "CAPTURED", "payment_amount_minor_units": 50000,
        "counterfactual_failure_probability": 0.8, "counterfactual_loss_exposure_minor_units": 1000,
        "counterfactual_natural_success_gmv_minor_units": 49000, "attributed_protected_gmv_minor_units": 1000,
        "attribution_confidence": 0.9, "is_synthetic_baseline": True, "is_simulated_execution": True,
        "attribution_audit_payload": json.dumps({}), "created_at": as_of,
    }


async def _seed_trace(conn, *, source, key, reference=None):
    """A registered attempt with one valid, fully linked Stage 5-8 trace."""
    reference = reference or key
    trigger_id = uuid.uuid4()
    await _insert(
        conn, "payment_events",
        event_id=trigger_id, source_system=source, source_event_id=f"evt_{trigger_id.hex}", payment_id=reference,
        timestamp=T0, event_type="order.approved" if source == "paypal" else "payment.authorized",
        currency="INR", amount_minor_units=50000, payment_status="authorized", ingested_at=T0,
    )
    await _insert(
        conn, "payment_attempts",
        payment_attempt_id=key, source_system=source, provider_reference=reference,
        trigger_event_id=trigger_id, registered_at=T0,
    )
    pred = _prediction(key, source)
    await _insert(conn, "failure_predictions", **pred)
    dec = _decision(key, source, pred["prediction_id"])
    await _insert(conn, "intervention_decisions", **dec)
    cmd = _command(key, source, dec["decision_id"])
    await _insert(conn, "intervention_commands", **cmd)
    await _insert(conn, "payment_outcome_observations", **_observation(key, source, cmd["command_id"]))
    await _insert(
        conn, "counterfactual_attributions", **_attribution(key, source, cmd["command_id"], dec["decision_id"])
    )
    return {"key": key, "source": source, "pred_id": pred["prediction_id"],
            "dec_id": dec["decision_id"], "cmd_id": cmd["command_id"]}


@pytest.fixture
async def traces(db_engine):
    """Razorpay attempts A and B (same provider), and synthetic attempt S (another provider)."""
    suffix = uuid.uuid4().hex[:8]
    async with db_engine.begin() as conn:
        a = await _seed_trace(conn, source="razorpay", key=f"pay_link_a_{suffix}")
        b = await _seed_trace(conn, source="razorpay", key=f"pay_link_b_{suffix}")
        s = await _seed_trace(conn, source="synthetic", key=f"pay_link_s_{suffix}")
        # A second decision for B with no command yet, so a command can target it without uq_cmd_decision.
        b_dec2 = _decision(b["key"], "razorpay", b["pred_id"], version=2)
        await _insert(conn, "intervention_decisions", **b_dec2)
    b["dec2_id"] = b_dec2["decision_id"]
    return a, b, s


async def _rejected_by(engine, table, values, *, without=()):
    """
    The constraint PostgreSQL reports when inserting `values`, or None if the row is accepted.
    Constraints named in `without` are dropped first; the transaction is always rolled back, so
    neither the row nor the schema change persists.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            for name in without:
                await conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {name}"))
            try:
                async with conn.begin_nested():
                    await _insert(conn, table, **values)
            except IntegrityError as exc:
                cause = exc.orig.__cause__
                assert cause.sqlstate == FOREIGN_KEY_VIOLATION, str(exc)
                return cause.constraint_name
            return None
        finally:
            await trans.rollback()


async def test_revision_c_constraints_exist_with_exact_definitions(db_engine):
    async with db_engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT conname, pg_get_constraintdef(oid), convalidated, condeferrable FROM pg_constraint "
            "WHERE conname = ANY(:names)"
        ), {"names": list(EXPECTED_DEFINITIONS)})).all()
    assert {name: definition for name, definition, _, _ in rows} == EXPECTED_DEFINITIONS
    assert all(validated and not deferrable for _, _, validated, deferrable in rows)


async def test_decision_referencing_a_nonexistent_prediction_is_rejected(db_engine, traces):
    a, _, _ = traces
    row = _decision(a["key"], "razorpay", uuid.uuid4(), version=2)
    both = {"fk_decision_prediction", "fk_decision_prediction_attempt"}
    assert await _rejected_by(db_engine, "intervention_decisions", row) in both
    assert await _rejected_by(
        db_engine, "intervention_decisions", row, without=("fk_decision_prediction_attempt",)
    ) == "fk_decision_prediction"
    assert await _rejected_by(
        db_engine, "intervention_decisions", row, without=("fk_decision_prediction",)
    ) == "fk_decision_prediction_attempt"


async def test_decision_referencing_another_providers_prediction_is_rejected(db_engine, traces):
    a, _, s = traces
    row = _decision(a["key"], "razorpay", s["pred_id"], version=2)
    both = {"fk_decision_prediction", "fk_decision_prediction_attempt"}
    assert await _rejected_by(db_engine, "intervention_decisions", row) in both
    assert await _rejected_by(
        db_engine, "intervention_decisions", row, without=("fk_decision_prediction_attempt",)
    ) == "fk_decision_prediction"
    assert await _rejected_by(
        db_engine, "intervention_decisions", row, without=("fk_decision_prediction",)
    ) == "fk_decision_prediction_attempt"


async def test_decision_referencing_another_attempts_prediction_of_the_same_provider_is_rejected(db_engine, traces):
    a, b, _ = traces
    row = _decision(a["key"], "razorpay", b["pred_id"], version=2)
    assert await _rejected_by(db_engine, "intervention_decisions", row) == "fk_decision_prediction_attempt"


async def test_command_referencing_another_attempts_decision_is_rejected(db_engine, traces):
    a, b, _ = traces
    row = _command(a["key"], "razorpay", b["dec2_id"], version=2)
    assert await _rejected_by(db_engine, "intervention_commands", row) == "fk_command_decision_attempt"


async def test_observation_referencing_another_attempts_command_is_rejected(db_engine, traces):
    a, b, _ = traces
    row = _observation(a["key"], "razorpay", b["cmd_id"], version=2)
    assert await _rejected_by(db_engine, "payment_outcome_observations", row) == "fk_observation_command_attempt"


async def test_attribution_referencing_another_attempts_command_is_rejected(db_engine, traces):
    a, b, _ = traces
    row = _attribution(a["key"], "razorpay", b["cmd_id"], a["dec_id"], version=2)
    assert await _rejected_by(db_engine, "counterfactual_attributions", row) == "fk_attribution_command_attempt"


async def test_attribution_referencing_another_attempts_decision_is_rejected(db_engine, traces):
    a, b, _ = traces
    row = _attribution(a["key"], "razorpay", a["cmd_id"], b["dec_id"], version=2)
    assert await _rejected_by(db_engine, "counterfactual_attributions", row) == "fk_attribution_decision_attempt"


async def test_valid_same_attempt_links_are_accepted_for_every_provider(db_engine, traces):
    """Seeding already wrote one linked trace per provider; a further linked version also commits."""
    suffix = uuid.uuid4().hex[:8]
    async with db_engine.begin() as conn:
        paypal = await _seed_trace(
            conn, source="paypal", key=f"paypal-order:LINK{suffix}", reference=f"LINK{suffix}"
        )
        for trace in (*traces, paypal):
            key, source = trace["key"], trace["source"]
            dec = _decision(key, source, trace["pred_id"], version=3)
            await _insert(conn, "intervention_decisions", **dec)
            cmd = _command(key, source, dec["decision_id"], version=3)
            await _insert(conn, "intervention_commands", **cmd)
            await _insert(conn, "payment_outcome_observations", **_observation(key, source, cmd["command_id"], version=2))
            await _insert(conn, "counterfactual_attributions",
                          **_attribution(key, source, cmd["command_id"], dec["decision_id"], version=2))

    async with db_engine.connect() as conn:
        counts = (await conn.execute(text(
            "SELECT source_system, count(*) FROM intervention_commands GROUP BY source_system ORDER BY 1"
        ))).all()
    assert counts == [("paypal", 2), ("razorpay", 4), ("synthetic", 2)]


async def test_decision_without_a_stage5_prediction_is_still_accepted(db_engine, traces):
    """A NULL stage5_prediction_id (no Stage 5 prediction) is not checked by the foreign keys (MATCH SIMPLE)."""
    a, _, _ = traces
    row = _decision(a["key"], "razorpay", None, version=2)
    assert await _rejected_by(db_engine, "intervention_decisions", row) is None
