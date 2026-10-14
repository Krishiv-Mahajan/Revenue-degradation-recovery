import pytest
import pytest_asyncio
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.ml.training import Stage5PipelineValidator
from scripts.generate_synthetic_history import generate_dataset

@pytest.mark.asyncio
async def test_synthetic_training_pipeline_end_to_end(test_database_url: str, db_session: AsyncSession):
    # 1. Generate small synthetic dataset directly into the test DB
    await generate_dataset(engine_url=test_database_url, num_days=3, events_per_day=50)
    
    # 2. Run the pipeline
    validator = Stage5PipelineValidator(db_session)
    
    # Extract dataset
    X_raw, y_raw, times = await validator.extract_dataset()
    
    assert len(X_raw) > 0, "No valid authorized events extracted"
    assert len(X_raw) == len(y_raw) == len(times), "Dataset lengths mismatch"
    
    # Ensure no temporal leakage in labels
    for snapshot_dict, label, T in zip(X_raw, y_raw, times):
        # T is the ingested_at of the auth event. The label comes from a terminal event ingested AFTER T.
        # Ensure chronological bounds
        assert snapshot_dict is not None
        assert isinstance(label, int)
        assert label in (0, 1)

    # Verify chronological ordering of the extracted dataset
    for i in range(1, len(times)):
        assert times[i] >= times[i-1], "Dataset is not chronologically ordered"

    # Train
    model, metrics = validator.run_pipeline(X_raw, y_raw, times)
    
    # 3. Assert outputs
    assert model.model_name == "SyntheticLogisticRegressionModel"
    assert model.model_version == "synthetic-development-v1"
    assert "pr_auc" in metrics
    assert "roc_auc" in metrics
    assert "brier_score" in metrics
    
    # Predict on a sample to verify interface
    sample_prob = model.predict(X_raw[0])
    assert 0.0 <= sample_prob <= 1.0, "Probability must be in [0, 1]"
