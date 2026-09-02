"""
Stage 5 — Failure Prediction service layer (Phase 1).

Strictly foundational: handles prediction eligibility and terminal outcome lookup.
Does NOT implement feature engineering, modeling, or Stage 6 logic.
"""
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import PaymentEventModel
from src.core.services.feature_reconstruction_service import FeatureReconstructionService
from src.core.domain.failure_prediction_models import PredictionStatus


class FailurePredictionService:
    def __init__(self, session: AsyncSession, feature_reconstruction_service: FeatureReconstructionService) -> None:
        self.session = session
        self.feature_service = feature_reconstruction_service

    async def orchestrate_prediction(self, payment_attempt_id: str, T: datetime):
        """
        Phase 2 implementation: Extract features and determine prediction status.
        Model inference is deferred to Phase 3.
        """
        # 1. Check eligibility
        is_eligible = await self.is_eligible_for_prediction(payment_attempt_id, T)
        if not is_eligible:
            return None, PredictionStatus.NOT_ELIGIBLE
            
        # 2. Reconstruct features
        feature_snapshot = await self.feature_service.reconstruct_features(payment_attempt_id, T)
        
        # 3. Check sufficiency
        if feature_snapshot is None:
            return None, PredictionStatus.NOT_ELIGIBLE
            
        if feature_snapshot.insufficient_global_volume:
            return feature_snapshot, PredictionStatus.INSUFFICIENT_DATA
            
        # Returning PREDICTED status for Phase 2 verification
        return feature_snapshot, PredictionStatus.PREDICTED

    async def is_eligible_for_prediction(self, payment_attempt_id: str, T: datetime) -> bool:
        """
        Evaluate if a payment is eligible for prediction as of T.
        
        Rules:
        - Trigger event (payment.authorized) must exist.
        - No terminal event (payment.captured, payment.failed) for the same payment_id
          may have been ingested before T.
          
        Uses the ix_payment_events_eligibility index.
        """
        stmt = text("""
            SELECT EXISTS (
                SELECT 1
                FROM payment_events
                WHERE payment_id = :payment_id
                  AND event_type IN ('payment.captured', 'payment.failed')
                  AND ingested_at < :T
            )
        """)
        
        result = await self.session.execute(stmt, {"payment_id": payment_attempt_id, "T": T})
        terminal_exists = result.scalar()
        
        # Eligible if no prior terminal event
        return not terminal_exists

    async def lookup_terminal_label(
        self, payment_attempt_id: str, T: datetime, observation_window_minutes: Optional[int] = 30
    ) -> Optional[int]:
        """
        Look up the actual terminal outcome (for training/evaluation).
        Only looks at events ingested AFTER T.
        
        Label mapping:
        - first subsequent payment.failed => 1
        - first subsequent payment.captured => 0
        - no terminal event within the configurable observation window => None (unresolved/excluded)
        """
        query = select(PaymentEventModel).where(
            PaymentEventModel.payment_id == payment_attempt_id,
            PaymentEventModel.event_type.in_(['payment.captured', 'payment.failed']),
            PaymentEventModel.ingested_at >= T
        )
        
        if observation_window_minutes is not None:
            horizon_time = T + timedelta(minutes=observation_window_minutes)
            query = query.where(PaymentEventModel.ingested_at <= horizon_time)
            
        query = query.order_by(PaymentEventModel.ingested_at.asc()).limit(1)
        
        result = await self.session.execute(query)
        terminal_event = result.scalar_one_or_none()
        
        if not terminal_event:
            return None
            
        if terminal_event.event_type == 'payment.failed':
            return 1
        return 0
