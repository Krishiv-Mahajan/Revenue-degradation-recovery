"""
End-to-end tests of PayPal Stages 2-4 in the live pipeline, with the real heuristic model and the default
policy (no fixed probabilities, no test policies).

PayPal capture webhooks go through the real POST /webhooks/paypal route; each one runs the live pipeline,
which recomputes that window's PayPal-scope health snapshots (Stage 2), degradation signals and episodes
(Stage 3) and RCA (Stage 4). An approved PayPal order then runs Stages 5-8 against that PayPal-only context.

Historical baselines are PayPal capture events from one to three weeks earlier, written directly (as history
would already be stored) and turned into PayPal-scope snapshots by the real Stage 2 service.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from scripts.generate_live_context import compute_tumbling_windows
from src.core.analytics.service import PaymentHealthAnalyticsService
from src.core.domain.payment_attempts import paypal_attempt_id
from src.infrastructure.analytics_repository import AnalyticsRepository
from src.infrastructure.models import (
    CandidateCauseModel,
    DegradationEpisodeModel,
    DegradationSignalModel,
    EpisodeStateHistoryModel,
    FailurePredictionModel,
    InterventionCommandModel,
    InterventionDecisionModel,
    InterventionExecutionAttemptModel,
    PaymentEventModel,
    PaymentHealthSnapshotModel,
    RCAEvaluationModel,
)
from tests.integration.test_live_pipeline_e2e import (  # noqa: F401  (e2e is a fixture)
    AMOUNT,
    _approve_paypal_order,
    _assert_runs_succeeded,
    _authorize,
    _for_payment,
    _paypal_order_id,
    _payment_id,
    _post_paypal,
    _rows,
    _seed_razorpay_history,
    e2e,
)
from tests.unit import paypal_event_samples as samples

WINDOW_VOLUME = 50  # Stage 2's minimum volume for a window to be evaluated
OUTAGE_FAILURES_PER_WINDOW = 40  # 80% of captures declined
SCOPED_TABLES = (PaymentHealthSnapshotModel, DegradationSignalModel, DegradationEpisodeModel, EpisodeStateHistoryModel)


def _outage_windows(now):
    _, _, windows = compute_tumbling_windows(now)
    return windows


async def _seed_paypal_baselines(Session, windows, currencies=("USD",), weeks=(1, 2, 3)):
    """Healthy PayPal captures (47 to 49 of 50 completed per currency) in the same windows of earlier weeks."""
    async with Session() as session:
        rows = []
        for week in weeks:
            for w_index, (w_start, _) in enumerate(windows):
                start = w_start - timedelta(weeks=week)
                failures = 1 + (week + w_index) % 3
                for currency in currencies:
                    for i in range(WINDOW_VOLUME):
                        failed = i < failures
                        rows.append(PaymentEventModel(
                            event_id=uuid.uuid4(), source_system="paypal",
                            source_event_id=f"WH-E2E-BASE-{currency}-{week}-{w_index}-{i:02d}",
                            payment_id=f"E2EBASECAP{currency}{week}{w_index}{i:02d}",
                            order_id=f"E2EBASEORD{currency}{week}{w_index}{i:02d}",
                            timestamp=start + timedelta(seconds=5 * i + 1),
                            event_type="payment.failed" if failed else "payment.captured", currency=currency,
                            amount_minor_units=AMOUNT, payment_status="declined" if failed else "completed",
                            error_reason="DECLINED_BY_RISK_FRAUD_FILTERS" if failed else None,
                            ingested_at=start + timedelta(seconds=5 * i + 2),
                        ))
        session.add_all(rows)
        await session.commit()

        analytics = PaymentHealthAnalyticsService(AnalyticsRepository(session))
        for week in sorted(weeks, reverse=True):
            for w_start, w_end in windows:
                await analytics.calculate_and_save_window(
                    w_start - timedelta(weeks=week), w_end - timedelta(weeks=week), "paypal"
                )
        await session.commit()


def _capture_webhook(failed, webhook_id, at, currency):
    body = samples.capture_declined() if failed else samples.capture_completed()
    body["id"] = webhook_id
    body["create_time"] = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    body["resource"]["id"] = f"E2ECAP{uuid.uuid4().hex[:11].upper()}"
    body["resource"]["amount"]["currency_code"] = currency
    body["resource"]["supplementary_data"]["related_ids"]["order_id"] = _paypal_order_id()
    return json.dumps(body).encode()


async def _deliver_outage(e2e, windows, failures_per_window):
    """
    Real PayPal capture webhooks across the outage windows. failures_per_window maps currency to declined
    captures per window (of WINDOW_VOLUME). Returns the webhook ids in delivery order.
    """
    webhook_ids = []
    for w_index, (w_start, _) in enumerate(windows):
        for currency, failures in failures_per_window.items():
            for i in range(WINDOW_VOLUME):
                webhook_id = f"WH-E2E-OUTAGE-{currency}-{w_index}-{i:02d}"
                at = w_start + timedelta(seconds=5 * i + 1)
                response = await _post_paypal(e2e, _capture_webhook(i < failures, webhook_id, at, currency))
                assert (response.status_code, response.json()["status"]) == (200, "stored"), response.text
                webhook_ids.append(webhook_id)
    return webhook_ids


async def _latest_rca(Session, episode_id):
    async with Session() as session:
        return (await session.execute(
            select(RCAEvaluationModel).where(RCAEvaluationModel.episode_id == episode_id)
            .order_by(RCAEvaluationModel.evaluation_version.desc()).limit(1)
        )).scalar_one()


async def _scope_counts(Session, scope):
    counts = {}
    for model in SCOPED_TABLES:
        counts[model.__tablename__] = len(await _rows(Session, model, model.provider_scope == scope))
    return counts


# --- a PayPal-wide outage -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paypal_outage_flows_from_webhooks_to_an_act_decision_with_the_real_heuristic(e2e, monkeypatch):
    now = datetime.now(timezone.utc)
    windows = _outage_windows(now)
    await _seed_paypal_baselines(e2e.Session, windows)

    outage_ids = await _deliver_outage(e2e, windows, {"USD": OUTAGE_FAILURES_PER_WINDOW})
    order_id = _paypal_order_id()
    attempt_id = paypal_attempt_id(order_id)
    approval_id = await _approve_paypal_order(e2e, order_id, now)
    _assert_runs_succeeded(e2e.caplog, outage_ids + [approval_id])

    # Stage 2: PayPal-scope snapshots, baselined on PayPal history only.
    for w_start, _ in windows:
        snapshot = await _one_row(
            e2e.Session, PaymentHealthSnapshotModel,
            PaymentHealthSnapshotModel.window_start == w_start, PaymentHealthSnapshotModel.segment_dimension == "GLOBAL",
            PaymentHealthSnapshotModel.provider_scope == "paypal",
        )
        assert (snapshot.transaction_count, snapshot.failed_transaction_count) == (WINDOW_VOLUME, OUTAGE_FAILURES_PER_WINDOW)
        assert snapshot.insufficient_volume is False
        assert snapshot.baseline_sample_count == 3
        assert snapshot.baseline_success_rate == pytest.approx(0.96)

    # Stage 3: threshold and Z-score evidence, and one CRITICAL PayPal episode per affected segment.
    signals = await _rows(
        e2e.Session, DegradationSignalModel,
        DegradationSignalModel.provider_scope == "paypal", DegradationSignalModel.segment_dimension == "GLOBAL",
        DegradationSignalModel.window_start.in_([w for w, _ in windows]),
    )
    latest = {}
    for signal in signals:
        if signal.window_start not in latest or signal.evaluation_version > latest[signal.window_start].evaluation_version:
            latest[signal.window_start] = signal
    assert [latest[w].signal_type for w, _ in windows] == ["BAD"] * 3
    assert {latest[w].z_score_status for w, _ in windows} <= {"OK", "LOW_VARIANCE"}
    assert all(latest[w].z_score <= -3 for w, _ in windows)

    episodes = await _rows(e2e.Session, DegradationEpisodeModel, DegradationEpisodeModel.status == "ACTIVE")
    assert {(e.provider_scope, e.segment_dimension, e.segment_value, e.severity) for e in episodes} == {
        ("paypal", "GLOBAL", "ALL", "CRITICAL"), ("paypal", "currency", "USD", "CRITICAL"),
    }
    global_episode = next(e for e in episodes if e.segment_dimension == "GLOBAL")
    assert global_episode.started_at_window == windows[0][0]

    # Stage 4: the only currency covers the whole population, so it is not reported as a cause.
    rca = await _latest_rca(e2e.Session, global_episode.episode_id)
    assert rca.classification == "SYSTEMIC"
    assert await _rows(e2e.Session, CandidateCauseModel, CandidateCauseModel.evaluation_id == rca.evaluation_id) == []
    payload = rca.evidence_audit_payload
    assert payload["provider_scope"] == "paypal"
    [usd] = payload["structural_candidates_evaluated"]
    assert (usd["dimension"], usd["value"], usd["qualified_as_candidate"]) == ("CURRENCY", "USD", False)
    assert "entire episode population" in usd["disqualification_reason"]
    assert {e["dimension"] for e in payload["supporting_error_evidence"]} <= {"ERROR_REASON"}

    # Stage 5: the real heuristic, from PayPal-only evidence.
    prediction = await _for_payment(e2e.Session, FailurePredictionModel, attempt_id)
    snapshot = prediction.feature_snapshot
    assert (prediction.prediction_status, prediction.model_name) == ("PREDICTED", "DeterministicBaselineModel")
    assert (snapshot["is_in_active_degradation"], snapshot["degradation_severity"]) == (True, "CRITICAL")
    assert snapshot["global_30m_failure_rate"] == pytest.approx(0.8)
    assert snapshot["rca_candidate_matches_payment_segment"] is not True
    # 0.5 x 0.05 + 0.5 x 0.8 (PayPal 30-minute failure rate) + 0.30 (CRITICAL PayPal episode).
    assert prediction.failure_probability == pytest.approx(0.725)

    # Stage 6: ACT under the default policy and gates, tied to the PayPal episode.
    decision = await _for_payment(e2e.Session, InterventionDecisionModel, attempt_id)
    assert (decision.decision_type, decision.gate_verdict, decision.policy_version) == ("ACT", "PASSED", "1.2.0")
    assert decision.stage5_prediction_id == prediction.prediction_id
    # The SYSTEMIC diagnosis makes the diagnosis-backed gateway retry eligible, and it has the highest utility.
    assert decision.selected_route_id == "RETRY_SECONDARY_GATEWAY"
    # Both PayPal episodes are CRITICAL and started together; the documented tie-break (largest episode_id
    # string) picks one of them, never a Razorpay episode.
    context = decision.evaluation_audit_payload["upstream_context"]
    assert context["episode_id"] == max(str(e.episode_id) for e in episodes)
    [command] = await _rows(e2e.Session, InterventionCommandModel)
    assert (command.payment_attempt_id, command.route_key) == (attempt_id, decision.selected_route_id)
    # PayPal has no gateway-retry action, so the command is executed by the simulator and labelled as such.
    [execution] = await _rows(
        e2e.Session, InterventionExecutionAttemptModel, InterventionExecutionAttemptModel.command_id == command.command_id
    )
    assert (execution.executor_name, execution.is_simulation) == ("SIMULATOR_V1", True)

    # Nothing PayPal sent reached the Razorpay/synthetic scope.
    assert await _scope_counts(e2e.Session, "razorpay_pipeline") == dict.fromkeys(
        (m.__tablename__ for m in SCOPED_TABLES), 0
    )

    # The dashboard labels each episode with its analytics scope.
    monkeypatch.setenv("DASHBOARD_USERNAME", "e2e-dashboard-user")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "e2e-dashboard-password")
    auth = ("e2e-dashboard-user", "e2e-dashboard-password")
    listed = (await e2e.client.get("/api/v1/episodes", auth=auth)).json()
    assert {(e["provider_scope"], e["segment_dimension"]) for e in listed} == {("paypal", "GLOBAL"), ("paypal", "currency")}
    rca_view = (await e2e.client.get(f"/api/v1/rca/{context['episode_id']}", auth=auth)).json()
    assert rca_view["episode"]["provider_scope"] == "paypal"
    assert rca_view["intervention_context"]["act_decisions"] == 1


async def _one_row(Session, model, *conditions):
    rows = await _rows(Session, model, *conditions)
    assert len(rows) == 1, f"expected one {model.__tablename__} row, got {len(rows)}"
    return rows[0]


# --- no cross-provider contamination --------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_paypal_outage_never_degrades_razorpay_payments(e2e):
    now = datetime.now(timezone.utc)
    windows = _outage_windows(now)
    await _seed_paypal_baselines(e2e.Session, windows)
    await _seed_razorpay_history(e2e.Session, now)
    outage_ids = await _deliver_outage(e2e, windows, {"USD": OUTAGE_FAILURES_PER_WINDOW})

    payment_id = _payment_id()
    auth_id, _, _ = await _authorize(e2e, payment_id, now)

    _assert_runs_succeeded(e2e.caplog, outage_ids + [auth_id])
    paypal_episodes = await _rows(e2e.Session, DegradationEpisodeModel, DegradationEpisodeModel.provider_scope == "paypal")
    assert paypal_episodes and {e.status for e in paypal_episodes} == {"ACTIVE"}
    assert await _rows(
        e2e.Session, DegradationEpisodeModel, DegradationEpisodeModel.provider_scope == "razorpay_pipeline"
    ) == []

    # The Razorpay payment sees neither the PayPal episode nor PayPal's failure rate.
    prediction = await _for_payment(e2e.Session, FailurePredictionModel, payment_id)
    snapshot = prediction.feature_snapshot
    assert (snapshot["is_in_active_degradation"], snapshot["degradation_severity"]) == (False, None)
    assert (snapshot["global_30m_failure_rate"], snapshot["global_24h_failure_rate"]) == (None, 0.0)
    assert prediction.failure_probability == pytest.approx(0.025)
    decision = await _for_payment(e2e.Session, InterventionDecisionModel, payment_id)
    assert (decision.decision_type, decision.selected_route_id) == ("NO_ACTION", None)
    assert decision.evaluation_audit_payload["upstream_context"]["episode_id"] is None

    # The Razorpay window holds only the Razorpay authorization's scope; the PayPal captures are not in it.
    rzp_snapshots = await _rows(
        e2e.Session, PaymentHealthSnapshotModel, PaymentHealthSnapshotModel.provider_scope == "razorpay_pipeline",
        PaymentHealthSnapshotModel.segment_dimension == "GLOBAL",
    )
    assert rzp_snapshots and all(s.transaction_count <= 1 for s in rzp_snapshots)


# --- currency segmentation ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_usd_only_outage_is_diagnosed_as_usd_and_only_usd_attempts_reach_act(e2e):
    now = datetime.now(timezone.utc)
    windows = _outage_windows(now)
    await _seed_paypal_baselines(e2e.Session, windows, currencies=("USD", "EUR"))

    # USD captures are 90% declined; EUR captures stay healthy (1 of 50 declined).
    outage_ids = await _deliver_outage(e2e, windows, {"USD": 45, "EUR": 1})
    usd_order, eur_order = _paypal_order_id(), _paypal_order_id()
    usd_approval = await _approve_paypal_order(e2e, usd_order, now)
    eur_body = samples.order_approved(currency="EUR")
    eur_body["id"] = f"WH-E2E-APPROVED-{eur_order}"
    eur_body["create_time"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    eur_body["resource"]["id"] = eur_order
    response = await _post_paypal(e2e, json.dumps(eur_body).encode())
    assert (response.status_code, response.json()["status"]) == (200, "stored"), response.text
    _assert_runs_succeeded(e2e.caplog, outage_ids + [usd_approval, eur_body["id"]])

    episodes = await _rows(e2e.Session, DegradationEpisodeModel, DegradationEpisodeModel.status == "ACTIVE")
    assert {(e.provider_scope, e.segment_dimension, e.segment_value) for e in episodes} == {
        ("paypal", "GLOBAL", "ALL"), ("paypal", "currency", "USD"),
    }
    global_episode = next(e for e in episodes if e.segment_dimension == "GLOBAL")
    rca = await _latest_rca(e2e.Session, global_episode.episode_id)
    assert rca.classification == "SEGMENT_SPECIFIC"
    candidates = await _rows(e2e.Session, CandidateCauseModel, CandidateCauseModel.evaluation_id == rca.evaluation_id)
    assert [(c.rank, c.candidate_dimension, c.candidate_value, c.evidence_strength) for c in candidates] == [
        (1, "CURRENCY", "USD", "STRONG"),
    ]

    usd_prediction = await _for_payment(e2e.Session, FailurePredictionModel, paypal_attempt_id(usd_order))
    eur_prediction = await _for_payment(e2e.Session, FailurePredictionModel, paypal_attempt_id(eur_order))
    # Both see the CRITICAL PayPal GLOBAL episode and the same PayPal 30-minute failure rate (138 of 300 declined);
    # only the USD attempt matches the diagnosed segment (+0.20 for STRONG evidence).
    for prediction in (usd_prediction, eur_prediction):
        assert prediction.feature_snapshot["global_30m_failure_rate"] == pytest.approx(138 / 300)
        assert prediction.feature_snapshot["degradation_severity"] == "CRITICAL"
    assert usd_prediction.feature_snapshot["rca_candidate_matches_payment_segment"] is True
    assert eur_prediction.feature_snapshot["rca_candidate_matches_payment_segment"] is False
    assert usd_prediction.failure_probability == pytest.approx(0.025 + 0.5 * 138 / 300 + 0.30 + 0.20)
    assert eur_prediction.failure_probability == pytest.approx(0.025 + 0.5 * 138 / 300 + 0.30)

    usd_decision = await _for_payment(e2e.Session, InterventionDecisionModel, paypal_attempt_id(usd_order))
    eur_decision = await _for_payment(e2e.Session, InterventionDecisionModel, paypal_attempt_id(eur_order))
    # ACT needs p >= 0.70: only the attempt in the diagnosed currency gets there; EUR (p = 0.555) is MONITOR.
    assert (usd_decision.decision_type, usd_decision.gate_verdict) == ("ACT", "PASSED")
    assert (eur_decision.decision_type, eur_decision.selected_route_id) == ("MONITOR", None)
