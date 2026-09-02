"""
Stage 5 — Failure Prediction Features.
"""
from dataclasses import dataclass, asdict
from typing import Optional

@dataclass(frozen=True)
class FeatureSnapshot:
    """
    Strictly typed immutable feature snapshot for a payment attempt as of T.
    Contains pre-terminal Stage 1 fields, Stage 1 behavioral aggregations,
    Stage 3 degradation context, and Stage 4 RCA context.
    """
    
    # Pre-terminal Stage 1 features
    payment_method: Optional[str]
    bank: Optional[str]
    wallet: Optional[str]
    currency: str
    amount_minor_units: int
    hour_of_day: int
    day_of_week: int
    
    # Stage 1 Behavioral features
    # Format: [dimension]_[horizon]_failure_rate
    global_30m_failure_rate: Optional[float]
    global_24h_failure_rate: Optional[float]
    payment_method_30m_failure_rate: Optional[float]
    payment_method_24h_failure_rate: Optional[float]
    bank_30m_failure_rate: Optional[float]
    bank_24h_failure_rate: Optional[float]
    wallet_30m_failure_rate: Optional[float]
    wallet_24h_failure_rate: Optional[float]
    currency_30m_failure_rate: Optional[float]
    currency_24h_failure_rate: Optional[float]

    # Sufficiency flags
    insufficient_global_volume: bool
    
    # Stage 3 Degradation features
    is_in_active_degradation: bool
    degradation_severity: Optional[str] # e.g. MODERATE, HIGH, CRITICAL, None if not active
    
    # Stage 4 RCA features
    rca_classification: Optional[str] # SYSTEMIC, SEGMENT_SPECIFIC, UNKNOWN, None
    rca_candidate_dimension: Optional[str]
    rca_candidate_value: Optional[str]
    rca_evidence_strength: Optional[str]
    rca_excess_failure_contribution: Optional[float]
    rca_candidate_rank: Optional[int]
    rca_candidate_matches_payment_segment: Optional[bool]
    
    def to_dict(self) -> dict:
        return asdict(self)
