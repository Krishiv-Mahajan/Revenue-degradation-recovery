import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from src.infrastructure.models import PaymentEventModel
from src.core.services.failure_prediction_service import FailurePredictionService
from src.core.services.feature_reconstruction_service import FeatureReconstructionService
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.infrastructure.failure_prediction_repository import FailurePredictionRepository
from src.core.ml.model import DeterministicBaselineModel


@pytest.mark.asyncio
async def test_temporal_eligibility_late_event():
    """
    Test that a late-arriving terminal event (timestamp < T, but ingested_at > T)
    does NOT retroactively disqualify an authorized event at T.
    """
    db_session = AsyncMock()
    
    # Mock result for is_eligible_for_prediction
    # The SQL query filters ingested_at < T. A late event has ingested_at > T,
    # so the EXISTS query should return False.
    mock_result = MagicMock()
    mock_result.scalar.return_value = False
    db_session.execute.return_value = mock_result
    
    repo = FeatureReconstructionRepository(db_session)
    feat_svc = FeatureReconstructionService(repo)
    pred_repo = FailurePredictionRepository(db_session)
    model = DeterministicBaselineModel()
    svc = FailurePredictionService(db_session, feat_svc, pred_repo, model)
    payment_id = "pay_temporal_1"
    
    T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    
    # Because it was ingested after T (and the mock reflects the DB filtering it out),
    # the payment was eligible at time T
    is_eligible = await svc.is_eligible_for_prediction(payment_id, T)
    assert is_eligible is True

@pytest.mark.asyncio
async def test_temporal_label_lookup_excludes_future_horizon():
    """
    Label lookup should not see terminal events that arrive after the observation window.
    """
    db_session = AsyncMock()
    
    repo = FeatureReconstructionRepository(db_session)
    feat_svc = FeatureReconstructionService(repo)
    pred_repo = FailurePredictionRepository(db_session)
    model = DeterministicBaselineModel()
    svc = FailurePredictionService(db_session, feat_svc, pred_repo, model)
    payment_id = "pay_temporal_2"
    
    T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    
    # Terminal event arrives at T + 35m
    T_failed = T + timedelta(minutes=35)
    
    # Mocking two distinct queries based on the observation horizon
    mock_result_30 = MagicMock()
    mock_result_30.scalar_one_or_none.return_value = None
    
    mock_result_60 = MagicMock()
    failed_event = PaymentEventModel(
        event_id=uuid.uuid4(),
        source_system="test_system",
        source_event_id=str(uuid.uuid4()),
        payment_id=payment_id,
        timestamp=T_failed,
        event_type="payment.failed",
        currency="INR",
        amount_minor_units=1000,
        payment_status="processed",
        ingested_at=T_failed
    )
    mock_result_60.scalar_one_or_none.return_value = failed_event
    
    db_session.execute.side_effect = [mock_result_30, mock_result_60]
    
    # With a 30m horizon, this terminal event is excluded (mock returns None)
    label = await svc.lookup_terminal_label(payment_id, T, observation_window_minutes=30)
    print(f"DEBUG LABEL 30: {label}")
    print(f"CALL LIST: {db_session.execute.call_args_list}")
    assert label is None
    
    # With a 60m horizon, it is included (mock returns failed_event)
    label_60 = await svc.lookup_terminal_label(payment_id, T, observation_window_minutes=60)
    assert label_60 == 1
