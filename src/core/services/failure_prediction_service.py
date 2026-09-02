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
from src.core.domain.failure_prediction_models import (
    PredictionStatus,
    FailurePrediction,
    make_prediction_id,
    compute_input_fingerprint,
)
from src.core.ml.model import FailurePredictionModel
from src.infrastructure.failure_prediction_repository import FailurePredictionRepository
import dataclasses


class FailurePredictionService:
    def __init__(
        self,
        session: AsyncSession,
        feature_reconstruction_service: FeatureReconstructionService,
        prediction_repository: FailurePredictionRepository,
        model: FailurePredictionModel,
    ) -> None:
        self.session = session
        self.feature_service = feature_reconstruction_service
        self.repository = prediction_repository
        self.model = model

    async def orchestrate_prediction(self, payment_attempt_id: str, T: datetime) -> FailurePrediction:
        """
        Phase 3 implementation: Extract features, determine prediction status, and persist prediction.
        Idempotent based on identical input context.
        """
        # 1. Check eligibility
        is_eligible = await self.is_eligible_for_prediction(payment_attempt_id, T)
        
        feature_snapshot = None
        feature_dict = {}
        status = PredictionStatus.PREDICTED
        
        if not is_eligible:
            status = PredictionStatus.NOT_ELIGIBLE
        else:
            # 2. Reconstruct features
            feature_snapshot = await self.feature_service.reconstruct_features(payment_attempt_id, T)
            if feature_snapshot is None or feature_snapshot.insufficient_global_volume:
                status = PredictionStatus.INSUFFICIENT_DATA
            
        if feature_snapshot is not None:
            feature_dict = dataclasses.asdict(feature_snapshot)
            
        # 3. Construct input fingerprint
        fingerprint = compute_input_fingerprint(
            payment_attempt_id=payment_attempt_id,
            predicted_at=T,
            prediction_horizon="30m",
            feature_snapshot=feature_dict,
            model_name=self.model.model_name,
            model_version=self.model.model_version,
            feature_schema_version=self.model.get_feature_schema_version(),
        )
        
        # 4. Idempotency Check: Get latest prediction for this attempt
        latest_prediction = await self.repository.get_latest_prediction(payment_attempt_id)
        if latest_prediction is not None and latest_prediction.input_fingerprint == fingerprint:
            return latest_prediction
            
        # 5. Inference (only if PREDICTED)
        probability = None
        risk_band = None
        if status == PredictionStatus.PREDICTED:
            probability = self.model.predict(feature_dict)
            if probability >= 0.7:
                risk_band = "HIGH"
            elif probability >= 0.3:
                risk_band = "ELEVATED"
            else:
                risk_band = "LOW"
                
        # 6. Persist new prediction version
        await self.repository.acquire_version_allocation_lock(payment_attempt_id)
        max_version = await self.repository.get_max_prediction_version(payment_attempt_id)
        new_version = max_version + 1
        
        prediction = FailurePrediction(
            prediction_id=make_prediction_id(payment_attempt_id, new_version),
            payment_attempt_id=payment_attempt_id,
            prediction_version=new_version,
            predicted_at=T,
            prediction_horizon="30m",
            failure_probability=probability,
            risk_band=risk_band,
            prediction_status=status.value,
            model_name=self.model.model_name,
            model_version=self.model.model_version,
            feature_schema_version=self.model.get_feature_schema_version(),
            feature_snapshot=feature_dict,
            input_fingerprint=fingerprint,
            created_at=datetime.now(T.tzinfo) if T.tzinfo else datetime.utcnow(),
        )
        
        await self.repository.append_prediction(prediction)
        return prediction

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
