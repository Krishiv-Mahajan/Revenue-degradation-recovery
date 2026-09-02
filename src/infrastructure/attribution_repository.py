"""
Repository for Stage 8 Counterfactual Attribution persistence and upstream data retrieval.
Adheres strictly to append-only persistence, advisory locking, and information barriers.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.attribution_models import (
    AttributionStatus,
    CounterfactualAttribution,
    TreatmentStatus,
)
from src.infrastructure.models import (
    CounterfactualAttributionModel,
    FailurePredictionModel,
    InterventionCommandModel,
    InterventionDecisionModel,
    InterventionExecutionAttemptModel,
    PaymentEventModel,
    PaymentOutcomeObservationModel,
)


class AttributionRepository:
    """Repository handling counterfactual attribution persistence and upstream queries."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def acquire_attribution_lock(self, command_id: uuid.UUID) -> None:
        """
        Acquire a transaction-level PostgreSQL advisory lock on the command_id.
        Prevents concurrent workers from creating duplicate attribution versions.
        """
        lock_id = int.from_bytes(command_id.bytes[:8], byteorder="big", signed=True)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )

    def _to_domain(self, m: CounterfactualAttributionModel) -> CounterfactualAttribution:
        p0 = Decimal(str(m.counterfactual_failure_probability)) if m.counterfactual_failure_probability is not None else None
        conf = Decimal(str(m.attribution_confidence))

        return CounterfactualAttribution(
            attribution_id=m.attribution_id,
            command_id=m.command_id,
            payment_attempt_id=m.payment_attempt_id,
            decision_id=m.decision_id,
            attribution_version=m.attribution_version,
            attributed_at=m.attributed_at,
            as_of_timestamp=m.as_of_timestamp,
            attribution_status=AttributionStatus(m.attribution_status),
            methodology_name=m.methodology_name,
            methodology_version=m.methodology_version,
            treatment_status=TreatmentStatus(m.treatment_status),
            observed_payment_outcome=m.observed_payment_outcome,
            payment_amount_minor_units=m.payment_amount_minor_units,
            counterfactual_failure_probability=p0,
            counterfactual_loss_exposure_minor_units=m.counterfactual_loss_exposure_minor_units,
            counterfactual_natural_success_gmv_minor_units=m.counterfactual_natural_success_gmv_minor_units,
            attributed_protected_gmv_minor_units=m.attributed_protected_gmv_minor_units,
            attribution_confidence=conf,
            is_synthetic_baseline=m.is_synthetic_baseline,
            is_simulated_execution=m.is_simulated_execution,
            attribution_audit_payload=dict(m.attribution_audit_payload),
            created_at=m.created_at,
        )

    async def get_latest_attribution(self, command_id: uuid.UUID) -> Optional[CounterfactualAttribution]:
        """Fetch latest attribution record for a command by version."""
        stmt = (
            select(CounterfactualAttributionModel)
            .where(CounterfactualAttributionModel.command_id == command_id)
            .order_by(desc(CounterfactualAttributionModel.attribution_version))
            .limit(1)
        )
        res = await self.session.execute(stmt)
        model = res.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_attribution_by_as_of(
        self, command_id: uuid.UUID, as_of_timestamp: datetime
    ) -> Optional[CounterfactualAttribution]:
        """Fetch attribution record for a specific command and exact as_of_timestamp."""
        stmt = (
            select(CounterfactualAttributionModel)
            .where(
                CounterfactualAttributionModel.command_id == command_id,
                CounterfactualAttributionModel.as_of_timestamp == as_of_timestamp,
            )
            .limit(1)
        )
        res = await self.session.execute(stmt)
        model = res.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_all_attributions(self, command_id: uuid.UUID) -> List[CounterfactualAttribution]:
        """Fetch all historical attribution records for a command ordered by version."""
        stmt = (
            select(CounterfactualAttributionModel)
            .where(CounterfactualAttributionModel.command_id == command_id)
            .order_by(CounterfactualAttributionModel.attribution_version.asc())
        )
        res = await self.session.execute(stmt)
        return [self._to_domain(m) for m in res.scalars().all()]

    async def append_attribution(self, attr: CounterfactualAttribution) -> CounterfactualAttribution:
        """Persist a new immutable attribution record version."""
        model = CounterfactualAttributionModel(
            attribution_id=attr.attribution_id,
            command_id=attr.command_id,
            payment_attempt_id=attr.payment_attempt_id,
            decision_id=attr.decision_id,
            attribution_version=attr.attribution_version,
            attributed_at=attr.attributed_at,
            as_of_timestamp=attr.as_of_timestamp,
            attribution_status=attr.attribution_status.value,
            methodology_name=attr.methodology_name,
            methodology_version=attr.methodology_version,
            treatment_status=attr.treatment_status.value,
            observed_payment_outcome=attr.observed_payment_outcome,
            payment_amount_minor_units=attr.payment_amount_minor_units,
            counterfactual_failure_probability=attr.counterfactual_failure_probability,
            counterfactual_loss_exposure_minor_units=attr.counterfactual_loss_exposure_minor_units,
            counterfactual_natural_success_gmv_minor_units=attr.counterfactual_natural_success_gmv_minor_units,
            attributed_protected_gmv_minor_units=attr.attributed_protected_gmv_minor_units,
            attribution_confidence=attr.attribution_confidence,
            is_synthetic_baseline=attr.is_synthetic_baseline,
            is_simulated_execution=attr.is_simulated_execution,
            attribution_audit_payload=attr.attribution_audit_payload,
            created_at=attr.created_at,
        )
        self.session.add(model)
        await self.session.flush()
        return attr

    # Upstream data queries adhering to information barriers (<= as_of_timestamp)

    async def get_command(self, command_id: uuid.UUID) -> Optional[InterventionCommandModel]:
        """Fetch command model."""
        stmt = select(InterventionCommandModel).where(InterventionCommandModel.command_id == command_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_latest_execution_attempt(
        self, command_id: uuid.UUID, as_of_timestamp: datetime
    ) -> Optional[InterventionExecutionAttemptModel]:
        """Fetch latest execution attempt recorded <= as_of_timestamp."""
        stmt = (
            select(InterventionExecutionAttemptModel)
            .where(
                InterventionExecutionAttemptModel.command_id == command_id,
                InterventionExecutionAttemptModel.created_at <= as_of_timestamp,
            )
            .order_by(desc(InterventionExecutionAttemptModel.attempt_number))
            .limit(1)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_latest_outcome_observation(
        self, command_id: uuid.UUID, as_of_timestamp: datetime
    ) -> Optional[PaymentOutcomeObservationModel]:
        """Fetch latest payment outcome observation recorded <= as_of_timestamp."""
        stmt = (
            select(PaymentOutcomeObservationModel)
            .where(
                PaymentOutcomeObservationModel.command_id == command_id,
                PaymentOutcomeObservationModel.as_of_timestamp <= as_of_timestamp,
            )
            .order_by(desc(PaymentOutcomeObservationModel.observation_version))
            .limit(1)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_decision(self, decision_id: uuid.UUID) -> Optional[InterventionDecisionModel]:
        """Fetch Stage 6 decision model."""
        stmt = select(InterventionDecisionModel).where(InterventionDecisionModel.decision_id == decision_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_prediction(self, prediction_id: uuid.UUID) -> Optional[FailurePredictionModel]:
        """Fetch Stage 5 failure prediction model."""
        stmt = select(FailurePredictionModel).where(FailurePredictionModel.prediction_id == prediction_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_canonical_payment_amount(
        self, payment_attempt_id: str, as_of_timestamp: datetime
    ) -> Optional[int]:
        """
        Fetch canonical payment amount in minor units from Stage 1 payment_events table <= as_of_timestamp.
        """
        stmt = (
            select(PaymentEventModel.amount_minor_units)
            .where(
                PaymentEventModel.payment_id == payment_attempt_id,
                PaymentEventModel.ingested_at <= as_of_timestamp,
            )
            .limit(1)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()
