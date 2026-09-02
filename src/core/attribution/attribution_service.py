"""
Orchestration service for Stage 8 Counterfactual Attribution.
Enforces information barriers, treatment prerequisites, temporal precedence, and idempotent versioning.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.core.attribution.calculator import (
    METHODOLOGY_NAME,
    METHODOLOGY_VERSION,
    calculate_attribution,
)
from src.core.domain.attribution_exceptions import MissingUpstreamDataError
from src.core.domain.attribution_models import (
    CounterfactualAttribution,
    ModelProvenance,
    TreatmentStatus,
    make_attribution_id,
)
from src.infrastructure.attribution_repository import AttributionRepository


class CounterfactualAttributionService:
    """Service orchestrating counterfactual attribution and protected GMV derivation."""

    def __init__(self, attribution_repo: AttributionRepository):
        self.repo = attribution_repo

    async def attribute_payment_intervention(
        self,
        command_id: uuid.UUID,
        as_of_timestamp: Optional[datetime] = None,
    ) -> CounterfactualAttribution:
        """
        Evaluate and persist counterfactual attribution for an intervention command as-of a declared cutoff.
        """
        as_of = as_of_timestamp or datetime.now(timezone.utc)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)

        # 1. Acquire transaction advisory lock to serialize concurrent evaluations
        await self.repo.acquire_attribution_lock(command_id)

        # 2. Idempotency check: exact as_of timestamp re-evaluation returns existing record
        existing = await self.repo.get_attribution_by_as_of(command_id, as_of)
        if existing is not None:
            return existing

        # 3. Determine next attribution version
        latest_attr = await self.repo.get_latest_attribution(command_id)
        next_version = (latest_attr.attribution_version + 1) if latest_attr else 1

        # 4. Fetch upstream records strictly <= as_of
        command = await self.repo.get_command(command_id)
        if command is None:
            raise MissingUpstreamDataError(f"InterventionCommand with id={command_id} not found.")

        attempt = await self.repo.get_latest_execution_attempt(command_id, as_of)
        observation = await self.repo.get_latest_outcome_observation(command_id, as_of)

        decision = await self.repo.get_decision(command.decision_id)
        if decision is None:
            raise MissingUpstreamDataError(f"InterventionDecision with id={command.decision_id} not found.")

        prediction = None
        if decision.stage5_prediction_id:
            prediction = await self.repo.get_prediction(decision.stage5_prediction_id)

        payment_amount = await self.repo.get_canonical_payment_amount(
            command.payment_attempt_id, as_of
        )
        if payment_amount is None:
            payment_amount = 0

        # 5. Evaluate Treatment Status
        if decision.decision_type != "ACT":
            treatment_status = TreatmentStatus.UNTREATED
        elif command.command_status in ("NOT_NEEDED", "EXPIRED", "PENDING"):
            treatment_status = TreatmentStatus.UNTREATED
        elif attempt is None:
            treatment_status = TreatmentStatus.UNTREATED
        elif attempt.execution_status != "SUCCESS":
            treatment_status = TreatmentStatus.EXECUTION_FAILED
        else:
            # Execution was successful; verify temporal precedence
            if (
                observation is not None
                and observation.terminal_event_timestamp is not None
                and observation.terminal_event_timestamp < attempt.completed_at
            ):
                # Outcome occurred BEFORE execution completed; intervention was not the cause
                treatment_status = TreatmentStatus.UNTREATED
            else:
                treatment_status = TreatmentStatus.TREATED

        # 6. Evaluate Outcome
        if observation is None:
            observed_outcome = "UNKNOWN_IN_FLIGHT"
        else:
            observed_outcome = observation.payment_outcome

        # 7. Evaluate Prediction Provenance and p0
        if prediction is None:
            provenance = ModelProvenance.UNKNOWN
            pred_status = "NOT_ELIGIBLE"
            p0 = None
            is_synthetic = True
        else:
            p0 = (
                Decimal(str(prediction.failure_probability)).quantize(Decimal("0.0001"))
                if prediction.failure_probability is not None
                else None
            )
            pred_status = prediction.prediction_status
            if "synthetic" in (prediction.model_name or "").lower():
                provenance = ModelProvenance.SYNTHETIC_DEVELOPMENT
                is_synthetic = True
            else:
                provenance = ModelProvenance.EMPIRICAL_PRODUCTION
                is_synthetic = False

        # 8. Compute Timing Proximity Delta
        if attempt is not None and observation is not None and observation.terminal_event_timestamp is not None:
            delta_seconds = (observation.terminal_event_timestamp - attempt.completed_at).total_seconds()
        else:
            delta_seconds = None

        # 9. Calculate Risk-Weighted Attribution
        calc_result = calculate_attribution(
            payment_amount_minor_units=payment_amount,
            p0=p0,
            treatment_status=treatment_status,
            observed_payment_outcome=observed_outcome,
            provenance=provenance,
            prediction_status=pred_status,
            diagnosis_confidence=decision.diagnosis_confidence,
            timing_delta_seconds=delta_seconds,
        )

        # 10. Assemble Complete Audit Payload
        audit_payload = {
            "methodology_name": METHODOLOGY_NAME,
            "methodology_version": METHODOLOGY_VERSION,
            "stage5_model_id": prediction.model_name if prediction else None,
            "stage5_model_provenance": provenance.value,
            "counterfactual_risk_proxy_p0": str(p0) if p0 is not None else None,
            "treatment_evidence": {
                "decision_id": str(decision.decision_id),
                "command_id": str(command.command_id),
                "attempt_id": str(attempt.attempt_id) if attempt else None,
                "route_key": command.route_key,
                "execution_status": attempt.execution_status if attempt else None,
                "execution_completed_at": attempt.completed_at.isoformat() if attempt else None,
            },
            "outcome_evidence": {
                "observation_id": str(observation.observation_id) if observation else None,
                "terminal_event_id": str(observation.terminal_event_id) if observation and observation.terminal_event_id else None,
                "terminal_event_type": observation.terminal_event_type if observation else None,
                "terminal_event_timestamp": observation.terminal_event_timestamp.isoformat() if observation and observation.terminal_event_timestamp else None,
                "conflict_detected": observation.observation_audit_payload.get("conflict_detected", False) if observation else False,
                "conflicting_events": observation.observation_audit_payload.get("conflicting_events", []) if observation else [],
            },
            "evidence_score_components": calc_result.audit_details,
            "timing_delta_seconds": delta_seconds,
            "timestamps": {
                "t_decided": decision.decided_at.isoformat() if decision else None,
                "t_command_created": command.created_at.isoformat() if command else None,
                "t_execution_start": attempt.started_at.isoformat() if attempt else None,
                "t_execution_completed": attempt.completed_at.isoformat() if attempt else None,
                "t_terminal_event": observation.terminal_event_timestamp.isoformat() if observation and observation.terminal_event_timestamp else None,
                "as_of_timestamp": as_of.isoformat(),
            },
            "assumptions": [
                "Unconfoundedness given pre-treatment features",
                "p0 is pre-intervention failure-risk estimate used as counterfactual risk proxy",
                "Temporal precedence: execution completed before terminal outcome",
                "Monotonicity: intervention does not cause failure of otherwise successful payment",
            ],
        }

        attribution_id = make_attribution_id(command.command_id, next_version)
        now = datetime.now(timezone.utc)
        is_simulated = attempt.is_simulation if attempt else True

        attribution = CounterfactualAttribution(
            attribution_id=attribution_id,
            command_id=command.command_id,
            payment_attempt_id=command.payment_attempt_id,
            decision_id=decision.decision_id,
            attribution_version=next_version,
            attributed_at=now,
            as_of_timestamp=as_of,
            attribution_status=calc_result.attribution_status,
            methodology_name=METHODOLOGY_NAME,
            methodology_version=METHODOLOGY_VERSION,
            treatment_status=treatment_status,
            observed_payment_outcome=observed_outcome,
            payment_amount_minor_units=payment_amount,
            counterfactual_failure_probability=p0,
            counterfactual_loss_exposure_minor_units=calc_result.counterfactual_loss_exposure_minor_units,
            counterfactual_natural_success_gmv_minor_units=calc_result.counterfactual_natural_success_gmv_minor_units,
            attributed_protected_gmv_minor_units=calc_result.attributed_protected_gmv_minor_units,
            attribution_confidence=calc_result.attribution_confidence,
            is_synthetic_baseline=is_synthetic,
            is_simulated_execution=is_simulated,
            attribution_audit_payload=audit_payload,
            created_at=now,
        )

        return await self.repo.append_attribution(attribution)
