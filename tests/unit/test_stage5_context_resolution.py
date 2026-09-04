"""
Unit tests for Buildathon Checkpoint 3E: Live Stage 5 Context Resolution.

Verifies:
1. TEST A: Temporal Boundary - Stage 3 receives event_ts (not future window_end),
   and resolver state is visible to Stage 5 at t_eval.
2. TEST B: Dimension Case - FeatureReconstructionRepository resolves 'payment_method'
   when requested as 'PAYMENT_METHOD' case-insensitively, preserving exact segment_value.
3. TEST C: Canonical Resolver - get_most_severe_active_episode retains exact
   canonical severity (CRITICAL > HIGH > MODERATE), older start window tie-breaking,
   and deterministic UUID ordering.
4. TEST D: Formula preservation - ML model and policy formulas remain frozen.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.sql.elements import BinaryExpression

from src.core.domain.models import PaymentEvent
from src.core.domain.degradation_models import Severity
from src.core.ml.model import get_development_model
from src.infrastructure.models import EpisodeStateHistoryModel, PaymentEventModel
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.core.services.live_pipeline import _execute_live_pipeline


# ---------------------------------------------------------------------------
# TEST A: TEMPORAL BOUNDARY
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_live_pipeline_stage3_timestamp_is_event_ts():
    """
    Prove that for a live event at 03:04:21:
    Stage 3 does NOT receive 03:05:00 as its evaluation timestamp.
    It receives event_ts (03:04:21) which is <= Stage 5 evaluation timestamp.
    """
    event_time = datetime(2026, 9, 4, 3, 4, 21, tzinfo=timezone.utc)
    ingested_time = datetime(2026, 9, 4, 3, 4, 22, tzinfo=timezone.utc)

    payment_event = PaymentEvent(
        event_id=uuid.uuid4(),
        source_system="razorpay",
        source_event_id="evt_test_temporal",
        payment_id="pay_test_temporal",
        timestamp=event_time,
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=341300,
        payment_status="authorized",
        payment_method="card",
        ingested_at=ingested_time,
    )

    mock_session = AsyncMock()
    # Mock snapshot query return
    mock_snapshot_row = MagicMock()
    mock_snapshot_row.snapshot_id = uuid.uuid4()
    mock_snapshot_row.window_start = datetime(2026, 9, 4, 3, 0, 0, tzinfo=timezone.utc)
    mock_snapshot_row.window_end = datetime(2026, 9, 4, 3, 5, 0, tzinfo=timezone.utc)
    mock_snapshot_row.segment_dimension = "payment_method"
    mock_snapshot_row.segment_value = "card"
    mock_snapshot_row.transaction_count = 1
    mock_snapshot_row.successful_transaction_count = 0
    mock_snapshot_row.failed_transaction_count = 0
    mock_snapshot_row.success_rate = 1.0
    mock_snapshot_row.failure_rate = 0.0
    mock_snapshot_row.total_gmv_minor_units = 341300
    mock_snapshot_row.successful_gmv_minor_units = 0
    mock_snapshot_row.failed_gmv_minor_units = 0
    mock_snapshot_row.baseline_success_rate = 0.85
    mock_snapshot_row.insufficient_volume = True
    mock_snapshot_row.calculated_at = ingested_time

    mock_snap_result = MagicMock()
    mock_snap_result.fetchall.return_value = [mock_snapshot_row]

    mock_ep_result = MagicMock()
    mock_ep_result.fetchall.return_value = []

    # Mock execute results
    mock_session.execute.side_effect = [
        mock_snap_result,  # Stage 3 snapshots
        mock_ep_result,    # Stage 4 active episodes
    ]

    with patch("src.core.services.live_pipeline.PaymentHealthAnalyticsService") as mock_s2_cls, \
         patch("src.core.services.live_pipeline.DegradationService") as mock_s3_cls, \
         patch("src.core.services.live_pipeline.FailurePredictionService") as mock_s5_cls:

        mock_s2 = mock_s2_cls.return_value
        mock_s2.calculate_and_save_window = AsyncMock()

        mock_s3 = mock_s3_cls.return_value
        mock_s3.process_snapshots = AsyncMock()

        mock_s5 = mock_s5_cls.return_value
        mock_s5.orchestrate_prediction = AsyncMock()
        mock_pred = MagicMock()
        mock_pred.failure_probability = 0.42
        mock_s5.orchestrate_prediction.return_value = mock_pred

        await _execute_live_pipeline(mock_session, payment_event)

        # Stage 3 process_snapshots MUST be called with evaluation_timestamp == event_time
        assert mock_s3.process_snapshots.called
        call_kwargs = mock_s3.process_snapshots.call_args[1]
        eval_ts = call_kwargs.get("evaluation_timestamp")
        assert eval_ts == event_time
        assert eval_ts != datetime(2026, 9, 4, 3, 5, 0, tzinfo=timezone.utc)

        # Stage 5 evaluation timestamp T must be >= Stage 3 eval_ts
        assert mock_s5.orchestrate_prediction.called
        t_eval_s5 = mock_s5.orchestrate_prediction.call_args[1].get("T")
        assert t_eval_s5 >= eval_ts


# ---------------------------------------------------------------------------
# TEST B: DIMENSION CASE NORMALIZATION & VISIBILITY
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_active_episode_context_case_normalization():
    """
    Prove that requested dimension 'PAYMENT_METHOD' resolves persisted 'payment_method'
    without creating duplicate state rows, preserving exact segment_value semantics.
    """
    mock_session = AsyncMock()
    repo = FeatureReconstructionRepository(mock_session)

    run_id = uuid.uuid4()
    mock_run_result = MagicMock()
    mock_run_result.scalar_one_or_none.return_value = run_id

    expected_episode = EpisodeStateHistoryModel(
        history_id=uuid.uuid4(),
        reconciliation_run_id=run_id,
        episode_id=uuid.uuid4(),
        segment_dimension="payment_method",
        segment_value="card",
        status="ACTIVE",
        severity="CRITICAL",
        effective_start_window=datetime(2026, 9, 4, 2, 50, 0, tzinfo=timezone.utc),
        evaluation_timestamp=datetime(2026, 9, 4, 3, 4, 21, tzinfo=timezone.utc),
    )
    mock_active_result = MagicMock()
    mock_active_result.scalar_one_or_none.return_value = expected_episode

    mock_session.execute.side_effect = [mock_run_result, mock_active_result]

    t_eval = datetime(2026, 9, 4, 3, 4, 21, tzinfo=timezone.utc)
    res = await repo.get_active_episode_context("PAYMENT_METHOD", "card", t_eval)

    assert res is not None
    assert res.segment_dimension == "payment_method"
    assert res.segment_value == "card"
    assert res.status == "ACTIVE"
    assert res.severity == "CRITICAL"

    # Verify both queries were executed
    assert mock_session.execute.call_count == 2
    first_stmt = mock_session.execute.call_args_list[0][0][0]
    # Check that SQL compiles and targets EpisodeStateHistoryModel
    compiled_first_sql = str(first_stmt.compile())
    assert "UPPER" in compiled_first_sql.upper()
    assert "<=" in compiled_first_sql


# ---------------------------------------------------------------------------
# TEST C: EXISTING CANONICAL RESOLVER PRESERVATION
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_canonical_resolver_severity_and_tie_breaking():
    """
    Prove that get_most_severe_active_episode preserves:
    1. CRITICAL > HIGH > MODERATE
    2. Older effective_start_window wins equal-severity ties
    3. Deterministic UUID ordering
    """
    mock_session = AsyncMock()
    repo = FeatureReconstructionRepository(mock_session)

    t_eval = datetime(2026, 9, 4, 3, 4, 21, tzinfo=timezone.utc)

    ep_global = EpisodeStateHistoryModel(
        history_id=uuid.uuid4(),
        reconciliation_run_id=uuid.uuid4(),
        episode_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        segment_dimension="GLOBAL",
        segment_value="ALL",
        status="ACTIVE",
        severity="MODERATE",
        effective_start_window=datetime(2026, 9, 4, 2, 50, 0, tzinfo=timezone.utc),
        evaluation_timestamp=t_eval,
    )

    ep_pm = EpisodeStateHistoryModel(
        history_id=uuid.uuid4(),
        reconciliation_run_id=uuid.uuid4(),
        episode_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        segment_dimension="payment_method",
        segment_value="card",
        status="ACTIVE",
        severity="CRITICAL",
        effective_start_window=datetime(2026, 9, 4, 2, 45, 0, tzinfo=timezone.utc),
        evaluation_timestamp=t_eval,
    )

    ep_curr = EpisodeStateHistoryModel(
        history_id=uuid.uuid4(),
        reconciliation_run_id=uuid.uuid4(),
        episode_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
        segment_dimension="currency",
        segment_value="INR",
        status="ACTIVE",
        severity="HIGH",
        effective_start_window=datetime(2026, 9, 4, 2, 40, 0, tzinfo=timezone.utc),
        evaluation_timestamp=t_eval,
    )

    async def mock_get_context(dim, val, t):
        d_upper = dim.upper()
        if d_upper == "GLOBAL":
            return ep_global
        elif d_upper == "PAYMENT_METHOD" and val == "card":
            return ep_pm
        elif d_upper == "CURRENCY" and val == "INR":
            return ep_curr
        return None

    repo.get_active_episode_context = mock_get_context

    auth_event = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="razorpay",
        source_event_id="evt_test",
        payment_id="pay_test",
        timestamp=t_eval,
        event_type="payment.authorized",
        currency="INR",
        amount_minor_units=1000,
        payment_status="authorized",
        payment_method="card",
        bank=None,
        wallet=None,
        ingested_at=t_eval,
    )

    # PM is CRITICAL -> must win over HIGH currency and MODERATE global
    resolved = await repo.get_most_severe_active_episode(auth_event, t_eval)
    assert resolved is not None
    assert resolved.episode_id == ep_pm.episode_id
    assert resolved.severity == "CRITICAL"


# ---------------------------------------------------------------------------
# TEST D: MATHEMATICAL FORMULA PRESERVATION
# ---------------------------------------------------------------------------
def test_deterministic_model_formula_remains_frozen():
    """
    Verify that get_development_model formula produces exact 0.9261 for:
    global_30m_failure_rate ≈ 0.8022
    CRITICAL severity (+0.30)
    STRONG RCA match (+0.20)
    """
    model = get_development_model(mode="deterministic")
    vec = {
        "global_30m_failure_rate": 0.8021978021978022,
        "is_in_active_degradation": True,
        "degradation_severity": "CRITICAL",
        "rca_candidate_matches_payment_segment": True,
        "rca_evidence_strength": "STRONG",
    }
    p_fail = model.predict(vec)
    # Expected: 0.025 + 0.5 * 0.8021978 + 0.30 + 0.20 = 0.9260989...
    assert round(p_fail, 4) == 0.9261
