import uuid
from datetime import datetime, timezone
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.main import app
from src.api.dependencies import get_db_session

from src.infrastructure.models import Base, PaymentEventModel

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"

@pytest_asyncio.fixture
async def client():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def override_get_db_session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db_session, None)
    await engine.dispose()

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
async def test_health_check_db_failure():
    async def failing_get_db_session():
        class FailingSession:
            async def execute(self, stmt):
                raise Exception("Connection refused")
        yield FailingSession()

    app.dependency_overrides[get_db_session] = failing_get_db_session
    async with AsyncClient(app=app, base_url="http://test") as ac:
        response = await ac.get("/health")
    app.dependency_overrides.pop(get_db_session, None)
    assert response.status_code == 503
    assert "Database unreachable" in response.json()["detail"]

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
async def test_rca_success_with_seed_data(client):
    import uuid
    from datetime import datetime, timezone
    from src.infrastructure.models import DegradationEpisodeModel, RCAEvaluationModel, CandidateCauseModel

    ep_id = uuid.uuid4()
    eval_id = uuid.uuid4()
    cand_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # Insert episode and RCA evaluation via session
    engine = create_async_engine(DATABASE_URL, echo=False)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
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
        session.add_all([episode, evaluation, cause])
        await session.commit()
    await engine.dispose()

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
async def test_rca_episode_scoping_no_cross_episode_leakage(client):
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

    pay1 = f"pay_attempt_{uuid.uuid4()}"
    pay2 = f"pay_attempt_{uuid.uuid4()}"

    now = datetime.now(timezone.utc)

    engine = create_async_engine(DATABASE_URL, echo=False)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
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
        attr1 = CounterfactualAttributionModel(
            attribution_id=attr1_id,
            command_id=uuid.uuid4(),
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
        attr2 = CounterfactualAttributionModel(
            attribution_id=attr2_id,
            command_id=uuid.uuid4(),
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

        session.add_all([ep1, eval1, cand1, pred1, dec1, attr1, ep2, eval2, cand2, pred2, dec2, attr2])
        await session.commit()
    await engine.dispose()

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
async def test_payment_id_roundtrip_between_payments_and_timeline(client):
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

    engine = create_async_engine(DATABASE_URL, echo=False)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
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
    await engine.dispose()

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
async def test_captured_outcome_without_stage8_attribution_is_not_attributed(client):
    """
    Audit 3 Regression test:
    Proves that a successful CAPTURED outcome does NOT automatically imply attribution.
    A payment is ATTRIBUTED only when the canonical Stage 8 attribution record exists
    and has attribution_status == 'ATTRIBUTED'.
    """
    test_pid = f"pay_natural_capture_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    engine = create_async_engine(DATABASE_URL, echo=False)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
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
    await engine.dispose()

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



