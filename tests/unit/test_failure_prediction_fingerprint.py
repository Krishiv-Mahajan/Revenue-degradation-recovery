import pytest
from datetime import datetime, timezone
from src.core.domain.failure_prediction_models import compute_input_fingerprint

def test_fingerprint_format():
    """Test proving the fingerprint has the required sha256: format."""
    fp = compute_input_fingerprint(
        payment_attempt_id="pay_123",
        predicted_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        prediction_horizon="30m",
        feature_snapshot={"feature1": "value1"},
        model_name="test_model",
        model_version="1.0.0",
        feature_schema_version="1.0"
    )
    assert fp.startswith("sha256:")
    assert len(fp) == 7 + 64  # "sha256:" + 64-char hex digest
    
    hex_part = fp[7:]
    assert all(c in "0123456789abcdef" for c in hex_part)

def test_fingerprint_determinism():
    """Verify identical canonical inputs produce the identical fingerprint."""
    dt = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    
    # Snapshot dicts with different key insertion order but identical data
    snapshot_1 = {"b": 2, "a": 1, "c": 3}
    snapshot_2 = {"a": 1, "c": 3, "b": 2}
    
    fp1 = compute_input_fingerprint(
        payment_attempt_id="pay_123",
        predicted_at=dt,
        prediction_horizon="30m",
        feature_snapshot=snapshot_1,
        model_name="test_model",
        model_version="1.0.0",
        feature_schema_version="1.0"
    )
    
    fp2 = compute_input_fingerprint(
        payment_attempt_id="pay_123",
        predicted_at=dt,
        prediction_horizon="30m",
        feature_snapshot=snapshot_2,
        model_name="test_model",
        model_version="1.0.0",
        feature_schema_version="1.0"
    )
    
    assert fp1 == fp2

def test_fingerprint_changes_on_input_change():
    """Verify changing any fingerprint input changes the fingerprint."""
    dt = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    base_kwargs = {
        "payment_attempt_id": "pay_123",
        "predicted_at": dt,
        "prediction_horizon": "30m",
        "feature_snapshot": {"a": 1},
        "model_name": "test_model",
        "model_version": "1.0.0",
        "feature_schema_version": "1.0"
    }
    
    fp_base = compute_input_fingerprint(**base_kwargs)
    
    # Change payment_attempt_id
    kwargs_mod1 = base_kwargs.copy()
    kwargs_mod1["payment_attempt_id"] = "pay_456"
    assert compute_input_fingerprint(**kwargs_mod1) != fp_base
    
    # Change predicted_at
    kwargs_mod2 = base_kwargs.copy()
    kwargs_mod2["predicted_at"] = datetime(2026, 9, 1, 12, 1, tzinfo=timezone.utc)
    assert compute_input_fingerprint(**kwargs_mod2) != fp_base
    
    # Change prediction_horizon
    kwargs_mod3 = base_kwargs.copy()
    kwargs_mod3["prediction_horizon"] = "60m"
    assert compute_input_fingerprint(**kwargs_mod3) != fp_base
    
    # Change feature_snapshot
    kwargs_mod4 = base_kwargs.copy()
    kwargs_mod4["feature_snapshot"] = {"a": 2}
    assert compute_input_fingerprint(**kwargs_mod4) != fp_base
    
    # Change model_name
    kwargs_mod5 = base_kwargs.copy()
    kwargs_mod5["model_name"] = "test_model_v2"
    assert compute_input_fingerprint(**kwargs_mod5) != fp_base
    
    # Change model_version
    kwargs_mod6 = base_kwargs.copy()
    kwargs_mod6["model_version"] = "1.0.1"
    assert compute_input_fingerprint(**kwargs_mod6) != fp_base
    
    # Change feature_schema_version
    kwargs_mod7 = base_kwargs.copy()
    kwargs_mod7["feature_schema_version"] = "1.1"
    assert compute_input_fingerprint(**kwargs_mod7) != fp_base
