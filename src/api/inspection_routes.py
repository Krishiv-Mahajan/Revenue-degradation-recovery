from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from datetime import datetime, timedelta
import uuid

from src.api.dependencies import get_db_session
from src.infrastructure.models import (
    PaymentEventModel,
    PaymentHealthSnapshotModel,
    DegradationEpisodeModel,
    RCAEvaluationModel,
    CandidateCauseModel,
    FailurePredictionModel,
    InterventionDecisionModel,
    InterventionCommandModel,
    InterventionExecutionAttemptModel,
    PaymentOutcomeObservationModel,
    CounterfactualAttributionModel
)

router = APIRouter(prefix="/api/v1", tags=["internal-inspection"])

@router.get("/summary")
async def get_executive_summary(session: AsyncSession = Depends(get_db_session)) -> Dict[str, Any]:
    """
    Returns executive summary metrics across the end-to-end engine.
    Read-only internal/demo inspection endpoint.
    """
    # Total processed GMV (from auths)
    auth_stmt = select(func.sum(PaymentEventModel.amount_minor_units)).where(PaymentEventModel.event_type == 'payment.authorized')
    auth_res = await session.execute(auth_stmt)
    total_gmv = auth_res.scalar_one_or_none() or 0

    # Total protected GMV
    protected_stmt = select(func.sum(CounterfactualAttributionModel.attributed_protected_gmv_minor_units))
    protected_res = await session.execute(protected_stmt)
    total_protected_gmv = protected_res.scalar_one_or_none() or 0

    # Active degradation episodes
    active_episodes_stmt = select(func.count(DegradationEpisodeModel.episode_id)).where(DegradationEpisodeModel.status == 'ACTIVE')
    active_episodes_res = await session.execute(active_episodes_stmt)
    active_episodes = active_episodes_res.scalar_one_or_none() or 0

    # Total interventions executed
    interventions_stmt = select(func.count(InterventionCommandModel.command_id)).where(InterventionCommandModel.command_status == 'SUCCEEDED')
    interventions_res = await session.execute(interventions_stmt)
    total_interventions = interventions_res.scalar_one_or_none() or 0
    
    # Successful payment capture rate (overall)
    all_events_stmt = select(PaymentEventModel.event_type, func.count(PaymentEventModel.event_id)).group_by(PaymentEventModel.event_type)
    all_events_res = await session.execute(all_events_stmt)
    event_counts = dict(all_events_res.all())
    
    total_auths = event_counts.get('payment.authorized', 0)
    total_captures = event_counts.get('payment.captured', 0)
    success_rate = (total_captures / total_auths) if total_auths > 0 else 0.0

    return {
        "total_gmv_minor_units": total_gmv,
        "total_protected_gmv_minor_units": total_protected_gmv,
        "active_degradation_episodes": active_episodes,
        "total_successful_interventions": total_interventions,
        "overall_success_rate": success_rate,
        "total_auths": total_auths,
        "total_captures": total_captures
    }


@router.get("/episodes")
async def get_episodes(
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum number of episodes to return"),
    session: AsyncSession = Depends(get_db_session)
) -> List[Dict[str, Any]]:
    """
    Returns degradation episodes ordered by most recent started window.
    Safe bounded read-only inspection endpoint.
    """
    stmt = (
        select(DegradationEpisodeModel)
        .order_by(desc(DegradationEpisodeModel.started_at_window))
        .limit(limit)
    )
    res = await session.execute(stmt)
    episodes = res.scalars().all()
    
    return [
        {
            "episode_id": str(e.episode_id),
            "segment_dimension": e.segment_dimension,
            "segment_value": e.segment_value,
            "started_at_window": e.started_at_window.isoformat(),
            "ended_at_window": e.ended_at_window.isoformat() if e.ended_at_window else None,
            "status": e.status,
            "severity": e.severity,
            "peak_absolute_drop": e.peak_absolute_drop
        }
        for e in episodes
    ]

@router.get("/rca/{episode_id}")
async def get_rca_evaluation(episode_id: str, session: AsyncSession = Depends(get_db_session)) -> Dict[str, Any]:
    """
    Returns the latest RCA evaluation and candidate causes for an episode.
    Enriched with episode metadata and downstream prediction/intervention telemetry.
    Read-only inspection endpoint.
    """
    try:
        ep_uuid = uuid.UUID(episode_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RCA evaluation not found")

    # 1. Fetch Episode
    ep_stmt = select(DegradationEpisodeModel).where(DegradationEpisodeModel.episode_id == ep_uuid)
    ep_res = await session.execute(ep_stmt)
    episode = ep_res.scalar_one_or_none()

    # 2. Fetch Latest RCA Evaluation
    eval_stmt = (
        select(RCAEvaluationModel)
        .where(RCAEvaluationModel.episode_id == ep_uuid)
        .order_by(desc(RCAEvaluationModel.evaluation_version))
        .limit(1)
    )
    eval_res = await session.execute(eval_stmt)
    evaluation = eval_res.scalar_one_or_none()
    
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RCA evaluation not found")
        
    causes_stmt = (
        select(CandidateCauseModel)
        .where(CandidateCauseModel.evaluation_id == evaluation.evaluation_id)
        .order_by(CandidateCauseModel.rank)
    )
    causes_res = await session.execute(causes_stmt)
    causes = causes_res.scalars().all()

    # 3. Downstream Telemetry strictly scoped to the evaluated episode
    ep_str = str(ep_uuid)

    dec_stmt = (
        select(
            InterventionDecisionModel.decision_type,
            func.count(InterventionDecisionModel.decision_id)
        )
        .where(
            InterventionDecisionModel.evaluation_audit_payload["upstream_context"]["episode_id"].as_string() == ep_str
        )
        .group_by(InterventionDecisionModel.decision_type)
    )
    dec_res = await session.execute(dec_stmt)
    dec_counts = dict(dec_res.all())

    pred_stmt = (
        select(
            func.count(FailurePredictionModel.prediction_id),
            func.max(FailurePredictionModel.failure_probability)
        )
        .select_from(FailurePredictionModel)
        .join(
            InterventionDecisionModel,
            FailurePredictionModel.prediction_id == InterventionDecisionModel.stage5_prediction_id
        )
        .where(
            InterventionDecisionModel.evaluation_audit_payload["upstream_context"]["episode_id"].as_string() == ep_str
        )
    )
    pred_res = await session.execute(pred_stmt)
    pred_row = pred_res.fetchone()
    pred_count = pred_row[0] if pred_row else 0
    peak_prob = pred_row[1] if pred_row else 0.0

    route_stmt = (
        select(InterventionDecisionModel.selected_route_id)
        .where(
            InterventionDecisionModel.evaluation_audit_payload["upstream_context"]["episode_id"].as_string() == ep_str,
            InterventionDecisionModel.selected_route_id.isnot(None)
        )
        .group_by(InterventionDecisionModel.selected_route_id)
        .order_by(desc(func.count(InterventionDecisionModel.decision_id)))
        .limit(1)
    )
    route_res = await session.execute(route_stmt)
    primary_route = route_res.scalar_one_or_none()

    attr_stmt = (
        select(func.sum(CounterfactualAttributionModel.attributed_protected_gmv_minor_units))
        .select_from(CounterfactualAttributionModel)
        .join(
            InterventionDecisionModel,
            CounterfactualAttributionModel.decision_id == InterventionDecisionModel.decision_id,
        )
        .where(
            InterventionDecisionModel.evaluation_audit_payload["upstream_context"]["episode_id"].as_string() == ep_str
        )
    )
    attr_res = await session.execute(attr_stmt)
    window_protected_gmv = attr_res.scalar_one_or_none() or 0
    
    return {
        "evaluation_id": str(evaluation.evaluation_id),
        "classification": evaluation.classification,
        "analysis_window_start": evaluation.analysis_window_start.isoformat(),
        "analysis_window_end": evaluation.analysis_window_end.isoformat(),
        "episode": {
            "episode_id": str(episode.episode_id),
            "segment_dimension": episode.segment_dimension,
            "segment_value": episode.segment_value,
            "severity": episode.severity,
            "status": episode.status,
            "started_at_window": episode.started_at_window.isoformat(),
            "ended_at_window": episode.ended_at_window.isoformat() if episode.ended_at_window else None,
            "peak_absolute_drop": episode.peak_absolute_drop,
            "affected_window_count": episode.affected_window_count
        } if episode else None,
        "candidates": [
            {
                "candidate_id": str(c.candidate_id),
                "dimension": c.candidate_dimension,
                "value": c.candidate_value,
                "evidence_strength": c.evidence_strength,
                "excess_failure_contribution": c.excess_failure_contribution,
                "rank": c.rank
            }
            for c in causes
        ],
        "prediction_context": {
            "window_predictions_evaluated": pred_count or 0,
            "peak_failure_probability": peak_prob or 0.0,
            "severity_bonus_tier": episode.severity if episode else None,
            "top_rca_evidence_strength": causes[0].evidence_strength if causes else None,
            "prediction_status": "PREDICTED" if pred_count else "NO_PREDICTIONS"
        },
        "intervention_context": {
            "act_decisions": dec_counts.get("ACT", 0),
            "monitor_decisions": dec_counts.get("MONITOR", 0),
            "primary_selected_route": primary_route,
            "decision_threshold": 0.70,
            "window_protected_gmv_minor_units": window_protected_gmv
        }
    }

@router.get("/attributions")
async def get_recent_attributions(
    limit: int = Query(default=50, ge=1, le=500, description="Maximum number of attributions to return"),
    session: AsyncSession = Depends(get_db_session)
) -> List[Dict[str, Any]]:
    """
    Returns recent protected GMV attributions.
    Safe bounded read-only inspection endpoint.
    """
    stmt = (
        select(CounterfactualAttributionModel)
        .order_by(desc(CounterfactualAttributionModel.created_at))
        .limit(limit)
    )
    res = await session.execute(stmt)
    attributions = res.scalars().all()
    
    return [
        {
            "attribution_id": str(a.attribution_id),
            "payment_attempt_id": a.payment_attempt_id,
            "status": a.attribution_status,
            "treatment_status": a.treatment_status,
            "observed_payment_outcome": a.observed_payment_outcome,
            "payment_amount_minor_units": a.payment_amount_minor_units,
            "counterfactual_failure_probability": float(a.counterfactual_failure_probability) if a.counterfactual_failure_probability else None,
            "counterfactual_loss_exposure_minor_units": a.counterfactual_loss_exposure_minor_units,
            "attributed_protected_gmv_minor_units": a.attributed_protected_gmv_minor_units,
            "attribution_confidence": float(a.attribution_confidence),
            "attributed_at": a.attributed_at.isoformat()
        }
        for a in attributions
    ]

@router.get("/timeline/{payment_id}")
async def get_payment_timeline(payment_id: str, session: AsyncSession = Depends(get_db_session)) -> Dict[str, Any]:
    """
    Returns an end-to-end chronological lifecycle trace across Stages 1–8 for a specific payment.
    Read-only inspection endpoint.
    """
    # 1. Events
    events_stmt = select(PaymentEventModel).where(PaymentEventModel.payment_id == payment_id).order_by(PaymentEventModel.timestamp)
    events_res = await session.execute(events_stmt)
    events = events_res.scalars().all()
    
    if not events:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Payment {payment_id} not found")

    # 2. Prediction
    pred_stmt = select(FailurePredictionModel).where(FailurePredictionModel.payment_attempt_id == payment_id).order_by(desc(FailurePredictionModel.prediction_version)).limit(1)
    pred_res = await session.execute(pred_stmt)
    prediction = pred_res.scalar_one_or_none()
    
    # 3. Decision
    dec_stmt = select(InterventionDecisionModel).where(InterventionDecisionModel.payment_attempt_id == payment_id).order_by(desc(InterventionDecisionModel.decision_version)).limit(1)
    dec_res = await session.execute(dec_stmt)
    decision = dec_res.scalar_one_or_none()
    
    command = None
    execution = None
    observation = None
    attribution = None
    
    if decision:
        # 4. Command
        cmd_stmt = select(InterventionCommandModel).where(InterventionCommandModel.decision_id == decision.decision_id)
        cmd_res = await session.execute(cmd_stmt)
        command = cmd_res.scalar_one_or_none()
        
        if command:
            # 5. Execution Attempt
            exec_stmt = select(InterventionExecutionAttemptModel).where(InterventionExecutionAttemptModel.command_id == command.command_id).order_by(desc(InterventionExecutionAttemptModel.attempt_number)).limit(1)
            exec_res = await session.execute(exec_stmt)
            execution = exec_res.scalar_one_or_none()
            
            # 6. Observation
            obs_stmt = select(PaymentOutcomeObservationModel).where(PaymentOutcomeObservationModel.command_id == command.command_id).order_by(desc(PaymentOutcomeObservationModel.observation_version)).limit(1)
            obs_res = await session.execute(obs_stmt)
            observation = obs_res.scalar_one_or_none()
            
            # 7. Attribution
            attr_stmt = select(CounterfactualAttributionModel).where(CounterfactualAttributionModel.command_id == command.command_id).order_by(desc(CounterfactualAttributionModel.attribution_version)).limit(1)
            attr_res = await session.execute(attr_stmt)
            attribution = attr_res.scalar_one_or_none()
            
    return {
        "payment_id": payment_id,
        "events": [
            {
                "event_type": e.event_type,
                "timestamp": e.timestamp.isoformat(),
                "status": e.payment_status,
                "amount_minor_units": e.amount_minor_units
            } for e in events
        ],
        "prediction": {
            "status": prediction.prediction_status,
            "failure_probability": prediction.failure_probability,
            "risk_band": prediction.risk_band
        } if prediction else None,
        "decision": {
            "type": decision.decision_type,
            "route": decision.selected_route_id,
            "gate_verdict": decision.gate_verdict
        } if decision else None,
        "command": {
            "status": command.command_status,
            "route": command.route_key
        } if command else None,
        "execution": {
            "status": execution.execution_status,
            "duration_ms": execution.duration_ms
        } if execution else None,
        "observation": {
            "outcome": observation.payment_outcome
        } if observation else None,
        "attribution": {
            "status": attribution.attribution_status,
            "protected_gmv": attribution.attributed_protected_gmv_minor_units,
            "confidence": float(attribution.attribution_confidence) if attribution.attribution_confidence else None
        } if attribution else None
    }
