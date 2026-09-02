"""
Stage 6 — Intervention Decisioning domain models.

Deterministically decides among NO_ACTION, MONITOR, and ACT.
Selects exactly one permitted intervention route when ACT.
Decisioning only: does not execute interventions or evaluate financial outcomes.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional


# Deterministic namespace for Stage 6 UUID5 identity
NAMESPACE_STAGE6: uuid.UUID = uuid.UUID("e5f6a7b8-c9d0-1234-ef01-567890abcdef")


class DecisionType(str, Enum):
    NO_ACTION = "NO_ACTION"
    MONITOR = "MONITOR"
    ACT = "ACT"


class GateVerdict(str, Enum):
    PASSED = "PASSED"
    FAILED_KILL_SWITCH = "FAILED_KILL_SWITCH"
    FAILED_STAGE5_UNAVAILABLE = "FAILED_STAGE5_UNAVAILABLE"
    FAILED_STAGE5_INELIGIBLE = "FAILED_STAGE5_INELIGIBLE"
    FAILED_TERMINAL_OUTCOME_INGESTED = "FAILED_TERMINAL_OUTCOME_INGESTED"
    FAILED_EPISODE_INVALIDATED = "FAILED_EPISODE_INVALIDATED"
    FAILED_UNRELIABLE_DIAGNOSIS = "FAILED_UNRELIABLE_DIAGNOSIS"
    FAILED_GLOBAL_RATE_LIMIT = "FAILED_GLOBAL_RATE_LIMIT"
    FAILED_ROUTE_COOLDOWN = "FAILED_ROUTE_COOLDOWN"
    FAILED_INVALID_AMOUNT = "FAILED_INVALID_AMOUNT"
    FAILED_NO_ELIGIBLE_ROUTES = "FAILED_NO_ELIGIBLE_ROUTES"


class InterventionRouteKey(str, Enum):
    """
    Statically permitted intervention routes.
    Stage 6 NEVER discovers routes dynamically.
    """
    RETRY_SECONDARY_GATEWAY = "RETRY_SECONDARY_GATEWAY"
    PROMPT_PAYMENT_METHOD_SWITCH = "PROMPT_PAYMENT_METHOD_SWITCH"
    DYNAMIC_RETRY_BACKOFF = "DYNAMIC_RETRY_BACKOFF"
    DEGRADATION_CIRCUIT_BYPASS = "DEGRADATION_CIRCUIT_BYPASS"
    FALLBACK_PAYMENT_LINK = "FALLBACK_PAYMENT_LINK"


@dataclass(frozen=True)
class CandidateRouteScore:
    """
    Evaluation score for a candidate intervention route.
    Uses Decimal/integer minor units to preserve strict monetary invariants.
    """
    route_key: InterventionRouteKey
    is_eligible: bool
    eligibility_reason: str
    expected_recovery_benefit: Decimal
    policy_cost_minor_units: int
    policy_utility: Decimal
    route_alignment_score: float
    rank: int


@dataclass(frozen=True)
class GateEvaluationResult:
    """
    Result of evaluating a hard safety gate.
    """
    passed: bool
    verdict: GateVerdict
    details: dict[str, Any]


@dataclass(frozen=True)
class InterventionDecision:
    """
    Immutable, versioned intervention decision for a payment attempt.
    Append-only — never updated or deleted after insertion.
    """
    decision_id: uuid.UUID
    payment_attempt_id: str
    decision_version: int
    decided_at: datetime
    decision_type: DecisionType
    selected_route_id: Optional[InterventionRouteKey]
    failure_probability: Optional[float]
    stage5_prediction_id: Optional[uuid.UUID]
    diagnosis_confidence: Optional[str]
    decision_confidence: float
    gate_verdict: GateVerdict
    policy_id: str
    policy_version: str
    input_fingerprint: str
    evaluation_audit_payload: dict[str, Any]
    created_at: datetime


def make_decision_id(payment_attempt_id: str, decision_version: int) -> uuid.UUID:
    """
    Deterministic UUID5 for an intervention decision.
    """
    return uuid.uuid5(NAMESPACE_STAGE6, f"{payment_attempt_id}:{decision_version}")


def compute_decision_fingerprint(
    payment_attempt_id: str,
    t_decide: datetime,
    amount_minor_units: int,
    currency: str,
    stage5_prediction_id: Optional[uuid.UUID],
    failure_probability: Optional[float],
    prediction_status: str,
    episode_id: Optional[uuid.UUID],
    episode_status: Optional[str],
    rca_classification: Optional[str],
    rca_candidate_dimension: Optional[str],
    rca_candidate_value: Optional[str],
    rca_evidence_strength: Optional[str],
    policy_id: str,
    policy_version: str,
) -> str:
    """
    Deterministic SHA-256 fingerprint of the decision input state as-of t_decide.
    Evaluation identity = payment attempt + t_decide + upstream state + policy version.
    """
    fingerprint_inputs = {
        "payment_attempt_id": str(payment_attempt_id),
        "t_decide": t_decide.isoformat(),
        "amount_minor_units": int(amount_minor_units),
        "currency": str(currency),
        "stage5_prediction_id": str(stage5_prediction_id) if stage5_prediction_id else None,
        "failure_probability": float(failure_probability) if failure_probability is not None else None,
        "prediction_status": str(prediction_status),
        "episode_id": str(episode_id) if episode_id else None,
        "episode_status": str(episode_status) if episode_status else None,
        "rca_classification": str(rca_classification) if rca_classification else None,
        "rca_candidate_dimension": str(rca_candidate_dimension) if rca_candidate_dimension else None,
        "rca_candidate_value": str(rca_candidate_value) if rca_candidate_value else None,
        "rca_evidence_strength": str(rca_evidence_strength) if rca_evidence_strength else None,
        "policy_id": str(policy_id),
        "policy_version": str(policy_version),
    }

    raw = json.dumps(fingerprint_inputs, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
