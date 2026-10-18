"""
End-to-end tests of the live recovery pipeline.

Signed Razorpay webhooks go through the real POST /ingest/razorpay route, and the route's background
task runs the real run_live_pipeline (Stages 2-8) against the validated TEST_DATABASE_URL only.

Test seams, all installed by the e2e fixture and restored afterwards:
- app.dependency_overrides: the test database session, a test webhook secret, the real
  get_ingestion_service (tests/api/test_routes.py installs a mock at import time), and a PayPal
  ingestion service whose verifier accepts every webhook without contacting PayPal.
- live_pipeline.AsyncSessionLocal: the background task's session factory, bound to the test database.
- live_pipeline.get_development_model: replaced only when a test asks for a fixed failure probability.

The route discards the background task's return value, so route-triggered runs are checked through the
pipeline's own [LivePipelineSuccess] / [LivePipelineError] log records; direct replays assert the returned dict.

Stage 5 only predicts when the payment's provider has enough global terminal volume, so most tests seed
Razorpay history three hours back (24-hour failure rate 0.0), outside every window the pipeline recomputes.

Some tests pin current behavior, not desired behavior, and must be revised deliberately if it changes:
- the runner calls Stage 6 only when p > 0.5, so the policy's MONITOR band [0.35, 0.5] is never recorded;
- a duplicate webhook's response carries a fresh event_id, not the stored one;
- replaying a terminal event fails at Stage 7 on the (command_id, as_of_timestamp) unique constraint.
"""
import hashlib
import hmac
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts.generate_live_context import (
    compute_tumbling_windows,
    run_stage2_analytics,
    run_stage3_degradation,
    run_stage4_rca,
    seed_historical_baselines,
    seed_upstream_synthetic_traffic,
)
from src.api.dependencies import (
    get_ingestion_service,
    get_paypal_ingestion_service,
    get_razorpay_webhook_secret,
)
from src.core.domain.models import PaymentEvent
from src.core.ml.model import DeterministicBaselineModel, FailurePredictionModel as StageFiveModel
from src.core.services import live_pipeline
from src.core.services.paypal_ingestion_service import PayPalIngestionService
from src.infrastructure.database import get_db_session
from src.infrastructure.models import (
    CandidateCauseModel,
    CounterfactualAttributionModel,
    DegradationEpisodeModel,
    DegradationSignalModel,
    EpisodeStateHistoryModel,
    FailurePredictionModel,
    InterventionCommandModel,
    InterventionDecisionModel,
    InterventionExecutionAttemptModel,
    PaymentEventModel,
    PaymentHealthSnapshotModel,
    PaymentOutcomeObservationModel,
    RawIngestionRecordModel,
    RCAEvaluationModel,
)
from src.infrastructure.repository import SQLIngestionRepository
from src.main import app
from tests.unit import paypal_event_samples as samples

WEBHOOK_SECRET = "e2e-test-webhook-secret"
AMOUNT = 50000
CAPTURE_DELAY_SECONDS = 30
# The default policy's routes that need no RCA diagnosis; the only ones eligible without an episode.
NO_DIAGNOSIS_ROUTES = {"DYNAMIC_RETRY_BACKOFF", "FALLBACK_PAYMENT_LINK"}
PAUSE_SIGNALS = {"LOW_VOLUME", "NO_BASELINE"}

TABLES = (
    RawIngestionRecordModel,
    PaymentEventModel,
    PaymentHealthSnapshotModel,
    DegradationSignalModel,
    DegradationEpisodeModel,
    EpisodeStateHistoryModel,
    RCAEvaluationModel,
    CandidateCauseModel,
    FailurePredictionModel,
    InterventionDecisionModel,
    InterventionCommandModel,
    InterventionExecutionAttemptModel,
    PaymentOutcomeObservationModel,
    CounterfactualAttributionModel,
)
STAGE_5_TO_8_TABLES = {
    model.__tablename__ for model in (
        FailurePredictionModel,
        InterventionDecisionModel,
        InterventionCommandModel,
        InterventionExecutionAttemptModel,
        PaymentOutcomeObservationModel,
        CounterfactualAttributionModel,
    )
}


class SyntheticFixedProbabilityTestModel(StageFiveModel):
    """Test double. Its name contains "synthetic", so Stage 8 weighs it as SYNTHETIC_DEVELOPMENT (0.6)."""

    def __init__(self, probability):
        self.probability = probability
        self.model_name = "SyntheticFixedProbabilityTestModel"
        self.model_version = f"fixed-{probability!r}"

    def predict(self, feature_vector):
        return self.probability

    def get_feature_schema_version(self):
        return "v1.0.0"


class _AcceptingPayPalVerifier:
    """Stands in for PayPalClient: accepts every webhook and never contacts PayPal."""

    async def verify_webhook_signature(self, headers, raw_body):
        return SimpleNamespace(verified=True)


# --- fixture ---------------------------------------------------------------------------------------

@pytest_asyncio.fixture
async def e2e(db_engine, monkeypatch, caplog):
    Session = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)

    async def session_override():
        async with Session() as session:
            yield session

    async def accepting_paypal_ingestion_service():
        async with Session() as session:
            yield PayPalIngestionService(SQLIngestionRepository(session), _AcceptingPayPalVerifier())

    overrides = {
        get_db_session: session_override,
        get_razorpay_webhook_secret: lambda: WEBHOOK_SECRET,
        get_ingestion_service: get_ingestion_service,
        get_paypal_ingestion_service: accepting_paypal_ingestion_service,
    }
    missing = object()
    previous = {dependency: app.dependency_overrides.get(dependency, missing) for dependency in overrides}
    app.dependency_overrides.update(overrides)
    monkeypatch.setattr(live_pipeline, "AsyncSessionLocal", Session)
    caplog.set_level(logging.INFO, logger="live_pipeline")

    def use_fixed_probability(probability):
        model = SyntheticFixedProbabilityTestModel(probability)
        monkeypatch.setattr(live_pipeline, "get_development_model", lambda mode="deterministic": model)

    try:
        async with AsyncClient(app=app, base_url="http://test") as client:
            yield SimpleNamespace(
                client=client, Session=Session, caplog=caplog, use_fixed_probability=use_fixed_probability,
            )
    finally:
        for dependency, value in previous.items():
            if value is missing:
                app.dependency_overrides.pop(dependency, None)
            else:
                app.dependency_overrides[dependency] = value


# --- helpers ---------------------------------------------------------------------------------------

def _payment_id():
    return f"pay_E2E{uuid.uuid4().hex[:12]}"


def _razorpay_webhook(event, payment_id, created_at, *, amount=AMOUNT, method="upi"):
    entity = {
        "id": payment_id, "entity": "payment", "amount": amount, "currency": "INR",
        "status": event.split(".", 1)[1], "order_id": f"order_{payment_id[4:]}", "method": method,
        "created_at": created_at,
    }
    if method == "upi":
        entity["vpa"] = "buyer@okhdfc"
    body = {
        "entity": "event", "account_id": "acc_E2ETest00001", "event": event, "contains": ["payment"],
        "payload": {"payment": {"entity": entity}}, "created_at": created_at,
    }
    return json.dumps(body, sort_keys=True).encode()


async def _post_razorpay(e2e, body, source_event_id):
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return await e2e.client.post(
        "/ingest/razorpay",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": source_event_id,
        },
    )


def _paypal_body(builder, webhook_id, payment_id, at):
    body = builder()
    body["id"] = webhook_id
    body["create_time"] = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    body["resource"]["id"] = payment_id
    return json.dumps(body).encode()


async def _post_paypal(e2e, body):
    return await e2e.client.post("/webhooks/paypal", content=body, headers={"Content-Type": "application/json"})


def _pipeline_runs(caplog):
    """(status, message) for every pipeline run logged so far, in order."""
    runs = []
    for record in caplog.records:
        if record.name != "live_pipeline":
            continue
        message = record.getMessage()
        if message.startswith("[LivePipelineSuccess]"):
            runs.append(("success", message))
        elif message.startswith("[LivePipelineError]"):
            runs.append(("failed", message))
    return runs


def _assert_runs_succeeded(caplog, source_event_ids):
    runs = _pipeline_runs(caplog)
    assert [status for status, _ in runs] == ["success"] * len(source_event_ids), runs
    for (_, message), source_event_id in zip(runs, source_event_ids):
        assert f"source_event_id={source_event_id} " in message


async def _counts(Session):
    counts = {}
    async with Session() as session:
        for model in TABLES:
            counts[model.__tablename__] = (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
    return counts


async def _rows(Session, model, *conditions):
    async with Session() as session:
        return (await session.execute(select(model).where(*conditions))).scalars().all()


async def _one(Session, model, *conditions):
    rows = await _rows(Session, model, *conditions)
    assert len(rows) == 1, f"expected one {model.__tablename__} row, got {len(rows)}"
    return rows[0]


async def _for_payment(Session, model, payment_id):
    return await _one(Session, model, model.payment_attempt_id == payment_id)


async def _seed_razorpay_history(Session, now, count=50):
    """Captured Razorpay terminal events three hours back: enough global volume for a Stage 5 prediction."""
    at = now - timedelta(hours=3)
    async with Session() as session:
        session.add_all([
            PaymentEventModel(
                event_id=uuid.uuid4(), source_system="razorpay", source_event_id=f"evt_e2e_hist_{i:03d}",
                payment_id=f"pay_E2EHist{i:03d}", timestamp=at, event_type="payment.captured", currency="INR",
                amount_minor_units=AMOUNT, payment_status="captured", payment_method="netbanking", ingested_at=at,
            )
            for i in range(count)
        ])
        await session.commit()


async def _seed_degraded_card_context(Session, now):
    """The live-context generator's scenario: an ACTIVE CRITICAL episode with RCA on PAYMENT_METHOD=card."""
    _, _, windows = compute_tumbling_windows(now)
    async with Session() as session:
        await seed_historical_baselines(session, windows, "card", now)
        await seed_upstream_synthetic_traffic(session, windows, "card", "INR", 42)
        await run_stage2_analytics(session, windows)
        active_episodes = await run_stage3_degradation(session, windows)
        assert active_episodes, "the generator scenario produced no ACTIVE episode"
        await run_stage4_rca(session, active_episodes, "card")


def _domain_event(row):
    return PaymentEvent(**{name: getattr(row, name) for name in PaymentEvent.model_fields})


async def _authorize(e2e, payment_id, now, *, method="upi"):
    source_event_id = f"evt_auth_{payment_id}"
    body = _razorpay_webhook("payment.authorized", payment_id, int(now.timestamp()), method=method)
    response = await _post_razorpay(e2e, body, source_event_id)
    assert response.status_code == 200, response.text
    return source_event_id, body, response


async def _capture(e2e, payment_id, now):
    source_event_id = f"evt_capture_{payment_id}"
    created_at = int(now.timestamp()) + CAPTURE_DELAY_SECONDS
    body = _razorpay_webhook("payment.captured", payment_id, created_at)
    response = await _post_razorpay(e2e, body, source_event_id)
    assert response.status_code == 200, response.text
    return source_event_id, body, response


async def _assert_attributed_simulated_intervention(Session, payment_id, capture_source_event_id):
    """The Stage 5-8 chain for an ACT decision at p = 0.90 followed by a Razorpay capture."""
    prediction = await _for_payment(Session, FailurePredictionModel, payment_id)
    assert (prediction.prediction_status, prediction.failure_probability) == ("PREDICTED", 0.90)
    assert prediction.model_name == "SyntheticFixedProbabilityTestModel"

    decision = await _for_payment(Session, InterventionDecisionModel, payment_id)
    assert (decision.decision_type, decision.gate_verdict, decision.policy_version) == ("ACT", "PASSED", "1.0.0")
    assert decision.selected_route_id in NO_DIAGNOSIS_ROUTES
    assert decision.stage5_prediction_id == prediction.prediction_id

    command = await _for_payment(Session, InterventionCommandModel, payment_id)
    assert (command.decision_id, command.command_status, command.route_key) == (
        decision.decision_id, "SUCCEEDED", decision.selected_route_id,
    )
    attempt = await _one(
        Session, InterventionExecutionAttemptModel, InterventionExecutionAttemptModel.command_id == command.command_id,
    )
    assert (attempt.execution_status, attempt.executor_name, attempt.is_simulation, attempt.provider_response_code) == (
        "SUCCESS", "SIMULATOR_V1", True, "SIMULATED_ACK",
    )

    capture_event = await _one(Session, PaymentEventModel, PaymentEventModel.source_event_id == capture_source_event_id)
    observation = await _one(
        Session, PaymentOutcomeObservationModel, PaymentOutcomeObservationModel.command_id == command.command_id,
    )
    assert (observation.payment_outcome, observation.terminal_event_type, observation.terminal_event_id) == (
        "CAPTURED", "payment.captured", capture_event.event_id,
    )
    assert observation.observation_audit_payload["conflict_detected"] is False
    assert observation.terminal_event_timestamp > attempt.completed_at

    attribution = await _one(
        Session, CounterfactualAttributionModel, CounterfactualAttributionModel.command_id == command.command_id,
    )
    assert (attribution.attribution_status, attribution.treatment_status, attribution.observed_payment_outcome) == (
        "ATTRIBUTED", "TREATED", "CAPTURED",
    )
    # p0 = 0.9000; alpha = c_prediction 0.6 (synthetic) x c_diagnosis 0.6 (no RCA) x c_timing 1.0 (<= 60 s).
    assert attribution.payment_amount_minor_units == AMOUNT
    assert attribution.counterfactual_failure_probability == Decimal("0.9000")
    assert attribution.attribution_confidence == Decimal("0.3600")
    assert attribution.counterfactual_loss_exposure_minor_units == 45000
    assert attribution.counterfactual_natural_success_gmv_minor_units == 5000
    assert attribution.attributed_protected_gmv_minor_units == 16200
    assert (attribution.is_simulated_execution, attribution.is_synthetic_baseline) == (True, True)
    assert 0 <= attribution.attributed_protected_gmv_minor_units <= attribution.counterfactual_loss_exposure_minor_units
    assert attribution.counterfactual_loss_exposure_minor_units <= attribution.payment_amount_minor_units


# --- cold start with the real heuristic ------------------------------------------------------------

@pytest.mark.asyncio
async def test_cold_start_authorization_and_capture_record_no_decision_with_the_real_heuristic(e2e):
    now = datetime.now(timezone.utc)
    payment_id = _payment_id()

    auth_id, _, auth_response = await _authorize(e2e, payment_id, now)
    capture_id, _, _ = await _capture(e2e, payment_id, now)

    _assert_runs_succeeded(e2e.caplog, [auth_id, capture_id])
    auth_event = await _one(e2e.Session, PaymentEventModel, PaymentEventModel.source_event_id == auth_id)
    assert auth_response.json() == {"status": "success", "event_id": str(auth_event.event_id)}
    assert (auth_event.source_system, auth_event.event_type, auth_event.amount_minor_units) == (
        "razorpay", "payment.authorized", AMOUNT,
    )

    capture_event = await _one(e2e.Session, PaymentEventModel, PaymentEventModel.source_event_id == capture_id)
    window_start = capture_event.timestamp.replace(
        minute=capture_event.timestamp.minute // 5 * 5, second=0, microsecond=0,
    )
    global_snapshot = await _one(
        e2e.Session, PaymentHealthSnapshotModel,
        PaymentHealthSnapshotModel.window_start == window_start,
        PaymentHealthSnapshotModel.segment_dimension == "GLOBAL",
    )
    assert (global_snapshot.successful_transaction_count, global_snapshot.insufficient_volume) == (1, True)
    signals = await _rows(e2e.Session, DegradationSignalModel)
    assert signals and {signal.signal_type for signal in signals} <= PAUSE_SIGNALS

    # No Razorpay terminal history: Stage 5 records INSUFFICIENT_DATA and the model is never called.
    prediction = await _for_payment(e2e.Session, FailurePredictionModel, payment_id)
    assert (prediction.model_name, prediction.model_version) == ("DeterministicBaselineModel", "untrained-heuristic-v2")
    assert (prediction.prediction_status, prediction.failure_probability) == ("INSUFFICIENT_DATA", None)

    counts = await _counts(e2e.Session)
    assert (counts["degradation_episodes"], counts["rca_evaluations"]) == (0, 0)
    assert {table: counts[table] for table in STAGE_5_TO_8_TABLES - {"failure_predictions"}} == dict.fromkeys(
        STAGE_5_TO_8_TABLES - {"failure_predictions"}, 0,
    )


@pytest.mark.asyncio
async def test_real_heuristic_with_razorpay_history_predicts_below_the_runner_threshold(e2e):
    now = datetime.now(timezone.utc)
    payment_id = _payment_id()
    await _seed_razorpay_history(e2e.Session, now)

    auth_id, _, _ = await _authorize(e2e, payment_id, now)

    _assert_runs_succeeded(e2e.caplog, [auth_id])
    prediction = await _for_payment(e2e.Session, FailurePredictionModel, payment_id)
    assert prediction.prediction_status == "PREDICTED"
    assert prediction.model_name == "DeterministicBaselineModel"
    # 0.5 x 0.05 base + 0.5 x 0.0 (24-hour Razorpay failure rate; the 30-minute window holds no history).
    snapshot = prediction.feature_snapshot
    assert (snapshot["global_30m_failure_rate"], snapshot["global_24h_failure_rate"]) == (None, 0.0)
    assert prediction.failure_probability == pytest.approx(0.025)
    assert await _rows(e2e.Session, InterventionDecisionModel) == []


# --- full chain: authorization -> simulated execution -> attribution -------------------------------

@pytest.mark.asyncio
async def test_signed_authorization_then_capture_attributes_a_simulated_intervention(e2e):
    e2e.use_fixed_probability(0.90)
    now = datetime.now(timezone.utc)
    payment_id = _payment_id()
    await _seed_razorpay_history(e2e.Session, now)

    auth_id, _, _ = await _authorize(e2e, payment_id, now)
    after_authorization = await _counts(e2e.Session)
    assert (after_authorization["intervention_commands"], after_authorization["payment_outcome_observations"]) == (1, 0)
    assert after_authorization["counterfactual_attributions"] == 0

    capture_id, _, _ = await _capture(e2e, payment_id, now)

    _assert_runs_succeeded(e2e.caplog, [auth_id, capture_id])
    assert "act=ACT" in _pipeline_runs(e2e.caplog)[0][1]
    await _assert_attributed_simulated_intervention(e2e.Session, payment_id, capture_id)
    counts = await _counts(e2e.Session)
    assert (counts["raw_ingestion_records"], counts["degradation_episodes"], counts["rca_evaluations"]) == (2, 0, 0)


# --- duplicates and replays ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_and_conflicting_webhooks_do_not_rerun_the_pipeline(e2e):
    e2e.use_fixed_probability(0.90)
    now = datetime.now(timezone.utc)
    payment_id = _payment_id()
    await _seed_razorpay_history(e2e.Session, now)
    auth_id, auth_body, _ = await _authorize(e2e, payment_id, now)
    capture_id, capture_body, _ = await _capture(e2e, payment_id, now)
    before = await _counts(e2e.Session)
    runs_before = len(_pipeline_runs(e2e.caplog))

    duplicate_auth = await _post_razorpay(e2e, auth_body, auth_id)
    duplicate_capture = await _post_razorpay(e2e, capture_body, capture_id)
    conflicting_auth = await _post_razorpay(
        e2e, _razorpay_webhook("payment.authorized", payment_id, int(now.timestamp()), amount=AMOUNT + 1), auth_id,
    )

    assert (duplicate_auth.status_code, duplicate_capture.status_code, conflicting_auth.status_code) == (200, 200, 409)
    assert len(_pipeline_runs(e2e.caplog)) == runs_before
    assert await _counts(e2e.Session) == before
    # Characterization: a duplicate is re-normalized with a fresh uuid4, so its response does not
    # carry the stored event_id.
    stored_auth = await _one(e2e.Session, PaymentEventModel, PaymentEventModel.source_event_id == auth_id)
    assert duplicate_auth.json()["status"] == "success"
    assert duplicate_auth.json()["event_id"] != str(stored_auth.event_id)
    await _assert_attributed_simulated_intervention(e2e.Session, payment_id, capture_id)


@pytest.mark.asyncio
async def test_replaying_the_pipeline_for_stored_events(e2e):
    e2e.use_fixed_probability(0.90)
    now = datetime.now(timezone.utc)
    payment_id = _payment_id()
    await _seed_razorpay_history(e2e.Session, now)
    auth_id, _, _ = await _authorize(e2e, payment_id, now)
    capture_id, _, _ = await _capture(e2e, payment_id, now)
    stored_auth = await _one(e2e.Session, PaymentEventModel, PaymentEventModel.source_event_id == auth_id)
    stored_capture = await _one(e2e.Session, PaymentEventModel, PaymentEventModel.source_event_id == capture_id)
    prediction = await _for_payment(e2e.Session, FailurePredictionModel, payment_id)
    decision = await _for_payment(e2e.Session, InterventionDecisionModel, payment_id)
    command = await _for_payment(e2e.Session, InterventionCommandModel, payment_id)
    before = await _counts(e2e.Session)

    async with e2e.Session() as session:
        auth_replay = await live_pipeline.run_live_pipeline(_domain_event(stored_auth), session=session)

    assert auth_replay["status"] == "success", auth_replay
    assert auth_replay["prediction"].prediction_id == prediction.prediction_id
    assert auth_replay["decision"].decision_id == decision.decision_id
    assert auth_replay["command"].command_id == command.command_id
    after_auth_replay = await _counts(e2e.Session)
    # Stage 3 appends a new signal version for every re-evaluated window; nothing else may change.
    assert after_auth_replay["degradation_signals"] > before["degradation_signals"]
    assert {table: n for table, n in after_auth_replay.items() if table != "degradation_signals"} == {
        table: n for table, n in before.items() if table != "degradation_signals"
    }

    async with e2e.Session() as session:
        capture_replay = await live_pipeline.run_live_pipeline(_domain_event(stored_capture), session=session)

    # Characterization: the replay re-observes at the same as_of, which the (command_id, as_of_timestamp)
    # unique constraint rejects; the runner rolls Stages 5-8 back and reports the failure.
    assert (capture_replay["status"], capture_replay["failed_stage"]) == ("failed", "STAGE_7_OBSERVATION_TERMINAL")
    after_capture_replay = await _counts(e2e.Session)
    assert {table: after_capture_replay[table] for table in STAGE_5_TO_8_TABLES} == {
        table: before[table] for table in STAGE_5_TO_8_TABLES
    }
    await _assert_attributed_simulated_intervention(e2e.Session, payment_id, capture_id)


# --- provider isolation ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overlapping_paypal_events_do_not_affect_razorpay_records(e2e):
    e2e.use_fixed_probability(0.90)
    now = datetime.now(timezone.utc)
    payment_id = _payment_id()
    capture_time = datetime.fromtimestamp(int(now.timestamp()) + CAPTURE_DELAY_SECONDS, tz=timezone.utc)
    await _seed_razorpay_history(e2e.Session, now)

    declined = await _post_paypal(e2e, _paypal_body(samples.capture_declined, "WH-E2E-DECLINED-0001", payment_id, now))
    auth_id, _, _ = await _authorize(e2e, payment_id, now)
    completed = await _post_paypal(
        e2e, _paypal_body(samples.capture_completed, "WH-E2E-COMPLETED-0001", payment_id, capture_time),
    )
    capture_id, _, _ = await _capture(e2e, payment_id, now)

    assert (declined.status_code, declined.json()["status"]) == (200, "stored")
    assert (completed.status_code, completed.json()["status"]) == (200, "stored")
    paypal_events = await _rows(e2e.Session, PaymentEventModel, PaymentEventModel.source_system == "paypal")
    assert sorted((event.payment_id, event.event_type) for event in paypal_events) == [
        (payment_id, "payment.captured"), (payment_id, "payment.failed"),
    ]
    # Only the two Razorpay webhooks ran the pipeline.
    _assert_runs_succeeded(e2e.caplog, [auth_id, capture_id])
    # The earlier PayPal decline did not make Stage 6 treat the attempt as terminal, Stage 7 did not mark
    # the command NOT_NEEDED, and the observation and attribution come from the Razorpay capture only.
    await _assert_attributed_simulated_intervention(e2e.Session, payment_id, capture_id)

    window_start = capture_time.replace(minute=capture_time.minute // 5 * 5, second=0)
    global_snapshot = await _one(
        e2e.Session, PaymentHealthSnapshotModel,
        PaymentHealthSnapshotModel.window_start == window_start,
        PaymentHealthSnapshotModel.segment_dimension == "GLOBAL",
    )
    assert (global_snapshot.transaction_count, global_snapshot.successful_transaction_count) == (1, 1)


# --- runner threshold ------------------------------------------------------------------------------

@pytest.mark.parametrize("probability, expected_decision", [
    pytest.param(0.30, None, id="below-monitor-threshold"),
    # Characterization: the policy's MONITOR band starts at 0.35, but the runner only calls Stage 6 above 0.5.
    pytest.param(0.40, None, id="policy-monitor-band-below-runner-threshold"),
    pytest.param(0.50, None, id="at-runner-threshold"),
    pytest.param(math.nextafter(0.5, 1.0), "MONITOR", id="just-above-runner-threshold"),
    pytest.param(0.55, "MONITOR", id="monitor"),
    pytest.param(0.70, "ACT", id="act-threshold"),
])
@pytest.mark.asyncio
async def test_stage6_invocation_around_the_runner_threshold(e2e, probability, expected_decision):
    e2e.use_fixed_probability(probability)
    now = datetime.now(timezone.utc)
    payment_id = _payment_id()
    await _seed_razorpay_history(e2e.Session, now)

    auth_id, _, _ = await _authorize(e2e, payment_id, now)

    _assert_runs_succeeded(e2e.caplog, [auth_id])
    prediction = await _for_payment(e2e.Session, FailurePredictionModel, payment_id)
    assert (prediction.prediction_status, prediction.failure_probability) == ("PREDICTED", probability)
    decisions = await _rows(e2e.Session, InterventionDecisionModel)
    commands = await _rows(e2e.Session, InterventionCommandModel)
    counts = await _counts(e2e.Session)
    assert (counts["payment_outcome_observations"], counts["counterfactual_attributions"]) == (0, 0)

    if expected_decision is None:
        assert (decisions, commands) == ([], [])
    elif expected_decision == "MONITOR":
        assert [(d.decision_type, d.selected_route_id) for d in decisions] == [("MONITOR", None)]
        assert commands == []
    else:
        assert [d.decision_type for d in decisions] == ["ACT"]
        assert decisions[0].selected_route_id in NO_DIAGNOSIS_ROUTES
        assert [c.command_status for c in commands] == ["SUCCEEDED"]
        assert counts["intervention_execution_attempts"] == 1


# --- real degraded context -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_degraded_context_with_the_real_heuristic_records_a_monitor_decision(e2e):
    now = datetime.now(timezone.utc)
    payment_id = _payment_id()
    await _seed_degraded_card_context(e2e.Session, now)
    await _seed_razorpay_history(e2e.Session, now)

    auth_id, _, _ = await _authorize(e2e, payment_id, now, method="card")

    _assert_runs_succeeded(e2e.caplog, [auth_id])
    episodes = await _rows(e2e.Session, DegradationEpisodeModel, DegradationEpisodeModel.status == "ACTIVE")
    assert ("GLOBAL", "ALL", "CRITICAL") in {(e.segment_dimension, e.segment_value, e.severity) for e in episodes}
    assert await _rows(e2e.Session, RCAEvaluationModel)

    prediction = await _for_payment(e2e.Session, FailurePredictionModel, payment_id)
    snapshot = prediction.feature_snapshot
    assert (prediction.prediction_status, prediction.model_name) == ("PREDICTED", "DeterministicBaselineModel")
    assert (snapshot["is_in_active_degradation"], snapshot["degradation_severity"]) == (True, "CRITICAL")
    assert (snapshot["rca_evidence_strength"], snapshot["rca_candidate_matches_payment_segment"]) == ("STRONG", True)
    assert (snapshot["global_30m_failure_rate"], snapshot["global_24h_failure_rate"]) == (None, 0.0)
    # 0.5 x 0.05 + 0.5 x 0.0 + 0.30 (CRITICAL) + 0.20 (STRONG RCA on the payment's segment).
    assert prediction.failure_probability == pytest.approx(0.525)
    assert prediction.failure_probability == DeterministicBaselineModel().predict(snapshot)

    decision = await _for_payment(e2e.Session, InterventionDecisionModel, payment_id)
    assert (decision.decision_type, decision.selected_route_id, decision.gate_verdict) == ("MONITOR", None, "PASSED")
    assert await _rows(e2e.Session, InterventionCommandModel) == []


@pytest.mark.asyncio
async def test_degraded_context_without_razorpay_history_records_insufficient_data(e2e):
    now = datetime.now(timezone.utc)
    payment_id = _payment_id()
    await _seed_degraded_card_context(e2e.Session, now)

    auth_id, _, _ = await _authorize(e2e, payment_id, now, method="card")

    _assert_runs_succeeded(e2e.caplog, [auth_id])
    # The generator's traffic is source_system "synthetic", which the Razorpay global rates exclude.
    prediction = await _for_payment(e2e.Session, FailurePredictionModel, payment_id)
    assert (prediction.prediction_status, prediction.failure_probability) == ("INSUFFICIENT_DATA", None)
    assert prediction.feature_snapshot["is_in_active_degradation"] is True
    assert await _rows(e2e.Session, InterventionDecisionModel) == []
