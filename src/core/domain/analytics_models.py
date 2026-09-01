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

    insufficient_volume: bool

    calculated_at: datetime
