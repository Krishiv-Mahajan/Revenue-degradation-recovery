"""
Stage 8 domain models for counterfactual attribution and protected GMV.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional

NAMESPACE_STAGE8_ATTRIBUTION = uuid.uuid5(uuid.NAMESPACE_DNS, "attribution.recovery.engine")


def make_attribution_id(command_id: uuid.UUID, attribution_version: int) -> uuid.UUID:
    """Generate a deterministic UUID5 for a counterfactual attribution record."""
    return uuid.uuid5(NAMESPACE_STAGE8_ATTRIBUTION, f"attr:{command_id}:{attribution_version}")


class AttributionStatus(str, Enum):
    """Authoritative lifecycle status of a counterfactual attribution record."""
    ATTRIBUTED = "ATTRIBUTED"
    UNATTRIBUTED_PAYMENT_FAILED = "UNATTRIBUTED_PAYMENT_FAILED"
    UNATTRIBUTED_EXECUTION_FAILED = "UNATTRIBUTED_EXECUTION_FAILED"
    UNATTRIBUTED_UNTREATED = "UNATTRIBUTED_UNTREATED"
    UNRESOLVED_IN_FLIGHT = "UNRESOLVED_IN_FLIGHT"
    INDETERMINATE_INSUFFICIENT_EVIDENCE = "INDETERMINATE_INSUFFICIENT_EVIDENCE"


class TreatmentStatus(str, Enum):
    """Authoritative categorization of intervention treatment delivery."""
    TREATED = "TREATED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    UNTREATED = "UNTREATED"


class ModelProvenance(str, Enum):
    """Provenance class of the failure prediction model used for counterfactual risk proxy."""
    EMPIRICAL_PRODUCTION = "EMPIRICAL_PRODUCTION"
    SYNTHETIC_DEVELOPMENT = "SYNTHETIC_DEVELOPMENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CounterfactualAttribution:
    """
    Append-only factual and risk-weighted counterfactual attribution record.
    Strictly preserves integer minor-unit money and Decimal fixed-point confidence/probability.
    """
    attribution_id: uuid.UUID
    command_id: uuid.UUID
    payment_attempt_id: str
    decision_id: uuid.UUID
    attribution_version: int
    attributed_at: datetime
    as_of_timestamp: datetime
    attribution_status: AttributionStatus
    methodology_name: str
    methodology_version: str
    treatment_status: TreatmentStatus
    observed_payment_outcome: str
    payment_amount_minor_units: int
    counterfactual_failure_probability: Optional[Decimal]
    counterfactual_loss_exposure_minor_units: int
    counterfactual_natural_success_gmv_minor_units: int
    attributed_protected_gmv_minor_units: int
    attribution_confidence: Decimal
    is_synthetic_baseline: bool
    is_simulated_execution: bool
    attribution_audit_payload: Dict[str, Any]
    created_at: datetime
