import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker
from src.main import app
from src.api.dependencies import get_db_session

from src.infrastructure.models import (
    CounterfactualAttributionModel,
    DegradationEpisodeModel,
    FailurePredictionModel,
    InterventionCommandModel,
    InterventionDecisionModel,
    InterventionExecutionAttemptModel,
    PaymentEventModel,
    PaymentOutcomeObservationModel,
    RCAEvaluationModel,
)

DASHBOARD_TEST_CREDENTIALS = ("inspection-test-user", "inspection-test-password")


@pytest_asyncio.fixture
async def client(db_engine, monkeypatch):
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    async def override_get_db_session():
        async with Session() as session:
            yield session

    monkeypatch.setenv("DASHBOARD_USERNAME", DASHBOARD_TEST_CREDENTIALS[0])
    monkeypatch.setenv("DASHBOARD_PASSWORD", DASHBOARD_TEST_CREDENTIALS[1])
    app.dependency_overrides[get_db_session] = override_get_db_session
    async with AsyncClient(app=app, base_url="http://test", auth=DASHBOARD_TEST_CREDENTIALS) as ac:
        yield ac
    app.dependency_overrides.pop(get_db_session, None)
@pytest.mark.asyncio
async def test_health_check_endpoint(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

@pytest.mark.asyncio
async def test_dashboard_root_endpoint(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "Payment Recovery Engine" in response.text

@pytest.mark.asyncio
async def test_health_check_db_failure_returns_generic_detail_and_logs_type_only(caplog):
    database_details = ("db-internal.example", "10.0.0.5", "5433", "svc_payments", "payment_recovery_prod")
    driver_error = OperationalError(
        "SELECT 1",
        None,
        Exception(
            'connection to server at "db-internal.example" (10.0.0.5), port 5433 failed: '
            'FATAL: password authentication failed for user "svc_payments" database "payment_recovery_prod"'
        ),
    )
    assert all(detail in str(driver_error) for detail in database_details)

    async def failing_get_db_session():
        class FailingSession:
            async def execute(self, stmt):
                raise driver_error
        yield FailingSession()

    caplog.set_level(logging.DEBUG)
    app.dependency_overrides[get_db_session] = failing_get_db_session
    try:
        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.get("/health")
    finally:
        app.dependency_overrides.pop(get_db_session, None)

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unreachable"}
    for detail in database_details:
        assert detail not in response.text
        assert detail not in caplog.text
    assert any(
        r.name == "src.main" and r.getMessage() == "Health check database probe failed (OperationalError)"
        for r in caplog.records
    )

@pytest.mark.asyncio
async def test_executive_summary_endpoint(client):
    response = await client.get("/api/v1/summary")
    assert response.status_code == 200
    data = response.json()
    assert "total_gmv_minor_units" in data
    assert "total_protected_gmv_minor_units" in data
    assert "active_degradation_episodes" in data
    assert "total_successful_interventions" in data
    assert "overall_success_rate" in data
    assert "total_auths" in data
    assert "total_captures" in data

@pytest.mark.asyncio
async def test_episodes_endpoint(client):
    response = await client.get("/api/v1/episodes?limit=10")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

@pytest.mark.asyncio
async def test_episodes_endpoint_invalid_limit(client):
    response = await client.get("/api/v1/episodes?limit=0")
    assert response.status_code == 422  # Validation error (ge=1)

    response = client_resp = await client.get("/api/v1/episodes?limit=2000")
    assert client_resp.status_code == 422  # Validation error (le=1000)

@pytest.mark.asyncio
async def test_attributions_endpoint(client):
    response = await client.get("/api/v1/attributions?limit=25")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

@pytest.mark.asyncio
async def test_attributions_endpoint_invalid_limit(client):
    response = await client.get("/api/v1/attributions?limit=0")
    assert response.status_code == 422

@pytest.mark.asyncio
async def test_rca_not_found(client):
    response = await client.get("/api/v1/rca/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["detail"] == "RCA evaluation not found"

@pytest.mark.asyncio
async def test_timeline_not_found(client):
    response = await client.get("/api/v1/timeline/non_existent_payment_id")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]

@pytest.mark.asyncio
async def test_rca_invalid_uuid(client):
    response = await client.get("/api/v1/rca/not-a-valid-uuid")
    assert response.status_code == 404
    assert response.json()["detail"] == "RCA evaluation not found"

@pytest.mark.asyncio
async def test_rca_success_with_seed_data(client, db_engine):
    import uuid
    from datetime import datetime, timezone
    from src.infrastructure.models import DegradationEpisodeModel, RCAEvaluationModel, CandidateCauseModel

    ep_id = uuid.uuid4()
    eval_id = uuid.uuid4()
    cand_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # Insert episode and RCA evaluation via session
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        episode = DegradationEpisodeModel(
            episode_id=ep_id,
            segment_dimension="BANK",
            segment_value="HDFC",
            started_at_window=now,
            status="ACTIVE",
            peak_absolute_drop=0.68,
            affected_window_count=3,
            severity="CRITICAL",
        )
        evaluation = RCAEvaluationModel(
            evaluation_id=eval_id,
            episode_id=ep_id,
            evaluation_version=1,
            classification="SEGMENT_SPECIFIC",
            analysis_window_start=now,
            analysis_window_end=now,
            input_fingerprint="sha256:" + "0" * 64,
            generated_at=now,
            evidence_audit_payload={},
        )
        cause = CandidateCauseModel(
            candidate_id=cand_id,
            evaluation_id=eval_id,
            candidate_dimension="BANK",
            candidate_value="HDFC",
            evidence_strength="STRONG",
            excess_failure_contribution=0.85,
            actual_segment_failures=10,
            expected_segment_failures=2.0,
            excess_segment_failures=8.0,
            rank=1,
        )
        session.add_all([episode, evaluation])
        await session.flush()
        session.add(cause)
        await session.commit()
    response = await client.get(f"/api/v1/rca/{ep_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["evaluation_id"] == str(eval_id)
    assert data["classification"] == "SEGMENT_SPECIFIC"
    assert data["episode"]["severity"] == "CRITICAL"
    assert data["episode"]["segment_dimension"] == "BANK"
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["dimension"] == "BANK"
    assert data["candidates"][0]["evidence_strength"] == "STRONG"
    assert "prediction_context" in data
    assert "intervention_context" in data

@pytest.mark.asyncio
async def test_rca_episode_scoping_no_cross_episode_leakage(client, db_engine):
    """
    Regression test proving two distinct episodes cannot leak downstream
    telemetry (predictions, decisions, protected GMV) into each other.
    """
    import uuid
    from datetime import datetime, timezone
    from src.infrastructure.models import (
        DegradationEpisodeModel,
        RCAEvaluationModel,
        CandidateCauseModel,
        FailurePredictionModel,
        InterventionDecisionModel,
        InterventionCommandModel,
        CounterfactualAttributionModel,
    )

    ep1_id = uuid.uuid4()
    eval1_id = uuid.uuid4()
    cand1_id = uuid.uuid4()
    pred1_id = uuid.uuid4()
    dec1_id = uuid.uuid4()
    attr1_id = uuid.uuid4()

    ep2_id = uuid.uuid4()
    eval2_id = uuid.uuid4()
    cand2_id = uuid.uuid4()
    pred2_id = uuid.uuid4()
    dec2_id = uuid.uuid4()
    attr2_id = uuid.uuid4()
    cmd1_id = uuid.uuid4()
    cmd2_id = uuid.uuid4()

    pay1 = f"pay_attempt_{uuid.uuid4()}"
    pay2 = f"pay_attempt_{uuid.uuid4()}"

    now = datetime.now(timezone.utc)

    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        # Episode 1
        ep1 = DegradationEpisodeModel(
            episode_id=ep1_id,
            segment_dimension="BANK",
            segment_value="HDFC",
            started_at_window=now,
            status="ACTIVE",
            peak_absolute_drop=0.68,
            affected_window_count=2,
            severity="CRITICAL",
        )
        eval1 = RCAEvaluationModel(
            evaluation_id=eval1_id,
            episode_id=ep1_id,
            evaluation_version=1,
            classification="SEGMENT_SPECIFIC",
            analysis_window_start=now,
            analysis_window_end=now,
            input_fingerprint="sha256:" + "1" * 64,
            generated_at=now,
            evidence_audit_payload={},
        )
        cand1 = CandidateCauseModel(
            candidate_id=cand1_id,
            evaluation_id=eval1_id,
            candidate_dimension="BANK",
            candidate_value="HDFC",
            evidence_strength="STRONG",
            excess_failure_contribution=0.85,
            actual_segment_failures=10,
            expected_segment_failures=2.0,
            excess_segment_failures=8.0,
            rank=1,
        )
        pred1 = FailurePredictionModel(
            prediction_id=pred1_id,
            payment_attempt_id=pay1,
            prediction_version=1,
            predicted_at=now,
            prediction_horizon="30m",
            failure_probability=0.75,
            risk_band="HIGH",
            prediction_status="PREDICTED",
            model_name="test_model",
            model_version="1.0",
            feature_schema_version="1.0",
            feature_snapshot={},
            input_fingerprint="sha256:" + "1" * 64,
            created_at=now,
        )
        dec1 = InterventionDecisionModel(
            decision_id=dec1_id,
            payment_attempt_id=pay1,
            decision_version=1,
            decided_at=now,
            decision_type="ACT",
            selected_route_id="FALLBACK_PAYMENT_LINK",
            failure_probability=0.75,
            stage5_prediction_id=pred1_id,
            diagnosis_confidence="STRONG",
            decision_confidence=0.9,
            gate_verdict="PASSED",
            policy_id="test_policy",
            policy_version="1.0",
            input_fingerprint="sha256:" + "1" * 64,
            evaluation_audit_payload={"upstream_context": {"episode_id": str(ep1_id)}},
            created_at=now,
        )
        cmd1 = InterventionCommandModel(
            command_id=cmd1_id,
            decision_id=dec1_id,
            payment_attempt_id=pay1,
            decision_version=1,
            route_key="FALLBACK_PAYMENT_LINK",
            policy_id="test_policy",
            policy_version="1.0",
            command_status="SUCCEEDED",
            idempotency_key=f"idem_{cmd1_id}",
            created_at=now,
            expires_at=now,
            updated_at=now,
        )
        attr1 = CounterfactualAttributionModel(
            attribution_id=attr1_id,
            command_id=cmd1_id,
            payment_attempt_id=pay1,
            decision_id=dec1_id,
            attribution_version=1,
            attributed_at=now,
            as_of_timestamp=now,
            attribution_status="ATTRIBUTED",
            methodology_name="COUNTERFACTUAL_V1",
            methodology_version="1.0",
            treatment_status="EXECUTED",
            observed_payment_outcome="CAPTURED",
            payment_amount_minor_units=10000,
            counterfactual_loss_exposure_minor_units=10000,
            counterfactual_natural_success_gmv_minor_units=0,
            attributed_protected_gmv_minor_units=10000,
            attribution_confidence=0.60,
            is_synthetic_baseline=False,
            is_simulated_execution=True,
            attribution_audit_payload={},
            created_at=now,
        )

        # Episode 2
        ep2 = DegradationEpisodeModel(
            episode_id=ep2_id,
            segment_dimension="BANK",
            segment_value="ICICI",
            started_at_window=now,
            status="ACTIVE",
            peak_absolute_drop=0.45,
            affected_window_count=1,
            severity="HIGH",
        )
        eval2 = RCAEvaluationModel(
            evaluation_id=eval2_id,
            episode_id=ep2_id,
            evaluation_version=1,
            classification="SEGMENT_SPECIFIC",
            analysis_window_start=now,
            analysis_window_end=now,
            input_fingerprint="sha256:" + "2" * 64,
            generated_at=now,
            evidence_audit_payload={},
        )
        cand2 = CandidateCauseModel(
            candidate_id=cand2_id,
            evaluation_id=eval2_id,
            candidate_dimension="BANK",
            candidate_value="ICICI",
            evidence_strength="MODERATE",
            excess_failure_contribution=0.50,
            actual_segment_failures=5,
            expected_segment_failures=2.0,
            excess_segment_failures=3.0,
            rank=1,
        )
        pred2 = FailurePredictionModel(
            prediction_id=pred2_id,
            payment_attempt_id=pay2,
            prediction_version=1,
            predicted_at=now,
            prediction_horizon="30m",
            failure_probability=0.71,
            risk_band="HIGH",
            prediction_status="PREDICTED",
            model_name="test_model",
            model_version="1.0",
            feature_schema_version="1.0",
            feature_snapshot={},
            input_fingerprint="sha256:" + "2" * 64,
            created_at=now,
        )
        dec2 = InterventionDecisionModel(
            decision_id=dec2_id,
            payment_attempt_id=pay2,
            decision_version=1,
            decided_at=now,
            decision_type="MONITOR",
            selected_route_id=None,
            failure_probability=0.71,
            stage5_prediction_id=pred2_id,
            diagnosis_confidence="MODERATE",
            decision_confidence=0.8,
            gate_verdict="PASSED",
            policy_id="test_policy",
            policy_version="1.0",
            input_fingerprint="sha256:" + "2" * 64,
            evaluation_audit_payload={"upstream_context": {"episode_id": str(ep2_id)}},
            created_at=now,
        )
        cmd2 = InterventionCommandModel(
            command_id=cmd2_id,
            decision_id=dec2_id,
            payment_attempt_id=pay2,
            decision_version=1,
            route_key="FALLBACK_PAYMENT_LINK",
            policy_id="test_policy",
            policy_version="1.0",
            command_status="SUCCEEDED",
            idempotency_key=f"idem_{cmd2_id}",
            created_at=now,
            expires_at=now,
            updated_at=now,
        )
        attr2 = CounterfactualAttributionModel(
            attribution_id=attr2_id,
            command_id=cmd2_id,
            payment_attempt_id=pay2,
            decision_id=dec2_id,
            attribution_version=1,
            attributed_at=now,
            as_of_timestamp=now,
            attribution_status="ATTRIBUTED",
            methodology_name="COUNTERFACTUAL_V1",
            methodology_version="1.0",
            treatment_status="EXECUTED",
            observed_payment_outcome="CAPTURED",
            payment_amount_minor_units=25000,
            counterfactual_loss_exposure_minor_units=25000,
            counterfactual_natural_success_gmv_minor_units=0,
            attributed_protected_gmv_minor_units=25000,
            attribution_confidence=0.60,
            is_synthetic_baseline=False,
            is_simulated_execution=True,
            attribution_audit_payload={},
            created_at=now,
        )

        session.add_all([ep1, eval1, pred1, dec1, cmd1, ep2, eval2, pred2, dec2, cmd2])
        await session.flush()
        session.add_all([cand1, attr1, cand2, attr2])
        await session.commit()
    # Query Episode 1
    resp1 = await client.get(f"/api/v1/rca/{ep1_id}")
    assert resp1.status_code == 200
    d1 = resp1.json()
    assert d1["prediction_context"]["window_predictions_evaluated"] == 1
    assert d1["prediction_context"]["peak_failure_probability"] == 0.75
    assert d1["intervention_context"]["act_decisions"] == 1
    assert d1["intervention_context"]["monitor_decisions"] == 0
    assert d1["intervention_context"]["primary_selected_route"] == "FALLBACK_PAYMENT_LINK"
    assert d1["intervention_context"]["window_protected_gmv_minor_units"] == 10000

    # Query Episode 2
    resp2 = await client.get(f"/api/v1/rca/{ep2_id}")
    assert resp2.status_code == 200
    d2 = resp2.json()
    assert d2["prediction_context"]["window_predictions_evaluated"] == 1
    assert d2["prediction_context"]["peak_failure_probability"] == 0.71
    assert d2["intervention_context"]["act_decisions"] == 0
    assert d2["intervention_context"]["monitor_decisions"] == 1
    assert d2["intervention_context"]["primary_selected_route"] is None
    assert d2["intervention_context"]["window_protected_gmv_minor_units"] == 25000

@pytest.mark.asyncio
async def test_recovery_workspace_endpoint(client):
    """
    Verifies that GET /api/v1/recovery returns summary statistics and
    dense recovery ledger records with valid structure.
    """
    response = await client.get("/api/v1/recovery?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "summary" in data
    assert "ledger" in data
    assert "total_count" in data
    summary = data["summary"]
    assert "interventions_dispatched" in summary
    assert "act_count" in summary
    assert "monitor_count" in summary
    assert "successful_captures" in summary
    assert "failed_outcomes" in summary
    assert "total_protected_gmv_minor_units" in summary
    assert "attribution_count" in summary
    assert "avg_attribution_confidence" in summary

    # Test decision_type filter
    act_resp = await client.get("/api/v1/recovery?decision_type=ACT&limit=5")
    assert act_resp.status_code == 200
    act_data = act_resp.json()
    for row in act_data["ledger"]:
        assert row["decision_type"] == "ACT"

@pytest.mark.asyncio
async def test_payments_list_endpoint(client):
    """
    Verifies that GET /api/v1/payments returns searchable/filterable payments list.
    """
    response = await client.get("/api/v1/payments?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total_count" in data
    assert isinstance(data["items"], list)

    # Test filtering by filter_type
    intervened_resp = await client.get("/api/v1/payments?filter_type=INTERVENED&limit=5")
    assert intervened_resp.status_code == 200
    for item in intervened_resp.json()["items"]:
        assert item["is_intervened"] is True

@pytest.mark.asyncio
async def test_attributions_enriched_endpoint(client):
    """
    Verifies that GET /api/v1/attributions includes decision_id,
    counterfactual_outcome, and confidence_components.
    """
    response = await client.get("/api/v1/attributions?limit=5")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    if data:
        attr = data[0]
        assert "decision_id" in attr
        assert "counterfactual_outcome" in attr
        assert "confidence_components" in attr
        assert "attribution_confidence" in attr


@pytest.mark.asyncio
async def test_payment_id_roundtrip_between_payments_and_timeline(client, db_engine):
    """
    Regression test:
    1. Create/seed a valid payment.
    2. Fetch it through GET /api/v1/payments.
    3. Extract the exact returned payment_id.
    4. Call GET /api/v1/timeline/{returned_payment_id}.
    5. Assert HTTP 200.
    6. Assert the returned timeline belongs to that exact payment.
    7. Assert the payment_id in the timeline matches the payments endpoint identifier.
    """
    test_pid = f"pay_roundtrip_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        auth_evt = PaymentEventModel(
            event_id=uuid.uuid4(),
            source_system="test",
            source_event_id=f"evt_auth_{uuid.uuid4().hex[:8]}",
            payment_id=test_pid,
            timestamp=now,
            event_type="payment.authorized",
            currency="INR",
            amount_minor_units=45000,
            payment_status="authorized",
            payment_method="UPI",
            bank="HDFC",
            ingested_at=now,
        )
        term_evt = PaymentEventModel(
            event_id=uuid.uuid4(),
            source_system="test",
            source_event_id=f"evt_term_{uuid.uuid4().hex[:8]}",
            payment_id=test_pid,
            timestamp=now,
            event_type="payment.captured",
            currency="INR",
            amount_minor_units=45000,
            payment_status="captured",
            payment_method="UPI",
            bank="HDFC",
            ingested_at=now,
        )
        session.add_all([auth_evt, term_evt])
        await session.commit()
    # 2. Fetch it through GET /api/v1/payments
    payments_resp = await client.get(f"/api/v1/payments?search={test_pid}")
    assert payments_resp.status_code == 200
    payments_data = payments_resp.json()
    assert payments_data["total_count"] >= 1
    matched = [item for item in payments_data["items"] if item["payment_id"] == test_pid]
    assert len(matched) == 1

    # 3. Extract the exact returned payment_id
    returned_payment_id = matched[0]["payment_id"]
    assert returned_payment_id == test_pid

    # 4. Call GET /api/v1/timeline/{returned_payment_id}
    timeline_resp = await client.get(f"/api/v1/timeline/{returned_payment_id}")

    # 5. Assert HTTP 200
    assert timeline_resp.status_code == 200
    timeline_data = timeline_resp.json()

    # 6. Assert the returned timeline belongs to that exact payment
    assert len(timeline_data["events"]) == 2
    assert timeline_data["payment_info"]["amount_minor_units"] == 45000
    assert timeline_data["payment_info"]["payment_method"] == "UPI"
    assert timeline_data["payment_info"]["terminal_status"] == "captured"

    # 7. Assert the payment_id in the timeline matches the payments endpoint identifier
    assert timeline_data["payment_id"] == returned_payment_id
    assert timeline_data["payment_id"] == test_pid


@pytest.mark.asyncio
async def test_nonexistent_payment_returns_404(client):
    """
    Verifies that an actually nonexistent payment returns HTTP 404 with standard detail.
    """
    non_existent_pid = "pay_definitely_nonexistent_999999"
    resp = await client.get(f"/api/v1/timeline/{non_existent_pid}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == f"Payment {non_existent_pid} not found"


@pytest.mark.asyncio
async def test_captured_outcome_without_stage8_attribution_is_not_attributed(client, db_engine):
    """
    Audit 3 Regression test:
    Proves that a successful CAPTURED outcome does NOT automatically imply attribution.
    A payment is ATTRIBUTED only when the canonical Stage 8 attribution record exists
    and has attribution_status == 'ATTRIBUTED'.
    """
    test_pid = f"pay_natural_capture_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        auth_evt = PaymentEventModel(
            event_id=uuid.uuid4(),
            source_system="test",
            source_event_id=f"evt_auth_{uuid.uuid4().hex[:8]}",
            payment_id=test_pid,
            timestamp=now,
            event_type="payment.authorized",
            currency="INR",
            amount_minor_units=60000,
            payment_status="authorized",
            payment_method="UPI",
            bank="HDFC",
            ingested_at=now,
        )
        cap_evt = PaymentEventModel(
            event_id=uuid.uuid4(),
            source_system="test",
            source_event_id=f"evt_cap_{uuid.uuid4().hex[:8]}",
            payment_id=test_pid,
            timestamp=now,
            event_type="payment.captured",
            currency="INR",
            amount_minor_units=60000,
            payment_status="captured",
            payment_method="UPI",
            bank="HDFC",
            ingested_at=now,
        )
        session.add_all([auth_evt, cap_evt])
        await session.commit()
    # Query Payments list
    payments_resp = await client.get(f"/api/v1/payments?search={test_pid}")
    assert payments_resp.status_code == 200
    p_data = payments_resp.json()
    item = next(it for it in p_data["items"] if it["payment_id"] == test_pid)

    assert item["payment_status"] == "captured"
    assert item["is_intervened"] is False
    assert item["is_attributed"] is False
    assert item["attributed_protected_gmv_minor_units"] == 0
    assert item["attribution_confidence"] is None

    # Query Timeline
    timeline_resp = await client.get(f"/api/v1/timeline/{test_pid}")
    assert timeline_resp.status_code == 200
    t_data = timeline_resp.json()
    assert t_data["payment_info"]["terminal_status"] == "captured"
    assert t_data["decision"] is None
    assert t_data["command"] is None
    assert t_data["execution"] is None
    assert t_data["attribution"] is None


@pytest.mark.asyncio
async def test_stage8_counterfactual_semantics_equivalence(client):
    """
    Audit 2 Regression test:
    Proves that counterfactual_outcome exposes the canonical Stage 8 attribution result
    directly without independent threshold re-evaluation in the API layer.
    """
    attr_resp = await client.get("/api/v1/attributions?limit=10")
    assert attr_resp.status_code == 200
    attrs = attr_resp.json()
    for a in attrs:
        if a["status"] == "ATTRIBUTED":
            assert a["counterfactual_outcome"] == "WOULD_HAVE_FAILED"
            assert a["attributed_protected_gmv_minor_units"] > 0
            assert a["attribution_confidence"] > 0
        else:
            assert a["counterfactual_outcome"] == a["status"]




@pytest.mark.asyncio
async def test_payments_list_canonical_events(client, db_engine):
    """
    Test that the payments list correctly handles various lifecycles without duplicates
    and correctly joins recovery information without fabricating it.
    """
    import uuid
    from datetime import datetime, timezone, timedelta
    from src.infrastructure.models import PaymentEventModel, InterventionDecisionModel
    
    now = datetime.now(timezone.utc)
    
    # Setup test payments
    pid_success = f"pay_succ_{uuid.uuid4().hex[:8]}"
    pid_auth_fail = f"pay_afail_{uuid.uuid4().hex[:8]}"
    pid_fail_only = f"pay_fonly_{uuid.uuid4().hex[:8]}"
    
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        # Case 1: payment.authorized -> payment.captured with recovery info
        session.add_all([
            PaymentEventModel(
                event_id=uuid.uuid4(), source_system="test", source_event_id=uuid.uuid4().hex,
                payment_id=pid_success, timestamp=now - timedelta(minutes=5), event_type="payment.authorized",
                currency="INR", amount_minor_units=1000, payment_status="authorized",
                payment_method="UPI", bank="HDFC", ingested_at=now - timedelta(minutes=5)
            ),
            PaymentEventModel(
                event_id=uuid.uuid4(), source_system="test", source_event_id=uuid.uuid4().hex,
                payment_id=pid_success, timestamp=now - timedelta(minutes=4), event_type="payment.captured",
                currency="INR", amount_minor_units=1000, payment_status="captured",
                payment_method="UPI", bank="HDFC", ingested_at=now - timedelta(minutes=4)
            )
        ])
        
        # Add decision for success payment
        dec_id = uuid.uuid4()
        session.add(InterventionDecisionModel(
            decision_id=dec_id, payment_attempt_id=pid_success, decision_version=1,
            decided_at=now - timedelta(minutes=4, seconds=30), decision_type="ACT",
            selected_route_id="ROUTE_A", failure_probability=0.8, stage5_prediction_id=uuid.uuid4(),
            diagnosis_confidence="HIGH", decision_confidence=0.9,
            gate_verdict="PASSED", policy_id="P1", policy_version="1.0",
            input_fingerprint="sha256:0", evaluation_audit_payload={}, created_at=now
        ))
        
        # Case 2: payment.authorized -> payment.failed
        session.add_all([
            PaymentEventModel(
                event_id=uuid.uuid4(), source_system="test", source_event_id=uuid.uuid4().hex,
                payment_id=pid_auth_fail, timestamp=now - timedelta(minutes=3), event_type="payment.authorized",
                currency="INR", amount_minor_units=2000, payment_status="authorized",
                payment_method="UPI", bank="HDFC", ingested_at=now - timedelta(minutes=3)
            ),
            PaymentEventModel(
                event_id=uuid.uuid4(), source_system="test", source_event_id=uuid.uuid4().hex,
                payment_id=pid_auth_fail, timestamp=now - timedelta(minutes=2), event_type="payment.failed",
                currency="INR", amount_minor_units=2000, payment_status="failed",
                payment_method="UPI", bank="HDFC", ingested_at=now - timedelta(minutes=2)
            )
        ])
        
        # Case 3: payment.failed alone
        session.add(
            PaymentEventModel(
                event_id=uuid.uuid4(), source_system="test", source_event_id=uuid.uuid4().hex,
                payment_id=pid_fail_only, timestamp=now - timedelta(minutes=1), event_type="payment.failed",
                currency="INR", amount_minor_units=3000, payment_status="failed",
                payment_method="UPI", bank="HDFC", ingested_at=now - timedelta(minutes=1)
            )
        )
        
        await session.commit()
    # Verification
    # Case 1
    resp_success = await client.get(f"/api/v1/payments?search={pid_success}")
    items = resp_success.json()["items"]
    assert len(items) == 1, "payment.authorized -> captured should return exactly 1 payment"
    assert items[0]["payment_status"] == "captured"
    assert items[0]["is_intervened"] is True
    
    # Case 2
    resp_afail = await client.get(f"/api/v1/payments?search={pid_auth_fail}")
    items = resp_afail.json()["items"]
    assert len(items) == 1, "payment.authorized -> failed should return exactly 1 payment"
    assert items[0]["payment_status"] == "failed"
    assert items[0]["is_intervened"] is False
    
    # Case 3
    resp_fonly = await client.get(f"/api/v1/payments?search={pid_fail_only}")
    items = resp_fonly.json()["items"]
    assert len(items) == 1, "payment.failed alone should return exactly 1 payment"
    assert items[0]["payment_status"] == "failed"
    assert items[0]["is_intervened"] is False
    assert items[0]["is_attributed"] is False
    assert items[0]["amount_minor_units"] == 3000


def _provider_event(now, source_system, payment_id, event_type, payment_status, minutes_ago,
                    currency="INR", amount_minor_units=50000):
    at = now - timedelta(minutes=minutes_ago)
    return PaymentEventModel(
        event_id=uuid.uuid4(), source_system=source_system, source_event_id=uuid.uuid4().hex,
        payment_id=payment_id, timestamp=at, event_type=event_type, currency=currency,
        amount_minor_units=amount_minor_units, payment_status=payment_status,
        payment_method="UPI" if source_system == "razorpay" else None, ingested_at=at,
    )


async def _seed_razorpay_payment_with_paypal_events(db_engine):
    """
    Seeds one Razorpay payment (authorized, then captured), PayPal events reusing its payment_id
    (an earlier decline and a later refund) and a separate PayPal-only capture.
    """
    now = datetime.now(timezone.utc)
    razorpay_pid = f"pay_rzp_{uuid.uuid4().hex[:8]}"
    paypal_pid = f"PAYPAL{uuid.uuid4().hex[:11].upper()}"
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        session.add_all([
            _provider_event(now, "razorpay", razorpay_pid, "payment.authorized", "authorized", 5),
            _provider_event(now, "razorpay", razorpay_pid, "payment.captured", "captured", 4),
            _provider_event(now, "paypal", razorpay_pid, "payment.failed", "failed", 10, "USD", 7000),
            _provider_event(now, "paypal", razorpay_pid, "payment.refunded", "refunded", 1, "USD", 7000),
            _provider_event(now, "paypal", paypal_pid, "payment.captured", "captured", 3, "USD", 9000),
        ])
        await session.commit()
    return razorpay_pid, paypal_pid


@pytest.mark.asyncio
async def test_summary_excludes_paypal_events(client, db_engine):
    await _seed_razorpay_payment_with_paypal_events(db_engine)

    resp = await client.get("/api/v1/summary")

    assert resp.status_code == 200
    data = resp.json()
    assert (data["total_auths"], data["total_captures"], data["overall_success_rate"]) == (1, 1, 1.0)
    assert data["total_gmv_minor_units"] == 50000


@pytest.mark.asyncio
async def test_payments_list_excludes_paypal_events(client, db_engine):
    razorpay_pid, paypal_pid = await _seed_razorpay_payment_with_paypal_events(db_engine)

    data = (await client.get("/api/v1/payments")).json()
    assert data["total_count"] == 1
    [item] = data["items"]
    assert item["payment_id"] == razorpay_pid
    assert (item["payment_status"], item["currency"], item["amount_minor_units"]) == ("captured", "INR", 50000)

    for status, expected in (("captured", [razorpay_pid]), ("failed", []), ("authorized", [])):
        filtered = (await client.get(f"/api/v1/payments?status={status}")).json()
        assert [i["payment_id"] for i in filtered["items"]] == expected
        assert filtered["total_count"] == len(expected)

    searched = (await client.get(f"/api/v1/payments?search={paypal_pid}")).json()
    assert (searched["items"], searched["total_count"]) == ([], 0)


@pytest.mark.asyncio
async def test_timeline_excludes_paypal_events(client, db_engine):
    from src.infrastructure.models import InterventionDecisionModel

    razorpay_pid, paypal_pid = await _seed_razorpay_payment_with_paypal_events(db_engine)
    now = datetime.now(timezone.utc)
    dec_id = uuid.uuid4()
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        session.add(InterventionDecisionModel(
            decision_id=dec_id, payment_attempt_id=razorpay_pid, decision_version=1,
            decided_at=now - timedelta(minutes=4, seconds=30), decision_type="ACT",
            selected_route_id="ROUTE_A", failure_probability=0.8, stage5_prediction_id=uuid.uuid4(),
            diagnosis_confidence="HIGH", decision_confidence=0.9,
            gate_verdict="PASSED", policy_id="P1", policy_version="1.0",
            input_fingerprint="sha256:0", evaluation_audit_payload={}, created_at=now
        ))
        await session.commit()

    resp = await client.get(f"/api/v1/timeline/{razorpay_pid}")
    assert resp.status_code == 200
    data = resp.json()
    assert [e["event_type"] for e in data["events"]] == ["payment.authorized", "payment.captured"]
    assert data["payment_info"]["currency"] == "INR"
    assert data["payment_info"]["amount_minor_units"] == 50000
    assert data["payment_info"]["terminal_status"] == "captured"
    assert data["decision"]["decision_id"] == str(dec_id)

    paypal_resp = await client.get(f"/api/v1/timeline/{paypal_pid}")
    assert paypal_resp.status_code == 404
    assert paypal_resp.json()["detail"] == f"Payment {paypal_pid} not found"


# --- Phase 1: latest-version aggregation, per-currency money and provenance ---------------------

HEURISTIC_MODEL = ("DeterministicBaselineModel", "untrained-heuristic-v2")


async def _seed_attributed_payment(
    db_engine,
    *,
    currency="INR",
    amount=100000,
    protected_versions=(10000,),
    observation_versions=1,
    authorized=True,
    simulated=True,
    executor_name="SIMULATOR_V1",
    model=HEURISTIC_MODEL,
    model_provenance="SYNTHETIC_DEVELOPMENT",
    episode_id=None,
):
    """
    Seeds one Stage 1-8 trace: authorization and capture events, a prediction, an ACT decision,
    a SUCCEEDED command with one execution attempt, `observation_versions` outcome observations and
    one attribution row per entry in `protected_versions` (versions 1..n of the same command).
    """
    now = datetime.now(timezone.utc)
    pid = f"pay_phase1_{uuid.uuid4().hex[:10]}"
    prediction_id, decision_id, command_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        if authorized:
            session.add(PaymentEventModel(
                event_id=uuid.uuid4(), source_system="razorpay", source_event_id=uuid.uuid4().hex,
                payment_id=pid, timestamp=now - timedelta(minutes=10), event_type="payment.authorized",
                currency=currency, amount_minor_units=amount, payment_status="authorized",
                payment_method="UPI", ingested_at=now - timedelta(minutes=10),
            ))
        session.add_all([
            PaymentEventModel(
                event_id=uuid.uuid4(), source_system="razorpay", source_event_id=uuid.uuid4().hex,
                payment_id=pid, timestamp=now - timedelta(minutes=5), event_type="payment.captured",
                currency=currency, amount_minor_units=amount, payment_status="captured",
                payment_method="UPI", ingested_at=now - timedelta(minutes=5),
            ),
            FailurePredictionModel(
                prediction_id=prediction_id, payment_attempt_id=pid, prediction_version=1,
                predicted_at=now - timedelta(minutes=10), prediction_horizon="30m", failure_probability=0.8,
                risk_band="HIGH", prediction_status="PREDICTED", model_name=model[0], model_version=model[1],
                feature_schema_version="v1.0.0", feature_snapshot={}, input_fingerprint="sha256:" + "a" * 64,
                created_at=now,
            ),
            InterventionDecisionModel(
                decision_id=decision_id, payment_attempt_id=pid, decision_version=1,
                decided_at=now - timedelta(minutes=9), decision_type="ACT", selected_route_id="FALLBACK_PAYMENT_LINK",
                failure_probability=0.8, stage5_prediction_id=prediction_id, diagnosis_confidence="STRONG",
                decision_confidence=0.9, gate_verdict="PASSED", policy_id="default-recovery-v1",
                policy_version="1.0.0", input_fingerprint="sha256:" + "b" * 64,
                evaluation_audit_payload={"upstream_context": {"episode_id": str(episode_id) if episode_id else None}},
                created_at=now,
            ),
            InterventionCommandModel(
                command_id=command_id, decision_id=decision_id, payment_attempt_id=pid, decision_version=1,
                route_key="FALLBACK_PAYMENT_LINK", policy_id="default-recovery-v1", policy_version="1.0.0",
                command_status="SUCCEEDED", idempotency_key=f"cmd_{decision_id}",
                created_at=now - timedelta(minutes=9), expires_at=now, updated_at=now - timedelta(minutes=9),
            ),
            InterventionExecutionAttemptModel(
                attempt_id=uuid.uuid4(), command_id=command_id, attempt_number=1,
                started_at=now - timedelta(minutes=9), completed_at=now - timedelta(minutes=9), duration_ms=15,
                execution_status="SUCCESS", executor_name=executor_name, is_simulation=simulated,
                provider_action_type="PAYMENT_LINK_DISPATCH", provider_response_payload={},
                created_at=now - timedelta(minutes=9),
            ),
        ])
        for version in range(1, observation_versions + 1):
            observed = now - timedelta(minutes=5) + timedelta(seconds=version)
            session.add(PaymentOutcomeObservationModel(
                observation_id=uuid.uuid4(), command_id=command_id, payment_attempt_id=pid,
                observation_version=version, observed_at=observed, as_of_timestamp=observed,
                payment_outcome="CAPTURED", observation_audit_payload={}, created_at=observed,
            ))
        await session.flush()
        for version, protected in enumerate(protected_versions, start=1):
            attributed = now - timedelta(minutes=4) + timedelta(seconds=version)
            session.add(CounterfactualAttributionModel(
                attribution_id=uuid.uuid4(), command_id=command_id, payment_attempt_id=pid,
                decision_id=decision_id, attribution_version=version, attributed_at=attributed,
                as_of_timestamp=attributed, attribution_status="ATTRIBUTED",
                methodology_name="COUNTERFACTUAL_RISK_WEIGHTED_V1", methodology_version="1.0.0",
                treatment_status="TREATED", observed_payment_outcome="CAPTURED", payment_amount_minor_units=amount,
                counterfactual_failure_probability=0.8, counterfactual_loss_exposure_minor_units=protected,
                counterfactual_natural_success_gmv_minor_units=amount - protected,
                attributed_protected_gmv_minor_units=protected, attribution_confidence=0.6,
                is_synthetic_baseline=model_provenance != "EMPIRICAL_PRODUCTION", is_simulated_execution=simulated,
                attribution_audit_payload={"stage5_model_id": model[0], "stage5_model_provenance": model_provenance},
                created_at=attributed,
            ))
        await session.commit()
    return SimpleNamespace(payment_id=pid, decision_id=decision_id, command_id=command_id)


async def _seed_episode_with_rca(db_engine):
    episode_id, now = uuid.uuid4(), datetime.now(timezone.utc)
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        session.add(DegradationEpisodeModel(
            episode_id=episode_id, segment_dimension="BANK", segment_value="HDFC", started_at_window=now,
            status="ACTIVE", peak_absolute_drop=0.5, affected_window_count=2, severity="CRITICAL",
        ))
        await session.flush()
        session.add(RCAEvaluationModel(
            evaluation_id=uuid.uuid4(), episode_id=episode_id, evaluation_version=1, classification="SEGMENT_SPECIFIC",
            analysis_window_start=now, analysis_window_end=now, input_fingerprint="sha256:" + "c" * 64,
            generated_at=now, evidence_audit_payload={},
        ))
        await session.commit()
    return episode_id


def _inr(amount, simulated=1, synthetic=1, positive=1):
    return {
        "currency": "INR", "amount_minor_units": amount, "positive_attribution_count": positive,
        "simulated_execution_count": simulated, "synthetic_baseline_count": synthetic,
    }


@pytest.mark.asyncio
async def test_superseded_attribution_versions_are_not_double_counted(client, db_engine):
    episode_id = await _seed_episode_with_rca(db_engine)
    trace = await _seed_attributed_payment(
        db_engine, protected_versions=(10000, 4000), observation_versions=2, episode_id=episode_id
    )

    summary = (await client.get("/api/v1/summary")).json()
    assert summary["total_protected_gmv_minor_units"] == 4000
    assert summary["total_protected_gmv_by_currency"] == [_inr(4000)]

    recovery = (await client.get("/api/v1/recovery")).json()
    assert recovery["summary"]["total_protected_gmv_minor_units"] == 4000
    assert recovery["summary"]["total_protected_gmv_by_currency"] == [_inr(4000)]
    assert (recovery["summary"]["attribution_count"], recovery["summary"]["rescued_count"]) == (1, 1)
    assert recovery["summary"]["successful_captures"] == 1
    rows = [row for row in recovery["ledger"] if row["decision_id"] == str(trace.decision_id)]
    assert len(rows) == 1 and recovery["total_count"] == 1
    assert (rows[0]["attributed_protected_gmv_minor_units"], rows[0]["currency"]) == (4000, "INR")

    latest = (await client.get("/api/v1/attributions")).json()
    assert [(a["attribution_version"], a["attributed_protected_gmv_minor_units"]) for a in latest] == [(2, 4000)]
    history = (await client.get("/api/v1/attributions?include_history=true")).json()
    assert sorted(a["attribution_version"] for a in history) == [1, 2]

    payments = (await client.get(f"/api/v1/payments?search={trace.payment_id}")).json()
    assert payments["total_count"] == 1
    assert [i["attributed_protected_gmv_minor_units"] for i in payments["items"]] == [4000]

    timeline = (await client.get(f"/api/v1/timeline/{trace.payment_id}")).json()
    assert (timeline["attribution"]["attribution_version"], timeline["attribution"]["protected_gmv"]) == (2, 4000)

    rca = (await client.get(f"/api/v1/rca/{episode_id}")).json()
    assert rca["intervention_context"]["window_protected_gmv_minor_units"] == 4000
    assert rca["intervention_context"]["window_protected_gmv_by_currency"] == [_inr(4000)]


@pytest.mark.asyncio
async def test_amounts_in_different_currencies_are_never_summed(client, db_engine):
    inr = await _seed_attributed_payment(db_engine, currency="INR", amount=100000, protected_versions=(10000,))
    usd = await _seed_attributed_payment(db_engine, currency="USD", amount=7000, protected_versions=(2000,))
    unknown = await _seed_attributed_payment(db_engine, amount=3000, protected_versions=(500,), authorized=False)

    summary = (await client.get("/api/v1/summary")).json()
    assert summary["total_gmv_minor_units"] is None
    assert summary["total_gmv_by_currency"] == [
        {"currency": "INR", "amount_minor_units": 100000},
        {"currency": "USD", "amount_minor_units": 7000},
    ]
    assert summary["total_protected_gmv_minor_units"] is None
    assert [(g["currency"], g["amount_minor_units"]) for g in summary["total_protected_gmv_by_currency"]] == [
        ("INR", 10000), ("USD", 2000), (None, 500),
    ]

    recovery = (await client.get("/api/v1/recovery")).json()
    assert recovery["summary"]["total_protected_gmv_minor_units"] is None
    assert recovery["summary"]["total_protected_gmv_by_currency"] == summary["total_protected_gmv_by_currency"]
    ledger_currency = {row["payment_attempt_id"]: row["currency"] for row in recovery["ledger"]}
    assert ledger_currency == {inr.payment_id: "INR", usd.payment_id: "USD", unknown.payment_id: None}

    attributions = (await client.get("/api/v1/attributions")).json()
    assert {a["payment_attempt_id"]: a["currency"] for a in attributions} == ledger_currency


@pytest.mark.asyncio
async def test_single_currency_totals_keep_the_legacy_scalar_fields(client, db_engine):
    await _seed_attributed_payment(db_engine, currency="USD", amount=7000, protected_versions=(2000,))
    await _seed_attributed_payment(db_engine, currency="USD", amount=3000, protected_versions=(1000,))

    summary = (await client.get("/api/v1/summary")).json()
    assert summary["total_gmv_minor_units"] == 10000
    assert summary["total_gmv_by_currency"] == [{"currency": "USD", "amount_minor_units": 10000}]
    assert summary["total_protected_gmv_minor_units"] == 3000
    assert [(g["currency"], g["amount_minor_units"]) for g in summary["total_protected_gmv_by_currency"]] == [
        ("USD", 3000)
    ]


@pytest.mark.asyncio
async def test_provenance_reports_simulated_execution_and_heuristic_predictions(client, db_engine):
    simulated = await _seed_attributed_payment(db_engine)
    other = await _seed_attributed_payment(
        db_engine, simulated=False, executor_name="TEST_NON_SIMULATED_EXECUTOR",
        model=("ExternalTestModel", "v9"), model_provenance="UNKNOWN",
    )

    summary = (await client.get("/api/v1/summary")).json()
    assert (summary["total_successful_interventions"], summary["simulated_successful_interventions"]) == (2, 1)
    assert summary["execution_provenance"] == [
        {"executor_name": "SIMULATOR_V1", "is_simulation": True, "succeeded_command_count": 1},
        {"executor_name": "TEST_NON_SIMULATED_EXECUTOR", "is_simulation": False, "succeeded_command_count": 1},
    ]
    assert summary["prediction_models"] == [
        {"model_name": "DeterministicBaselineModel", "model_version": "untrained-heuristic-v2",
         "model_provenance": "SYNTHETIC_DEVELOPMENT", "prediction_count": 1},
        {"model_name": "ExternalTestModel", "model_version": "v9",
         "model_provenance": "UNKNOWN", "prediction_count": 1},
    ]

    timeline = (await client.get(f"/api/v1/timeline/{simulated.payment_id}")).json()
    assert {k: timeline["prediction"][k] for k in ("model_name", "model_version", "model_provenance")} == {
        "model_name": "DeterministicBaselineModel", "model_version": "untrained-heuristic-v2",
        "model_provenance": "SYNTHETIC_DEVELOPMENT",
    }
    assert (timeline["execution"]["executor_name"], timeline["execution"]["is_simulation"]) == ("SIMULATOR_V1", True)
    attribution = timeline["attribution"]
    assert (attribution["is_simulated_execution"], attribution["is_synthetic_baseline"]) == (True, True)
    assert attribution["stage5_model_provenance"] == "SYNTHETIC_DEVELOPMENT"
    assert (attribution["methodology_name"], attribution["methodology_version"]) == (
        "COUNTERFACTUAL_RISK_WEIGHTED_V1", "1.0.0"
    )
    other_timeline = (await client.get(f"/api/v1/timeline/{other.payment_id}")).json()
    assert other_timeline["execution"]["is_simulation"] is False
    assert other_timeline["prediction"]["model_provenance"] == "UNKNOWN"

    recovery = (await client.get("/api/v1/recovery")).json()
    assert recovery["summary"]["simulated_execution_count"] == 1
    flags = {row["payment_attempt_id"]: (row["executor_name"], row["is_simulation"]) for row in recovery["ledger"]}
    assert flags == {
        simulated.payment_id: ("SIMULATOR_V1", True),
        other.payment_id: ("TEST_NON_SIMULATED_EXECUTOR", False),
    }

    attributions = {a["payment_attempt_id"]: a for a in (await client.get("/api/v1/attributions")).json()}
    assert attributions[simulated.payment_id]["is_simulated_execution"] is True
    assert attributions[other.payment_id]["is_simulated_execution"] is False
    assert attributions[simulated.payment_id]["stage5_model_provenance"] == "SYNTHETIC_DEVELOPMENT"

    payments = (await client.get(f"/api/v1/payments?search={simulated.payment_id}")).json()
    assert payments["items"][0]["is_simulated_execution"] is True


@pytest.mark.asyncio
async def test_payments_list_uses_only_the_latest_decision_version(client, db_engine):
    pid = f"pay_versions_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    Session = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with Session() as session:
        session.add(PaymentEventModel(
            event_id=uuid.uuid4(), source_system="razorpay", source_event_id=uuid.uuid4().hex, payment_id=pid,
            timestamp=now, event_type="payment.authorized", currency="INR", amount_minor_units=1000,
            payment_status="authorized", payment_method="UPI", ingested_at=now,
        ))
        for version, decision_type in ((1, "MONITOR"), (2, "ACT")):
            session.add(InterventionDecisionModel(
                decision_id=uuid.uuid4(), payment_attempt_id=pid, decision_version=version,
                decided_at=now + timedelta(seconds=version), decision_type=decision_type,
                selected_route_id="FALLBACK_PAYMENT_LINK" if decision_type == "ACT" else None,
                failure_probability=0.8, stage5_prediction_id=uuid.uuid4(), diagnosis_confidence="STRONG",
                decision_confidence=0.9, gate_verdict="PASSED", policy_id="P1", policy_version="1.0",
                input_fingerprint=f"sha256:{version}", evaluation_audit_payload={}, created_at=now,
            ))
        await session.commit()

    data = (await client.get(f"/api/v1/payments?search={pid}")).json()

    assert data["total_count"] == 1
    assert [(i["payment_id"], i["decision_type"]) for i in data["items"]] == [(pid, "ACT")]
