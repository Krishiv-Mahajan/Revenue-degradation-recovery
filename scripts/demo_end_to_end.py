"""
Reproducible end-to-end demonstration of Stages 1-8, driven through the real webhook route.

What happens (every stage decides on its own; nothing is forced):
1. Context: four weeks of synthetic baseline windows and a synthetic card outage are generated with
   scripts/generate_live_context.py, and Stages 2-4 run over them (degradation episode + RCA).
2. Events enter the system: an outage burst of Razorpay card payments (most failing) is sent as
   HMAC-signed webhooks to POST /ingest/razorpay, so each is verified, stored with its pipeline run
   (outbox) and processed by the live pipeline.
3. A target card payment is authorized through the same route: Stage 5 predicts its failure risk with
   the configured live model, Stage 6 decides, Stage 7 executes the selected route.
4. The payment is then captured (webhook); Stage 7 observes the outcome and Stage 8 attributes it.
5. A report prints every stage's record with its provenance: which parts are generated, simulated or real.

Execution is real only for routes the PayPal sandbox executor implements, when
PAYPAL_RECOVERY_EXECUTION_ENABLED=true and sandbox credentials are configured; otherwise the labelled
simulator runs. The report says which, and why.

Database safety: DATABASE_URL must name the disposable test database (localhost:5434/
payment_recovery_test) or a demo-allowlisted database; the schema must already be at Alembic head.
--reset truncates application tables first, only after the server confirms the database name.

Usage:
    DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5434/payment_recovery_test \\
        python -m scripts.demo_end_to_end --reset
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone

OUTAGE_PAYMENTS = 60
OUTAGE_FAILURE_SHARE = 0.6
AMOUNT_MINOR_UNITS = 250000
CAPTURE_DELAY_SECONDS = 30


def _target_database(raw_url: str):
    from src.infrastructure.db_safety import (
        UnsafeDatabaseError,
        assert_disposable_test_target,
        validate_demo_reset_database_url,
    )

    try:
        return assert_disposable_test_target(raw_url), "disposable test database"
    except UnsafeDatabaseError:
        pass
    try:
        return validate_demo_reset_database_url(raw_url), "demo database"
    except UnsafeDatabaseError as exc:
        raise SystemExit(f"Refusing to run the demo: {exc}")


def _webhook(event: str, payment_id: str, created_at: int, failed: bool = False) -> bytes:
    entity = {
        "id": payment_id, "entity": "payment", "amount": AMOUNT_MINOR_UNITS, "currency": "INR",
        "status": event.split(".", 1)[1], "order_id": f"order_{payment_id[4:]}", "method": "card",
        "created_at": created_at,
    }
    if failed:
        entity.update({
            "error_code": "BAD_REQUEST_ERROR", "error_description": "Payment failed",
            "error_source": "bank", "error_step": "payment_authorization", "error_reason": "payment_failed",
        })
    body = {
        "entity": "event", "account_id": "acc_DemoAccount01", "event": event, "contains": ["payment"],
        "payload": {"payment": {"entity": entity}}, "created_at": created_at,
    }
    return json.dumps(body, sort_keys=True).encode()


async def _post(client, secret: str, body: bytes, source_event_id: str):
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = await client.post(
        "/ingest/razorpay", content=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": source_event_id},
    )
    if response.status_code != 200:
        raise SystemExit(f"Webhook {source_event_id} was rejected: HTTP {response.status_code} {response.text}")


async def run(reset: bool) -> dict:
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import func, select, text

    from scripts.generate_live_context import (
        compute_tumbling_windows, run_stage2_analytics, run_stage3_degradation, run_stage4_rca,
        seed_historical_baselines, seed_upstream_synthetic_traffic,
    )
    from src.infrastructure.database import AsyncSessionLocal
    from src.infrastructure.db_safety import assert_connected_database, build_truncate_statement
    from src.infrastructure.models import (
        Base, CounterfactualAttributionModel, DegradationEpisodeModel, FailurePredictionModel,
        InterventionCommandModel, InterventionDecisionModel, InterventionExecutionAttemptModel,
        PaymentOutcomeObservationModel, PipelineRunModel, RCAEvaluationModel,
    )
    from src.infrastructure.paypal_recovery_executor import SUPPORTED_ROUTES, recovery_execution_enabled
    from src.main import app

    target, _ = _target_database(os.environ["DATABASE_URL"])
    async with AsyncSessionLocal() as session:
        assert_connected_database((await session.execute(text("SELECT current_database()"))).scalar_one(), target)
        if reset:
            await session.execute(text(build_truncate_statement(Base.metadata)))
            await session.commit()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = uuid.uuid4().hex[:8]

    # 1. Generated context: baselines and a synthetic card outage, Stages 2-4 over them.
    _, _, windows = compute_tumbling_windows(now)
    async with AsyncSessionLocal() as session:
        await seed_historical_baselines(session, windows, "card", now)
        await seed_upstream_synthetic_traffic(session, windows, "card", "INR", 42)
        await run_stage2_analytics(session, windows)
        active = await run_stage3_degradation(session, windows)
        await run_stage4_rca(session, active, "card")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://demo") as client:
        secret = os.environ["RAZORPAY_WEBHOOK_SECRET"]
        # 2. Outage burst through the real webhook route (verified, stored, pipeline-run).
        failed_count = int(OUTAGE_PAYMENTS * OUTAGE_FAILURE_SHARE)
        for i in range(OUTAGE_PAYMENTS):
            at = int((now - timedelta(minutes=20) + timedelta(seconds=15 * i)).timestamp())
            failed = i < failed_count
            event = "payment.failed" if failed else "payment.captured"
            pid = f"pay_Demo{run_id}{i:03d}"
            await _post(client, secret, _webhook(event, pid, at, failed=failed), f"evt_demo_{run_id}_{i:03d}")

        # 3-4. Target payment: authorization, then capture.
        target_pid = f"pay_DemoTarget{run_id}"
        await _post(client, secret, _webhook("payment.authorized", target_pid, int(now.timestamp())), f"evt_demo_auth_{run_id}")
        await _post(client, secret, _webhook("payment.captured", target_pid, int(now.timestamp()) + CAPTURE_DELAY_SECONDS),
                    f"evt_demo_capture_{run_id}")

    async with AsyncSessionLocal() as session:
        async def latest(model, *conditions, order):
            return (await session.execute(select(model).where(*conditions).order_by(order.desc()).limit(1))).scalar_one_or_none()

        episodes = (await session.execute(
            select(DegradationEpisodeModel).where(DegradationEpisodeModel.status == "ACTIVE")
        )).scalars().all()
        rca = await latest(RCAEvaluationModel, order=RCAEvaluationModel.generated_at)
        prediction = await latest(FailurePredictionModel, FailurePredictionModel.payment_attempt_id == target_pid,
                                  order=FailurePredictionModel.prediction_version)
        decision = await latest(InterventionDecisionModel, InterventionDecisionModel.payment_attempt_id == target_pid,
                                order=InterventionDecisionModel.decision_version)
        command = await latest(InterventionCommandModel, InterventionCommandModel.payment_attempt_id == target_pid,
                               order=InterventionCommandModel.created_at)
        execution = observation = attribution = None
        if command is not None:
            execution = await latest(InterventionExecutionAttemptModel, InterventionExecutionAttemptModel.command_id == command.command_id,
                                     order=InterventionExecutionAttemptModel.attempt_number)
            observation = await latest(PaymentOutcomeObservationModel, PaymentOutcomeObservationModel.command_id == command.command_id,
                                       order=PaymentOutcomeObservationModel.observation_version)
            attribution = await latest(CounterfactualAttributionModel, CounterfactualAttributionModel.command_id == command.command_id,
                                       order=CounterfactualAttributionModel.attribution_version)
        runs = dict((await session.execute(
            select(PipelineRunModel.status, func.count()).group_by(PipelineRunModel.status)
        )).all())

    route = decision.selected_route_id if decision else None
    real_route = route is not None and any(route == r.value for r in SUPPORTED_ROUTES)
    return {
        "run_id": run_id,
        "events_entered": {"outage_webhooks": OUTAGE_PAYMENTS, "failed": failed_count, "target_webhooks": 2,
                           "pipeline_runs_by_status": runs,
                           "provenance": "generated by this demo, HMAC-signed with a demo secret; no real Razorpay traffic"},
        "degradation": [{"segment": f"{e.segment_dimension}={e.segment_value}", "severity": e.severity,
                         "peak_absolute_drop": round(e.peak_absolute_drop, 4)} for e in episodes],
        "root_cause": None if rca is None else {"classification": rca.classification,
                                                "provenance": "computed over synthetic context traffic"},
        "prediction": None if prediction is None else {
            "status": prediction.prediction_status, "failure_probability": prediction.failure_probability,
            "model": f"{prediction.model_name} {prediction.model_version}",
            "provenance": "heuristic, not validated on real outcomes" if prediction.model_name == "DeterministicBaselineModel" else "artifact"},
        "decision": None if decision is None else {
            "type": decision.decision_type, "route": route, "gate_verdict": decision.gate_verdict,
            "decision_confidence": decision.decision_confidence, "policy": f"{decision.policy_id} {decision.policy_version}"},
        "execution": None if execution is None else {
            "status": execution.execution_status, "executor": execution.executor_name, "is_simulation": execution.is_simulation,
            "why": ("PayPal sandbox recovery checkout" if not execution.is_simulation else
                    f"route {route} has no PayPal action" if not real_route else
                    "PayPal recovery execution is not enabled/configured (PAYPAL_RECOVERY_EXECUTION_ENABLED, sandbox credentials)"
                    if not recovery_execution_enabled() else "PayPal configuration missing or refused; see logs"),
            "recovery_checkout": (execution.provider_response_payload or {}).get("approve_url")},
        "outcome": None if observation is None else {
            "observed": observation.payment_outcome, "terminal_event_type": observation.terminal_event_type},
        "revenue_impact": None if attribution is None else {
            "status": attribution.attribution_status, "currency": "INR",
            "payment_amount_minor_units": attribution.payment_amount_minor_units,
            "protected_gmv_minor_units": attribution.attributed_protected_gmv_minor_units,
            "attribution_confidence": float(attribution.attribution_confidence),
            "is_simulated_execution": attribution.is_simulated_execution,
            "is_synthetic_baseline": attribution.is_synthetic_baseline,
            "meaning": "risk-weighted counterfactual estimate under explicit assumptions, not observed recovered revenue"},
    }


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the reproducible Stage 1-8 demonstration.")
    parser.add_argument("--reset", action="store_true", help="truncate application tables first")
    args = parser.parse_args()

    raw_url = os.environ.get("DATABASE_URL", "")
    if not raw_url:
        print("DATABASE_URL is not set; point it at the disposable test database.", file=sys.stderr)
        return 2
    _target_database(raw_url)
    # The demo signs its own webhooks; the secret never leaves this process.
    os.environ["RAZORPAY_WEBHOOK_SECRET"] = f"demo-{secrets.token_hex(16)}"
    os.environ.setdefault("ALLOWED_HOSTS", "demo")
    print(json.dumps(asyncio.run(run(args.reset)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
