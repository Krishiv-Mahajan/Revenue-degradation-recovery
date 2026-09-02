"""
Stage 6 — Intervention Decisioning repository.

APPEND-ONLY: exposes only INSERT operations for intervention decisions.
No UPDATE, no DELETE. Immutability is enforced here at the repository boundary.
"""
from __future__ import annotations

import struct
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.intervention_models import (
    DecisionType,
    GateVerdict,
    InterventionDecision,
    InterventionRouteKey,
)
from src.infrastructure.models import InterventionDecisionModel, PaymentEventModel

# Fixed global lock key pair for atomic Stage 6 ACT admission
GLOBAL_RATE_LIMIT_LOCK_K1 = 0x54673641  # 'St6A'
GLOBAL_RATE_LIMIT_LOCK_K2 = 0x52415445  # 'RATE'


class InterventionRepository:
    """
    Append-only repository for Stage 6 Intervention Decisions.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def acquire_version_allocation_lock(self, payment_attempt_id: str) -> None:
        """
        Acquire a transaction-level advisory lock per payment_attempt_id
        to serialize version allocation and avoid race conditions.
        """
        hash_uuid = uuid.uuid5(uuid.NAMESPACE_OID, payment_attempt_id)
        k1, k2 = struct.unpack("<ii", hash_uuid.bytes[:8])
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:k1, :k2)"), {"k1": k1, "k2": k2}
        )

    async def acquire_global_rate_limit_lock(self) -> None:
        """
        Acquire a dedicated global transaction-level advisory lock.
        Used to ensure that checking remaining ACT capacity and inserting
        an ACT decision are completely atomic across competing requests.
        """
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
            {"k1": GLOBAL_RATE_LIMIT_LOCK_K1, "k2": GLOBAL_RATE_LIMIT_LOCK_K2},
        )

    async def get_max_decision_version(self, payment_attempt_id: str) -> int:
        """
        Returns the current maximum decision_version for a payment attempt.
        Returns 0 if no decisions exist yet.
        """
        stmt = select(func.max(InterventionDecisionModel.decision_version)).where(
            InterventionDecisionModel.payment_attempt_id == payment_attempt_id
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def get_latest_decision(self, payment_attempt_id: str) -> Optional[InterventionDecision]:
        """
        Returns the latest (highest decision_version) decision for a payment attempt.
        """
        stmt = (
            select(InterventionDecisionModel)
            .where(InterventionDecisionModel.payment_attempt_id == payment_attempt_id)
            .order_by(InterventionDecisionModel.decision_version.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._map_decision(model)

    async def get_decision_by_fingerprint(self, input_fingerprint: str) -> Optional[InterventionDecision]:
        """
        Retrieves an intervention decision by its exact input fingerprint.
        """
        stmt = (
            select(InterventionDecisionModel)
            .where(InterventionDecisionModel.input_fingerprint == input_fingerprint)
            .limit(1)
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._map_decision(model)

    async def count_recent_act_decisions(self, since_timestamp: datetime) -> int:
        """
        Count the number of ACT decisions recorded system-wide since since_timestamp.
        Used for global rate limit enforcement.
        """
        stmt = select(func.count()).where(
            InterventionDecisionModel.decision_type == DecisionType.ACT.value,
            InterventionDecisionModel.decided_at >= since_timestamp,
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def has_recent_route_decision(
        self, payment_attempt_id: str, route_id: str, since_timestamp: datetime
    ) -> bool:
        """
        Check if a specific route was selected for this payment attempt since since_timestamp.
        Used for route cooldown enforcement.
        """
        stmt = select(func.count()).where(
            InterventionDecisionModel.payment_attempt_id == payment_attempt_id,
            InterventionDecisionModel.selected_route_id == route_id,
            InterventionDecisionModel.decision_type == DecisionType.ACT.value,
            InterventionDecisionModel.decided_at >= since_timestamp,
        )
        result = await self.session.execute(stmt)
        count = result.scalar() or 0
        return count > 0

    async def check_terminal_event_exists(self, payment_id: str, t_decide: datetime) -> bool:
        """
        Checks if a terminal event (payment.captured, payment.failed) was ingested before t_decide.
        """
        stmt = text("""
            SELECT EXISTS (
                SELECT 1
                FROM payment_events
                WHERE payment_id = :payment_id
                  AND event_type IN ('payment.captured', 'payment.failed')
                  AND ingested_at < :t_decide
            )
        """)
        result = await self.session.execute(stmt, {"payment_id": payment_id, "t_decide": t_decide})
        return bool(result.scalar())

    async def get_stage5_prediction_as_of(
        self, payment_attempt_id: str, t_decide: datetime
    ) -> Optional[FailurePrediction]:
        """
        Retrieves the latest FailurePrediction for a payment attempt evaluated as-of t_decide.
        Strict temporal boundary: predicted_at <= t_decide.
        """
        from src.infrastructure.models import FailurePredictionModel
        from src.core.domain.failure_prediction_models import FailurePrediction

        stmt = (
            select(FailurePredictionModel)
            .where(
                FailurePredictionModel.payment_attempt_id == payment_attempt_id,
                FailurePredictionModel.predicted_at <= t_decide,
            )
            .order_by(FailurePredictionModel.prediction_version.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return FailurePrediction(
            prediction_id=model.prediction_id,
            payment_attempt_id=model.payment_attempt_id,
            prediction_version=model.prediction_version,
            predicted_at=model.predicted_at,
            prediction_horizon=model.prediction_horizon,
            failure_probability=model.failure_probability,
            risk_band=model.risk_band,
            prediction_status=model.prediction_status,
            model_name=model.model_name,
            model_version=model.model_version,
            feature_schema_version=model.feature_schema_version,
            feature_snapshot=model.feature_snapshot,
            input_fingerprint=model.input_fingerprint,
            created_at=model.created_at,
        )

    async def append_decision(self, decision: InterventionDecision) -> None:
        """
        Insert a new decision row. NEVER updates an existing row.
        """
        model = InterventionDecisionModel(
            decision_id=decision.decision_id,
            payment_attempt_id=decision.payment_attempt_id,
            decision_version=decision.decision_version,
            decided_at=decision.decided_at,
            decision_type=decision.decision_type.value,
            selected_route_id=decision.selected_route_id.value if decision.selected_route_id else None,
            failure_probability=decision.failure_probability,
            stage5_prediction_id=decision.stage5_prediction_id,
            diagnosis_confidence=decision.diagnosis_confidence,
            decision_confidence=decision.decision_confidence,
            gate_verdict=decision.gate_verdict.value,
            policy_id=decision.policy_id,
            policy_version=decision.policy_version,
            input_fingerprint=decision.input_fingerprint,
            evaluation_audit_payload=decision.evaluation_audit_payload,
            created_at=decision.created_at,
        )
        self.session.add(model)
        await self.session.flush()

    def _map_decision(self, model: InterventionDecisionModel) -> InterventionDecision:
        return InterventionDecision(
            decision_id=model.decision_id,
            payment_attempt_id=model.payment_attempt_id,
            decision_version=model.decision_version,
            decided_at=model.decided_at,
            decision_type=DecisionType(model.decision_type),
            selected_route_id=InterventionRouteKey(model.selected_route_id) if model.selected_route_id else None,
            failure_probability=model.failure_probability,
            stage5_prediction_id=model.stage5_prediction_id,
            diagnosis_confidence=model.diagnosis_confidence,
            decision_confidence=model.decision_confidence,
            gate_verdict=GateVerdict(model.gate_verdict),
            policy_id=model.policy_id,
            policy_version=model.policy_version,
            input_fingerprint=model.input_fingerprint,
            evaluation_audit_payload=model.evaluation_audit_payload,
            created_at=model.created_at,
        )
