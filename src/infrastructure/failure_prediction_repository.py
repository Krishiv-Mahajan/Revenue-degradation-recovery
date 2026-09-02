"""
Stage 5 — Failure Prediction repository.

APPEND-ONLY: exposes only INSERT operations for predictions.
No UPDATE, no DELETE. Immutability is enforced here at the repository boundary.
"""
import struct
import uuid
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.failure_prediction_models import FailurePrediction
from src.infrastructure.models import FailurePredictionModel


class FailurePredictionRepository:
    """
    Append-only repository for Stage 5 Failure Predictions.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_max_prediction_version(self, payment_attempt_id: str) -> int:
        """
        Returns the current maximum prediction_version for a payment attempt.
        Returns 0 if no predictions exist yet.
        """
        stmt = select(func.max(FailurePredictionModel.prediction_version)).where(
            FailurePredictionModel.payment_attempt_id == payment_attempt_id
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def acquire_version_allocation_lock(self, payment_attempt_id: str) -> None:
        """
        Acquire a transaction-level advisory lock to serialize version allocation.
        """
        # Create deterministic UUID from payment_attempt_id to generate 2 ints
        hash_uuid = uuid.uuid5(uuid.NAMESPACE_OID, payment_attempt_id)
        k1, k2 = struct.unpack("<ii", hash_uuid.bytes[:8])
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:k1, :k2)"), {"k1": k1, "k2": k2}
        )

    async def get_latest_prediction(self, payment_attempt_id: str) -> Optional[FailurePrediction]:
        """
        Returns the latest (highest prediction_version) prediction for a payment attempt.
        """
        stmt = (
            select(FailurePredictionModel)
            .where(FailurePredictionModel.payment_attempt_id == payment_attempt_id)
            .order_by(FailurePredictionModel.prediction_version.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._map_prediction(model)

    async def get_prediction_by_fingerprint(self, input_fingerprint: str) -> Optional[FailurePrediction]:
        """
        Retrieves a prediction by its exact input fingerprint.
        """
        stmt = (
            select(FailurePredictionModel)
            .where(FailurePredictionModel.input_fingerprint == input_fingerprint)
            .limit(1)
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._map_prediction(model)

    async def append_prediction(self, prediction: FailurePrediction) -> None:
        """
        Insert a new prediction row. NEVER updates an existing row.
        """
        model = FailurePredictionModel(
            prediction_id=prediction.prediction_id,
            payment_attempt_id=prediction.payment_attempt_id,
            prediction_version=prediction.prediction_version,
            predicted_at=prediction.predicted_at,
            prediction_horizon=prediction.prediction_horizon,
            failure_probability=prediction.failure_probability,
            risk_band=prediction.risk_band,
            prediction_status=prediction.prediction_status,
            model_name=prediction.model_name,
            model_version=prediction.model_version,
            feature_schema_version=prediction.feature_schema_version,
            feature_snapshot=prediction.feature_snapshot,
            input_fingerprint=prediction.input_fingerprint,
            created_at=prediction.created_at,
        )
        self.session.add(model)
        await self.session.flush()

    def _map_prediction(self, model: FailurePredictionModel) -> FailurePrediction:
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
