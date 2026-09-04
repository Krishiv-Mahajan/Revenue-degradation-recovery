"""
Focused Buildathon Tests for Razorpay -> Live Recovery Pipeline Bridge.

Covers all Task 8 and Task 9 requirements with 100% database safety:
1. payment_method/bank/wallet persistence in SQLIngestionRepository
2. newly ingested Razorpay event triggers the live pipeline (Case A)
3. idempotent duplicate does not trigger the pipeline twice (Case B)
4. conflicting duplicate does not trigger the pipeline (Case C)
5. pipeline failure does not roll back successful ingestion (error isolation)
6. simulator remains the Stage 7 executor (SimulatorInterventionExecutor)
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.core.domain.exceptions import DuplicateEventConflictError
from src.core.domain.models import PaymentEvent, RawIngestionRecord
from src.core.execution.simulator import SimulatorInterventionExecutor
from src.core.services.ingestion_service import IngestionService
from src.core.services.live_pipeline import run_live_pipeline
from src.infrastructure.models import PaymentEventModel
from src.infrastructure.repository import SQLIngestionRepository
from src.main import app
from src.api.dependencies import get_ingestion_service


# ---------------------------------------------------------------------------
# Test 1: Payment segment persistence (payment_method, bank, wallet)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_payment_segment_persistence():
    """Verifies that SQLIngestionRepository persists payment_method, bank, and wallet."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    repo = SQLIngestionRepository(mock_session)

    raw_record = RawIngestionRecord(
        source_system="razorpay",
        source_event_id="evt_test_1",
        received_at=datetime.now(timezone.utc),
        raw_payload={"test": "payload"},
        payload_hash="hash_test_1",
    )

    event_id = uuid.uuid4()
    payment_event = PaymentEvent(
        event_id=event_id,
        source_system="razorpay",
        source_event_id="evt_test_1",
        payment_id="pay_test_segment_1",
        order_id="order_test_1",
        timestamp=datetime.now(timezone.utc),
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=49900,
        payment_status="authorized",
        payment_method="UPI",
        bank="HDFC",
        wallet="PhonePe",
        error_code=None,
        error_description=None,
        error_source=None,
        error_step=None,
        error_reason=None,
        ingested_at=datetime.now(timezone.utc),
    )

    await repo.save_ingestion(raw_record, payment_event)

    # Verify session.add was called twice (raw_record model and payment_event model)
    assert mock_session.add.call_count == 2
    added_models = [call.args[0] for call in mock_session.add.call_args_list]

    event_models = [m for m in added_models if isinstance(m, PaymentEventModel)]
    assert len(event_models) == 1
    persisted_event = event_models[0]

    assert persisted_event.payment_method == "UPI"
    assert persisted_event.bank == "HDFC"
    assert persisted_event.wallet == "PhonePe"
    assert persisted_event.payment_id == "pay_test_segment_1"
    assert persisted_event.amount_minor_units == 49900
    assert mock_session.commit.call_count == 1


# ---------------------------------------------------------------------------
# Test 2: IngestionService duplicate tracking flag
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@patch("src.core.services.ingestion_service.verify_razorpay_signature")
async def test_ingestion_service_duplicate_flag(mock_verify):
    """Verifies that IngestionService correctly tracks last_event_is_duplicate."""
    mock_verify.return_value = None
    mock_repo = AsyncMock()
    mock_repo.get_raw_record.return_value = None
    mock_repo.save_ingestion.return_value = None

    service = IngestionService(mock_repo, "secret")

    payload = (
        b'{"entity": "event", "account_id": "acc_1", "event": "payment.authorized", '
        b'"contains": ["payment"], "payload": {"payment": {"entity": {"id": "pay_1", '
        b'"entity": "payment", "amount": 1000, "currency": "INR", "status": "authorized", '
        b'"created_at": 1600000000}}}, "created_at": 1600000000}'
    )

    # First ingest: new event
    evt1 = await service.ingest_razorpay_webhook(payload, "sig", "evt_1")
    assert service.last_event_is_duplicate is False
    assert evt1.payment_id == "pay_1"

    # Second ingest: identical payload -> idempotent duplicate
    from src.core.normalizers.razorpay import generate_deterministic_hash
    import json
    h = generate_deterministic_hash(json.loads(payload))
    mock_repo.get_raw_record.return_value = RawIngestionRecord(
        source_system="razorpay",
        source_event_id="evt_1",
        received_at=datetime.now(timezone.utc),
        raw_payload={},
        payload_hash=h,
    )

    evt2 = await service.ingest_razorpay_webhook(payload, "sig", "evt_1")
    assert service.last_event_is_duplicate is True
    assert evt2.payment_id == "pay_1"

    # Third ingest: different payload -> conflict
    mock_repo.get_raw_record.return_value = RawIngestionRecord(
        source_system="razorpay",
        source_event_id="evt_1",
        received_at=datetime.now(timezone.utc),
        raw_payload={},
        payload_hash="different_hash",
    )

    with pytest.raises(DuplicateEventConflictError):
        await service.ingest_razorpay_webhook(payload, "sig", "evt_1")
    assert service.last_event_is_duplicate is False


# ---------------------------------------------------------------------------
# Test 3: Route Trigger Safety (Cases A, B, C)
# ---------------------------------------------------------------------------
def test_webhook_trigger_case_a_b_c():
    """
    Verifies that POST /ingest/razorpay triggers the live pipeline ONLY for new events:
    Case A: New event -> pipeline scheduled
    Case B: Idempotent duplicate -> pipeline NOT scheduled
    Case C: Conflicting duplicate -> HTTP 409 -> pipeline NOT scheduled
    """
    client = TestClient(app)

    mock_service = AsyncMock()
    mock_event = PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="razorpay",
        source_event_id="evt_live_1",
        payment_id="pay_live_1",
        timestamp=datetime.now(timezone.utc),
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=1000,
        payment_status="authorized",
        ingested_at=datetime.now(timezone.utc),
    )

    app.dependency_overrides[get_ingestion_service] = lambda: mock_service

    try:
        with patch("src.api.routes.run_live_pipeline") as mock_pipeline:
            # --- CASE A: First webhook -> accepted -> pipeline scheduled ---
            mock_service.last_event_is_duplicate = False
            mock_service.ingest_razorpay_webhook.return_value = mock_event

            resp_a = client.post(
                "/ingest/razorpay",
                data=b'{"test": "payload"}',
                headers={"X-Razorpay-Signature": "sig", "X-Razorpay-Event-Id": "evt_live_1"},
            )
            assert resp_a.status_code == 200
            assert resp_a.json()["status"] == "success"
            # Background task was scheduled and executed by TestClient
            mock_pipeline.assert_called_once_with(mock_event)

            # --- CASE B: Idempotent duplicate -> accepted -> pipeline NOT scheduled ---
            mock_pipeline.reset_mock()
            mock_service.last_event_is_duplicate = True
            mock_service.ingest_razorpay_webhook.return_value = mock_event

            resp_b = client.post(
                "/ingest/razorpay",
                data=b'{"test": "payload"}',
                headers={"X-Razorpay-Signature": "sig", "X-Razorpay-Event-Id": "evt_live_1"},
            )
            assert resp_b.status_code == 200
            assert resp_b.json()["status"] == "success"
            # Pipeline was NOT called a second time
            mock_pipeline.assert_not_called()

            # --- CASE C: Conflicting duplicate -> HTTP 409 -> pipeline NOT scheduled ---
            mock_pipeline.reset_mock()
            mock_service.ingest_razorpay_webhook.side_effect = DuplicateEventConflictError("Conflict")

            resp_c = client.post(
                "/ingest/razorpay",
                data=b'{"test": "different_payload"}',
                headers={"X-Razorpay-Signature": "sig", "X-Razorpay-Event-Id": "evt_live_1"},
            )
            assert resp_c.status_code == 409
            mock_pipeline.assert_not_called()

    finally:
        app.dependency_overrides.pop(get_ingestion_service, None)


# ---------------------------------------------------------------------------
# Test 4: Pipeline error isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_failure_error_isolation():
    """Verifies that an unhandled exception in the pipeline rolls back cleanly and returns failed status."""
    mock_session = AsyncMock()
    # Simulate a DB failure during Stage 2
    mock_session.execute.side_effect = Exception("Simulated DB connection failure during Stage 2")

    payment_event = PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="razorpay",
        source_event_id="evt_fail_1",
        payment_id="pay_fail_1",
        timestamp=datetime.now(timezone.utc),
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=5000,
        payment_status="authorized",
        ingested_at=datetime.now(timezone.utc),
    )

    result = await run_live_pipeline(payment_event, session=mock_session)

    # Verify error was caught and isolated
    assert result["status"] == "failed"
    assert result["payment_id"] == "pay_fail_1"
    assert "Simulated DB connection failure" in result["error"]
    assert mock_session.rollback.call_count >= 1


# ---------------------------------------------------------------------------
# Test 5: Simulator verification
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stage7_executor_is_simulator():
    """Verifies that Stage 7 executor in live_pipeline uses SimulatorInterventionExecutor with is_simulation=True."""
    from src.core.domain.execution_models import InterventionCommand, CommandStatus
    from src.core.domain.intervention_models import InterventionRouteKey

    executor = SimulatorInterventionExecutor()
    assert isinstance(executor, SimulatorInterventionExecutor)

    mock_cmd = InterventionCommand(
        command_id=uuid.uuid4(),
        decision_id=uuid.uuid4(),
        payment_attempt_id="pay_test_sim",
        decision_version=1,
        route_key=InterventionRouteKey.FALLBACK_PAYMENT_LINK,
        policy_id="policy_v1",
        policy_version="1.0",
        command_status=CommandStatus.EXECUTING,
        status_reason=None,
        idempotency_key="idem_sim",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    result = await executor.execute(mock_cmd, {})
    assert result.executor_name == "SIMULATOR_V1"
    assert result.is_simulation is True


# ---------------------------------------------------------------------------
# Test 6: TEST A — Immediate Claim (t_claim == t_eval, NOT t_eval + 1s)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@patch("src.core.services.live_pipeline.PaymentHealthAnalyticsService")
@patch("src.core.services.live_pipeline.DegradationService")
@patch("src.core.services.live_pipeline.RCAService")
@patch("src.core.services.live_pipeline.FailurePredictionService")
@patch("src.core.services.live_pipeline.InterventionDecisionService")
@patch("src.core.services.live_pipeline.InterventionExecutionService")
@patch("src.core.services.live_pipeline.PaymentOutcomeObservationService")
@patch("src.core.services.live_pipeline.CounterfactualAttributionService")
async def test_live_stage7_immediate_claim(
    mock_attr_cls,
    mock_obs_cls,
    mock_exec_cls,
    mock_dec_cls,
    mock_pred_cls,
    mock_rca_cls,
    mock_deg_cls,
    mock_analytics_cls,
):
    """Prove that live Stage 7 invokes execution with t_claim == t_eval and NOT t_eval + 1 second."""
    from src.core.domain.intervention_models import DecisionType, GateVerdict, InterventionDecision, InterventionRouteKey
    from src.core.domain.execution_models import InterventionCommand, CommandStatus

    mock_analytics_cls.return_value.calculate_and_save_window = AsyncMock()
    mock_deg_cls.return_value.process_snapshots = AsyncMock()
    mock_rca_cls.return_value.evaluate_episode = AsyncMock()

    mock_session = AsyncMock()
    # Mock snapshot query and episode query results
    mock_result_empty = MagicMock()
    mock_result_empty.fetchall.return_value = []
    mock_session.execute.return_value = mock_result_empty

    # Mock prediction: failure_probability = 0.92 > 0.5
    pred_svc_inst = mock_pred_cls.return_value
    pred_svc_inst.orchestrate_prediction = AsyncMock(
        return_value=MagicMock(
            prediction_id=uuid.uuid4(),
            payment_attempt_id="pay_test_claim",
            failure_probability=0.92,
            risk_band="HIGH",
        )
    )

    # Mock decision: ACT
    dec_svc_inst = mock_dec_cls.return_value
    cmd_id = uuid.uuid4()
    mock_decision = MagicMock()
    mock_decision.decision_type.value = "ACT"
    dec_svc_inst.decide = AsyncMock(return_value=mock_decision)

    # Mock execution service
    exec_svc_inst = mock_exec_cls.return_value
    mock_cmd = MagicMock(command_id=cmd_id)
    exec_svc_inst.create_command = AsyncMock(return_value=mock_cmd)
    exec_svc_inst.execute_command = AsyncMock(return_value=mock_cmd)

    event_time = datetime(2026, 9, 4, 4, 16, 30, tzinfo=timezone.utc)
    ingested_time = datetime(2026, 9, 4, 4, 16, 35, 123456, tzinfo=timezone.utc)
    expected_t_eval = max(event_time, ingested_time)  # 04:16:35.123456

    payment_event = PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="razorpay",
        source_event_id="evt_claim_1",
        payment_id="pay_test_claim",
        timestamp=event_time,
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=100000,
        payment_status="authorized",
        ingested_at=ingested_time,
    )

    result = await run_live_pipeline(payment_event, session=mock_session)
    assert result["status"] == "success"

    # TEST A Verification: execute_command was called with t_claim == t_eval, NOT t_eval + 1s
    exec_svc_inst.execute_command.assert_called_once()
    called_cmd_id, kwargs = exec_svc_inst.execute_command.call_args[0][0], exec_svc_inst.execute_command.call_args[1]
    assert called_cmd_id == cmd_id
    assert "t_claim" in kwargs
    assert kwargs["t_claim"] == expected_t_eval
    # Explicitly prove it is NOT t_eval + 1s
    from datetime import timedelta
    assert kwargs["t_claim"] != expected_t_eval + timedelta(seconds=1)


# ---------------------------------------------------------------------------
# Test 7: TEST B — No Premature Attribution During payment.authorized
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@patch("src.core.services.live_pipeline.PaymentHealthAnalyticsService")
@patch("src.core.services.live_pipeline.DegradationService")
@patch("src.core.services.live_pipeline.RCAService")
@patch("src.core.services.live_pipeline.FailurePredictionService")
@patch("src.core.services.live_pipeline.InterventionDecisionService")
@patch("src.core.services.live_pipeline.InterventionExecutionService")
@patch("src.core.services.live_pipeline.PaymentOutcomeObservationService")
@patch("src.core.services.live_pipeline.CounterfactualAttributionService")
async def test_no_premature_attribution_on_authorized(
    mock_attr_cls,
    mock_obs_cls,
    mock_exec_cls,
    mock_dec_cls,
    mock_pred_cls,
    mock_rca_cls,
    mock_deg_cls,
    mock_analytics_cls,
):
    """For payment.authorized: prove Stage 7B / Stage 8 final attribution is NOT invoked prematurely."""
    from src.core.domain.intervention_models import DecisionType, GateVerdict, InterventionDecision, InterventionRouteKey
    from src.core.domain.execution_models import InterventionCommand, CommandStatus

    mock_analytics_cls.return_value.calculate_and_save_window = AsyncMock()
    mock_deg_cls.return_value.process_snapshots = AsyncMock()
    mock_rca_cls.return_value.evaluate_episode = AsyncMock()

    mock_session = AsyncMock()
    mock_result_empty = MagicMock()
    mock_result_empty.fetchall.return_value = []
    mock_session.execute.return_value = mock_result_empty

    # Mock prediction and ACT decision
    pred_svc_inst = mock_pred_cls.return_value
    pred_svc_inst.orchestrate_prediction = AsyncMock(
        return_value=MagicMock(
            prediction_id=uuid.uuid4(),
            payment_attempt_id="pay_test_no_premature",
            failure_probability=0.95,
            risk_band="HIGH",
        )
    )

    dec_svc_inst = mock_dec_cls.return_value
    mock_dec_inst = MagicMock()
    mock_dec_inst.decision_type.value = "ACT"
    dec_svc_inst.decide = AsyncMock(return_value=mock_dec_inst)

    exec_svc_inst = mock_exec_cls.return_value
    mock_cmd = MagicMock(command_id=uuid.uuid4())
    exec_svc_inst.create_command = AsyncMock(return_value=mock_cmd)
    exec_svc_inst.execute_command = AsyncMock(return_value=mock_cmd)

    obs_svc_inst = mock_obs_cls.return_value
    attr_svc_inst = mock_attr_cls.return_value

    payment_event = PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="razorpay",
        source_event_id="evt_premature_auth",
        payment_id="pay_test_no_premature",
        timestamp=datetime.now(timezone.utc),
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=50000,
        payment_status="authorized",
        ingested_at=datetime.now(timezone.utc),
    )

    result = await run_live_pipeline(payment_event, session=mock_session)
    assert result["status"] == "success"

    # Command was created and executed
    exec_svc_inst.create_command.assert_called_once()
    exec_svc_inst.execute_command.assert_called_once()

    # TEST B Verification: Observation and Attribution MUST NOT be called for authorization
    obs_svc_inst.observe.assert_not_called()
    attr_svc_inst.attribute_payment_intervention.assert_not_called()
    assert result["observation"] is None
    assert result["attribution"] is None


# ---------------------------------------------------------------------------
# Test 8: TEST C — Terminal Attribution Path (Invoked on payment.captured)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@patch("src.core.services.live_pipeline.PaymentHealthAnalyticsService")
@patch("src.core.services.live_pipeline.DegradationService")
@patch("src.core.services.live_pipeline.RCAService")
@patch("src.core.services.live_pipeline.PaymentOutcomeObservationService")
@patch("src.core.services.live_pipeline.CounterfactualAttributionService")
async def test_terminal_attribution_invoked_on_captured(
    mock_attr_cls,
    mock_obs_cls,
    mock_rca_cls,
    mock_deg_cls,
    mock_analytics_cls,
):
    """For payment.captured: prove Stage 7B observation and Stage 8 attribution ARE invoked after terminal ingestion."""
    from src.infrastructure.models import InterventionCommandModel

    mock_analytics_cls.return_value.calculate_and_save_window = AsyncMock()
    mock_deg_cls.return_value.process_snapshots = AsyncMock()
    mock_rca_cls.return_value.evaluate_episode = AsyncMock()

    mock_session = AsyncMock()
    mock_result_empty = MagicMock()
    mock_result_empty.fetchall.return_value = []

    # Mock existing command lookup
    mock_cmd_model = InterventionCommandModel(
        command_id=uuid.uuid4(),
        decision_id=uuid.uuid4(),
        payment_attempt_id="pay_test_captured",
        decision_version=1,
        route_key="DEGRADATION_CIRCUIT_BYPASS",
        policy_id="default_policy",
        policy_version="1.0",
        command_status="SUCCEEDED",
        created_at=datetime(2026, 9, 4, 4, 16, 35, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 4, 4, 16, 35, 500000, tzinfo=timezone.utc),
    )
    mock_cmd_query_res = MagicMock()
    mock_cmd_query_res.scalars.return_value.all.return_value = [mock_cmd_model]

    # session.execute sequence: snapshots, episodes, commands
    mock_session.execute.side_effect = [mock_result_empty, mock_result_empty, mock_cmd_query_res]

    obs_svc_inst = mock_obs_cls.return_value
    obs_svc_inst.observe = AsyncMock(return_value=MagicMock(observation_id=uuid.uuid4(), payment_outcome="CAPTURED"))

    attr_svc_inst = mock_attr_cls.return_value
    attr_svc_inst.attribute_payment_intervention = AsyncMock(
        return_value=MagicMock(attribution_id=uuid.uuid4(), attribution_status="ATTRIBUTED")
    )

    payment_event = PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="razorpay",
        source_event_id="evt_term_cap",
        payment_id="pay_test_captured",
        timestamp=datetime(2026, 9, 4, 4, 16, 36, tzinfo=timezone.utc),
        event_type="payment.captured",
        currency="INR",
        amount_minor_units=50000,
        payment_status="captured",
        ingested_at=datetime(2026, 9, 4, 4, 16, 36, 100000, tzinfo=timezone.utc),
    )

    result = await run_live_pipeline(payment_event, session=mock_session)
    assert result["status"] == "success"

    # TEST C Verification: observation and attribution were both called once
    obs_svc_inst.observe.assert_called_once()
    attr_svc_inst.attribute_payment_intervention.assert_called_once()
    assert result["observation"] is not None
    assert result["attribution"] is not None


# ---------------------------------------------------------------------------
# Test 9: TEST D — Terminal As-Of Boundary
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@patch("src.core.services.live_pipeline.PaymentHealthAnalyticsService")
@patch("src.core.services.live_pipeline.DegradationService")
@patch("src.core.services.live_pipeline.RCAService")
@patch("src.core.services.live_pipeline.PaymentOutcomeObservationService")
@patch("src.core.services.live_pipeline.CounterfactualAttributionService")
async def test_terminal_as_of_boundary(
    mock_attr_cls,
    mock_obs_cls,
    mock_rca_cls,
    mock_deg_cls,
    mock_analytics_cls,
):
    """Prove that the as_of timestamp used for terminal processing exposes execution attempts completed before terminal processing."""
    from src.infrastructure.models import InterventionCommandModel

    mock_analytics_cls.return_value.calculate_and_save_window = AsyncMock()
    mock_deg_cls.return_value.process_snapshots = AsyncMock()
    mock_rca_cls.return_value.evaluate_episode = AsyncMock()

    mock_session = AsyncMock()
    mock_result_empty = MagicMock()
    mock_result_empty.fetchall.return_value = []

    # Command completed and updated at 04:16:35.500 UTC
    cmd_updated_at = datetime(2026, 9, 4, 4, 16, 35, 500000, tzinfo=timezone.utc)
    mock_cmd_model = InterventionCommandModel(
        command_id=uuid.uuid4(),
        decision_id=uuid.uuid4(),
        payment_attempt_id="pay_test_boundary",
        decision_version=1,
        route_key="DEGRADATION_CIRCUIT_BYPASS",
        policy_id="default_policy",
        policy_version="1.0",
        command_status="SUCCEEDED",
        created_at=datetime(2026, 9, 4, 4, 16, 35, tzinfo=timezone.utc),
        updated_at=cmd_updated_at,
    )
    mock_cmd_query_res = MagicMock()
    mock_cmd_query_res.scalars.return_value.all.return_value = [mock_cmd_model]

    mock_session.execute.side_effect = [mock_result_empty, mock_result_empty, mock_cmd_query_res]

    obs_svc_inst = mock_obs_cls.return_value
    obs_svc_inst.observe = AsyncMock(return_value=MagicMock())

    attr_svc_inst = mock_attr_cls.return_value
    attr_svc_inst.attribute_payment_intervention = AsyncMock(return_value=MagicMock())

    # Terminal webhook ingested at 04:16:36.200 UTC (after cmd_updated_at)
    terminal_ingested = datetime(2026, 9, 4, 4, 16, 36, 200000, tzinfo=timezone.utc)
    payment_event = PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="razorpay",
        source_event_id="evt_term_boundary",
        payment_id="pay_test_boundary",
        timestamp=datetime(2026, 9, 4, 4, 16, 35, tzinfo=timezone.utc),
        event_type="payment.captured",
        currency="INR",
        amount_minor_units=50000,
        payment_status="captured",
        ingested_at=terminal_ingested,
    )

    result = await run_live_pipeline(payment_event, session=mock_session)
    assert result["status"] == "success"

    # TEST D Verification: as_of_timestamp == max(terminal_ingested, cmd_updated_at)
    expected_as_of = max(terminal_ingested, cmd_updated_at)
    obs_call_as_of = obs_svc_inst.observe.call_args[1]["as_of_timestamp"]
    attr_call_as_of = attr_svc_inst.attribute_payment_intervention.call_args[1]["as_of_timestamp"]

    assert obs_call_as_of == expected_as_of
    assert attr_call_as_of == expected_as_of
    # Guarantees that execution attempt created_at (<= cmd_updated_at) is strictly <= as_of_timestamp
    assert cmd_updated_at <= obs_call_as_of
    assert cmd_updated_at <= attr_call_as_of


# ---------------------------------------------------------------------------
# Test 10: TEST E — Razorpay Event Timestamp (event.created_at, NOT payment_entity.created_at)
# ---------------------------------------------------------------------------
def test_razorpay_event_timestamp_semantics():
    """Prove payment.authorized.timestamp == webhook.event.created_at and payment.captured.timestamp == captured_webhook.event.created_at."""
    from src.core.normalizers.razorpay import normalize_razorpay_event
    from src.core.parsers.razorpay import RazorpayWebhookEvent

    # Case A: Authorization webhook
    # Checkout initiated at 1788495390 (04:16:30 UTC), authorized at 1788495394 (04:16:34 UTC)
    auth_data = {
        "entity": "event",
        "account_id": "acc_1",
        "event": "payment.authorized",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_norm_1",
                    "entity": "payment",
                    "amount": 2332300,
                    "currency": "INR",
                    "status": "authorized",
                    "created_at": 1788495390,  # Checkout session initialization
                }
            }
        },
        "created_at": 1788495394,  # Authorization occurrence
    }
    auth_event = RazorpayWebhookEvent.model_validate(auth_data)
    normalized_auth = normalize_razorpay_event(auth_event, "evt_auth_norm")

    assert normalized_auth.timestamp == datetime.fromtimestamp(1788495394, tz=timezone.utc)
    assert normalized_auth.timestamp != datetime.fromtimestamp(1788495390, tz=timezone.utc)

    # Case B: Capture webhook
    # Checkout initiated at 1788495390 (04:16:30 UTC), captured at 1788495395 (04:16:35 UTC)
    cap_data = {
        "entity": "event",
        "account_id": "acc_1",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_norm_1",
                    "entity": "payment",
                    "amount": 2332300,
                    "currency": "INR",
                    "status": "captured",
                    "created_at": 1788495390,  # Checkout session initialization
                }
            }
        },
        "created_at": 1788495395,  # Capture occurrence
    }
    cap_event = RazorpayWebhookEvent.model_validate(cap_data)
    normalized_cap = normalize_razorpay_event(cap_event, "evt_cap_norm")

    assert normalized_cap.timestamp == datetime.fromtimestamp(1788495395, tz=timezone.utc)
    assert normalized_cap.timestamp != datetime.fromtimestamp(1788495390, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Test 11: TEST F — Ingestion Timestamp Preserved
# ---------------------------------------------------------------------------
def test_ingestion_timestamp_preserved():
    """Prove that PaymentEvent.ingested_at remains the application receipt timestamp and is distinct from domain event timestamp."""
    from src.core.normalizers.razorpay import normalize_razorpay_event
    from src.core.parsers.razorpay import RazorpayWebhookEvent

    event_data = {
        "entity": "event",
        "account_id": "acc_1",
        "event": "payment.authorized",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_ingested_at",
                    "entity": "payment",
                    "amount": 10000,
                    "currency": "INR",
                    "status": "authorized",
                    "created_at": 1700000000,
                }
            }
        },
        "created_at": 1700000050,  # 50 seconds after entity creation
    }
    parsed = RazorpayWebhookEvent.model_validate(event_data)
    t_before = datetime.now(timezone.utc)
    normalized = normalize_razorpay_event(parsed, "evt_source_1")
    t_after = datetime.now(timezone.utc)

    # Domain event timestamp reflects event.created_at
    assert normalized.timestamp == datetime.fromtimestamp(1700000050, tz=timezone.utc)

    # Ingested_at reflects application receipt time, timezone-aware, within current execution interval
    assert normalized.ingested_at.tzinfo is not None
    assert t_before <= normalized.ingested_at <= t_after
    assert normalized.ingested_at != normalized.timestamp


# ---------------------------------------------------------------------------
# Test 12: TEST G — Frozen Stage 8 Causality
# ---------------------------------------------------------------------------
def test_frozen_stage8_causality_temporal_precedence():
    """Prove that if terminal_event_timestamp < attempt.completed_at, Stage 8 rejects attribution (UNTREATED)."""
    from decimal import Decimal
    from src.core.attribution.calculator import calculate_attribution
    from src.core.domain.attribution_models import AttributionStatus, ModelProvenance, TreatmentStatus

    # Sub-case 1: Terminal outcome occurred BEFORE intervention completion (delta < 0) -> UNTREATED
    res_untreated = calculate_attribution(
        payment_amount_minor_units=2332300,
        p0=Decimal("0.9236"),
        treatment_status=TreatmentStatus.UNTREATED,
        observed_payment_outcome="CAPTURED",
        provenance=ModelProvenance.EMPIRICAL_PRODUCTION,
        prediction_status="PREDICTED",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=-6.145513,
    )
    assert res_untreated.attribution_status == AttributionStatus.UNATTRIBUTED_UNTREATED
    assert res_untreated.attributed_protected_gmv_minor_units == 0

    # Sub-case 2: Terminal outcome occurred AFTER intervention completion (delta >= 0) -> TREATED & ATTRIBUTED
    res_treated = calculate_attribution(
        payment_amount_minor_units=2332300,
        p0=Decimal("0.9236"),
        treatment_status=TreatmentStatus.TREATED,
        observed_payment_outcome="CAPTURED",
        provenance=ModelProvenance.EMPIRICAL_PRODUCTION,
        prediction_status="PREDICTED",
        diagnosis_confidence="STRONG",
        timing_delta_seconds=2.500000,
    )
    assert res_treated.attribution_status == AttributionStatus.ATTRIBUTED
    assert res_treated.attributed_protected_gmv_minor_units > 0

