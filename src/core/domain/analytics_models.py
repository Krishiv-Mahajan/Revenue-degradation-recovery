from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from typing import Optional

class PaymentHealthSnapshot(BaseModel):
    """
    Deterministic descriptive analytical snapshot of payment health.
    """
    model_config = ConfigDict(frozen=True)

    snapshot_id: UUID

    window_start: datetime
    window_end: datetime

    segment_dimension: str
    segment_value: str

    transaction_count: int
    successful_transaction_count: int
    failed_transaction_count: int

    success_rate: Optional[float] = None
    failure_rate: Optional[float] = None

    total_gmv_minor_units: int
    successful_gmv_minor_units: int
    failed_gmv_minor_units: int

    baseline_success_rate: Optional[float] = None
    # Spread of the same historical windows as the median baseline (for Stage 3 Z-score evidence).
    baseline_mean_success_rate: Optional[float] = None
    baseline_stddev_success_rate: Optional[float] = None
    baseline_sample_count: Optional[int] = None

    insufficient_volume: bool

    calculated_at: datetime

    # Stage 2-4 analytics scope (src/core/domain/analytics_scopes.py)
    provider_scope: str = "razorpay_pipeline"
