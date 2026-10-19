from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, or_, and_, case, literal, union_all
from datetime import datetime, timedelta
import uuid

from src.api.dependencies import get_db_session
from src.api.security import require_dashboard_auth
from src.core.attribution.attribution_service import classify_model_provenance
from src.core.domain.payment_attempts import PAYPAL_ATTEMPT_PREFIX, PAYPAL_TRIGGER_EVENT_TYPE
from src.core.domain.source_systems import NON_RAZORPAY_PIPELINE_SOURCES, PAYPAL_SOURCE_SYSTEM
from src.core.intervention.policy import get_default_policy
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

# Router-level so every inspection route, including future ones, requires dashboard credentials.
router = APIRouter(
    prefix="/api/v1", tags=["internal-inspection"], dependencies=[Depends(require_dashboard_auth)]
)


def _latest_version_ids(id_column, group_column, version_column):
    """Ids of the newest version per group. Append-only tables keep older versions as history."""
    latest = (
        select(group_column.label("group_key"), func.max(version_column).label("version"))
        .group_by(group_column)
        .subquery()
    )
    return select(id_column).join(
        latest, and_(group_column == latest.c.group_key, version_column == latest.c.version)
    )


def _latest_attribution_ids():
    return _latest_version_ids(
        CounterfactualAttributionModel.attribution_id,
        CounterfactualAttributionModel.command_id,
        CounterfactualAttributionModel.attribution_version,
    )


def _latest_observation_ids():
    return _latest_version_ids(
        PaymentOutcomeObservationModel.observation_id,
        PaymentOutcomeObservationModel.command_id,
        PaymentOutcomeObservationModel.observation_version,
    )


def _latest_attempt_ids():
    return _latest_version_ids(
        InterventionExecutionAttemptModel.attempt_id,
        InterventionExecutionAttemptModel.command_id,
        InterventionExecutionAttemptModel.attempt_number,
    )


def _latest_decision_ids():
    return _latest_version_ids(
        InterventionDecisionModel.decision_id,
        InterventionDecisionModel.payment_attempt_id,
        InterventionDecisionModel.decision_version,
    )


def _payment_currency():
    """
    Currency of each pipeline payment attempt, from the events Stage 8 sizes it by: payment.authorized
    for Razorpay attempts, order.approved for PayPal attempts (keyed by their PayPal attempt id).
    NULL when the attempt has no sizing event or its sizing events disagree; never assumed.
    """
    razorpay_sizing = select(
        PaymentEventModel.payment_id.label("payment_id"),
        PaymentEventModel.currency.label("currency"),
    ).where(
        PaymentEventModel.event_type == "payment.authorized",
        PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES),
    )
    paypal_sizing = select(
        (literal(PAYPAL_ATTEMPT_PREFIX) + PaymentEventModel.payment_id).label("payment_id"),
        PaymentEventModel.currency.label("currency"),
    ).where(
        PaymentEventModel.event_type == PAYPAL_TRIGGER_EVENT_TYPE,
        PaymentEventModel.source_system == PAYPAL_SOURCE_SYSTEM,
    )
    sizing = union_all(razorpay_sizing, paypal_sizing).subquery()
    return (
        select(
            sizing.c.payment_id,
            case(
                (func.count(func.distinct(sizing.c.currency)) == 1, func.min(sizing.c.currency)),
                else_=None,
            ).label("currency"),
        )
        .group_by(sizing.c.payment_id)
        .subquery()
    )


async def _protected_gmv_by_currency(
    session: AsyncSession, *conditions, join_decisions: bool = False
) -> List[Dict[str, Any]]:
    """Latest-version protected GMV per currency, with the provenance of the attributions behind it."""
    currency = _payment_currency()
    positive = CounterfactualAttributionModel.attributed_protected_gmv_minor_units > 0
    stmt = (
        select(
            currency.c.currency,
            func.coalesce(func.sum(CounterfactualAttributionModel.attributed_protected_gmv_minor_units), 0),
            func.count(CounterfactualAttributionModel.attribution_id).filter(positive),
            func.count(CounterfactualAttributionModel.attribution_id).filter(
                and_(positive, CounterfactualAttributionModel.is_simulated_execution.is_(True))
            ),
            func.count(CounterfactualAttributionModel.attribution_id).filter(
                and_(positive, CounterfactualAttributionModel.is_synthetic_baseline.is_(True))
            ),
        )
        .select_from(CounterfactualAttributionModel)
        .outerjoin(currency, currency.c.payment_id == CounterfactualAttributionModel.payment_attempt_id)
    )
    if join_decisions:
        stmt = stmt.join(
            InterventionDecisionModel,
            CounterfactualAttributionModel.decision_id == InterventionDecisionModel.decision_id,
        )
    stmt = (
        stmt.where(CounterfactualAttributionModel.attribution_id.in_(_latest_attribution_ids()), *conditions)
        .group_by(currency.c.currency)
        .order_by(currency.c.currency.asc().nulls_last())
    )
    return [
        {
            "currency": row[0],
            "amount_minor_units": int(row[1]),
            "positive_attribution_count": row[2],
            "simulated_execution_count": row[3],
            "synthetic_baseline_count": row[4],
        }
        for row in (await session.execute(stmt)).all()
    ]


def _single_currency_amount(groups: List[Dict[str, Any]]) -> Optional[int]:
    """Legacy scalar total: only meaningful when every amount shares one currency; None when mixed."""
    if not groups:
        return 0
    if len(groups) == 1:
        return groups[0]["amount_minor_units"]
    return None


async def _prediction_models(
    session: AsyncSession, *conditions, join_decisions: bool = False
) -> List[Dict[str, Any]]:
    stmt = select(
        FailurePredictionModel.model_name,
        FailurePredictionModel.model_version,
        func.count(FailurePredictionModel.prediction_id),
    ).select_from(FailurePredictionModel)
    if join_decisions:
        stmt = stmt.join(
            InterventionDecisionModel,
            FailurePredictionModel.prediction_id == InterventionDecisionModel.stage5_prediction_id,
        )
    stmt = (
        stmt.where(*conditions)
        .group_by(FailurePredictionModel.model_name, FailurePredictionModel.model_version)
        .order_by(FailurePredictionModel.model_name, FailurePredictionModel.model_version)
    )
    return [
        {
            "model_name": name,
            "model_version": version,
            "model_provenance": classify_model_provenance(name, version).value,
            "prediction_count": count,
        }
        for name, version, count in (await session.execute(stmt)).all()
    ]


@router.get("/summary")
async def get_executive_summary(session: AsyncSession = Depends(get_db_session)) -> Dict[str, Any]:
    """
    Returns executive summary metrics across the end-to-end engine.
    Money is reported per currency; the legacy scalar totals are null when currencies are mixed.
    Read-only internal/demo inspection endpoint.
    """
    # Total processed GMV (from auths), per currency
    auth_stmt = (
        select(PaymentEventModel.currency, func.sum(PaymentEventModel.amount_minor_units))
        .where(
            PaymentEventModel.event_type == 'payment.authorized',
            PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES),
        )
        .group_by(PaymentEventModel.currency)
        .order_by(PaymentEventModel.currency)
    )
    total_gmv_by_currency = [
        {"currency": currency, "amount_minor_units": int(amount or 0)}
        for currency, amount in (await session.execute(auth_stmt)).all()
    ]

    # Total protected GMV (latest attribution version per command), per currency
    total_protected_gmv_by_currency = await _protected_gmv_by_currency(session)

    # Active degradation episodes
    active_episodes_stmt = select(func.count(DegradationEpisodeModel.episode_id)).where(DegradationEpisodeModel.status == 'ACTIVE')
    active_episodes_res = await session.execute(active_episodes_stmt)
    active_episodes = active_episodes_res.scalar_one_or_none() or 0

    # Total interventions executed
    interventions_stmt = select(func.count(InterventionCommandModel.command_id)).where(InterventionCommandModel.command_status == 'SUCCEEDED')
    interventions_res = await session.execute(interventions_stmt)
    total_interventions = interventions_res.scalar_one_or_none() or 0

    # Which executor completed each SUCCEEDED command, judged by its latest attempt
    executor_stmt = (
        select(
            InterventionExecutionAttemptModel.executor_name,
            InterventionExecutionAttemptModel.is_simulation,
            func.count(func.distinct(InterventionExecutionAttemptModel.command_id)),
        )
        .join(
            InterventionCommandModel,
            InterventionCommandModel.command_id == InterventionExecutionAttemptModel.command_id,
        )
        .where(
            InterventionCommandModel.command_status == 'SUCCEEDED',
            InterventionExecutionAttemptModel.attempt_id.in_(_latest_attempt_ids()),
        )
        .group_by(InterventionExecutionAttemptModel.executor_name, InterventionExecutionAttemptModel.is_simulation)
        .order_by(InterventionExecutionAttemptModel.executor_name)
    )
    execution_provenance = [
        {"executor_name": name, "is_simulation": is_simulation, "succeeded_command_count": count}
        for name, is_simulation, count in (await session.execute(executor_stmt)).all()
    ]
    simulated_interventions = sum(
        e["succeeded_command_count"] for e in execution_provenance if e["is_simulation"]
    )
    
    # Successful payment capture rate (overall)
    all_events_stmt = (
        select(PaymentEventModel.event_type, func.count(PaymentEventModel.event_id))
        .where(PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES))
        .group_by(PaymentEventModel.event_type)
    )
    all_events_res = await session.execute(all_events_stmt)
    event_counts = dict(all_events_res.all())
    
    total_auths = event_counts.get('payment.authorized', 0)
    total_captures = event_counts.get('payment.captured', 0)
    success_rate = (total_captures / total_auths) if total_auths > 0 else 0.0

    return {
        "total_gmv_minor_units": _single_currency_amount(total_gmv_by_currency),
        "total_gmv_by_currency": total_gmv_by_currency,
        "total_protected_gmv_minor_units": _single_currency_amount(total_protected_gmv_by_currency),
        "total_protected_gmv_by_currency": total_protected_gmv_by_currency,
        "active_degradation_episodes": active_episodes,
        "total_successful_interventions": total_interventions,
        "simulated_successful_interventions": simulated_interventions,
        "execution_provenance": execution_provenance,
        "prediction_models": await _prediction_models(session),
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

    episode_decisions = (
        InterventionDecisionModel.evaluation_audit_payload["upstream_context"]["episode_id"].as_string() == ep_str
    )
    window_protected_gmv_by_currency = await _protected_gmv_by_currency(
        session, episode_decisions, join_decisions=True
    )
    episode_prediction_models = await _prediction_models(session, episode_decisions, join_decisions=True)
    
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
            "prediction_status": "PREDICTED" if pred_count else "NO_PREDICTIONS",
            "prediction_models": episode_prediction_models,
        },
        "intervention_context": {
            "act_decisions": dec_counts.get("ACT", 0),
            "monitor_decisions": dec_counts.get("MONITOR", 0),
            "primary_selected_route": primary_route,
            "decision_threshold": get_default_policy().act_risk_threshold,
            "window_protected_gmv_minor_units": _single_currency_amount(window_protected_gmv_by_currency),
            "window_protected_gmv_by_currency": window_protected_gmv_by_currency,
        }
    }

@router.get("/attributions")
async def get_recent_attributions(
    limit: int = Query(default=50, ge=1, le=500, description="Maximum number of attributions to return"),
    include_history: bool = Query(
        default=False, description="Include superseded attribution versions instead of only the latest per command"
    ),
    session: AsyncSession = Depends(get_db_session)
) -> List[Dict[str, Any]]:
    """
    Returns recent protected GMV attributions, by default only the latest version per command.
    Safe bounded read-only inspection endpoint.
    """
    currency = _payment_currency()
    stmt = (
        select(CounterfactualAttributionModel, currency.c.currency)
        .outerjoin(currency, currency.c.payment_id == CounterfactualAttributionModel.payment_attempt_id)
        .order_by(desc(CounterfactualAttributionModel.created_at))
        .limit(limit)
    )
    if not include_history:
        stmt = stmt.where(CounterfactualAttributionModel.attribution_id.in_(_latest_attribution_ids()))
    res = await session.execute(stmt)

    results = []
    for a, attribution_currency in res.all():
        payload = a.attribution_audit_payload if isinstance(a.attribution_audit_payload, dict) else {}
        evidence = payload.get("evidence_score_components", {})
        
        c_pred = float(evidence["c_prediction"]) if "c_prediction" in evidence else None
        c_diag = float(evidence["c_diagnosis"]) if "c_diagnosis" in evidence else None
        c_time = float(evidence["c_timing"]) if "c_timing" in evidence else None
        c_prov = float(evidence["c_provenance"]) if "c_provenance" in evidence else None

        results.append({
            "attribution_id": str(a.attribution_id),
            "attribution_version": a.attribution_version,
            "payment_attempt_id": a.payment_attempt_id,
            "decision_id": str(a.decision_id),
            "status": a.attribution_status,
            "treatment_status": a.treatment_status,
            "observed_payment_outcome": a.observed_payment_outcome,
            "counterfactual_outcome": "WOULD_HAVE_FAILED" if a.attribution_status == "ATTRIBUTED" else a.attribution_status,
            "currency": attribution_currency,
            "payment_amount_minor_units": a.payment_amount_minor_units,
            "is_simulated_execution": a.is_simulated_execution,
            "is_synthetic_baseline": a.is_synthetic_baseline,
            "stage5_model_id": payload.get("stage5_model_id"),
            "stage5_model_provenance": payload.get("stage5_model_provenance"),
            "methodology_name": a.methodology_name,
            "methodology_version": a.methodology_version,
            "counterfactual_failure_probability": float(a.counterfactual_failure_probability) if a.counterfactual_failure_probability is not None else None,
            "counterfactual_loss_exposure_minor_units": a.counterfactual_loss_exposure_minor_units,
            "attributed_protected_gmv_minor_units": a.attributed_protected_gmv_minor_units,
            "attribution_confidence": float(a.attribution_confidence),
            "attributed_at": a.attributed_at.isoformat(),
            "confidence_components": {
                "c_prediction": c_pred,
                "c_diagnosis": c_diag,
                "c_timing": c_time,
                "c_provenance": c_prov,
            }
        })
    return results

@router.get("/recovery")
async def get_recovery_workspace(
    decision_type: Optional[str] = Query(default=None, description="Filter by decision type: ACT, MONITOR"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session)
) -> Dict[str, Any]:
    """
    Returns operational recovery workspace telemetry:
    - High-level recovery and attribution summary metrics
    - Dense recovery ledger rows linking decisions, execution commands, observations, and attributions.
    Safe bounded read-only inspection endpoint.
    """
    # 1. Summary aggregations
    cmd_count = await session.scalar(select(func.count(InterventionCommandModel.command_id))) or 0
    act_count = await session.scalar(select(func.count(InterventionDecisionModel.decision_id)).where(InterventionDecisionModel.decision_type == "ACT")) or 0
    mon_count = await session.scalar(select(func.count(InterventionDecisionModel.decision_id)).where(InterventionDecisionModel.decision_type == "MONITOR")) or 0
    latest_observation = PaymentOutcomeObservationModel.observation_id.in_(_latest_observation_ids())
    latest_attribution = CounterfactualAttributionModel.attribution_id.in_(_latest_attribution_ids())
    latest_attempt = InterventionExecutionAttemptModel.attempt_id.in_(_latest_attempt_ids())
    cap_count = await session.scalar(select(func.count(PaymentOutcomeObservationModel.observation_id)).where(PaymentOutcomeObservationModel.payment_outcome == "CAPTURED", latest_observation)) or 0
    fail_count = await session.scalar(select(func.count(PaymentOutcomeObservationModel.observation_id)).where(PaymentOutcomeObservationModel.payment_outcome == "FAILED", latest_observation)) or 0
    protected_gmv_by_currency = await _protected_gmv_by_currency(session)
    attr_count = await session.scalar(select(func.count(CounterfactualAttributionModel.attribution_id)).where(latest_attribution)) or 0
    avg_conf = await session.scalar(select(func.avg(CounterfactualAttributionModel.attribution_confidence)).where(latest_attribution)) or 0.0
    simulated_execution_count = await session.scalar(
        select(func.count(InterventionExecutionAttemptModel.attempt_id)).where(
            latest_attempt, InterventionExecutionAttemptModel.is_simulation.is_(True)
        )
    ) or 0

    rescued_stmt = select(
        func.count(CounterfactualAttributionModel.attribution_id),
        func.avg(CounterfactualAttributionModel.attribution_confidence)
    ).where(CounterfactualAttributionModel.attributed_protected_gmv_minor_units > 0, latest_attribution)
    rescued_res = await session.execute(rescued_stmt)
    rescued_row = rescued_res.fetchone()
    rescued_count = rescued_row[0] if rescued_row else 0
    rescued_avg_conf = float(rescued_row[1]) if (rescued_row and rescued_row[1] is not None) else 0.0

    summary = {
        "interventions_dispatched": cmd_count,
        "simulated_execution_count": simulated_execution_count,
        "act_count": act_count,
        "monitor_count": mon_count,
        "successful_captures": cap_count,
        "failed_outcomes": fail_count,
        "total_protected_gmv_minor_units": _single_currency_amount(protected_gmv_by_currency),
        "total_protected_gmv_by_currency": protected_gmv_by_currency,
        "attribution_count": attr_count,
        "avg_attribution_confidence": float(avg_conf),
        "rescued_count": rescued_count,
        "rescued_avg_confidence": rescued_avg_conf,
    }

    # 2. Ledger Query
    ledger_currency = _payment_currency()
    base_query = (
        select(
            InterventionDecisionModel.decision_id,
            InterventionDecisionModel.payment_attempt_id,
            InterventionDecisionModel.decision_type,
            InterventionDecisionModel.selected_route_id,
            InterventionDecisionModel.decided_at,
            InterventionDecisionModel.evaluation_audit_payload,
            InterventionCommandModel.command_id,
            InterventionCommandModel.command_status,
            InterventionCommandModel.created_at.label("command_created_at"),
            PaymentOutcomeObservationModel.payment_outcome,
            PaymentOutcomeObservationModel.observed_at,
            CounterfactualAttributionModel.attribution_id,
            CounterfactualAttributionModel.attributed_protected_gmv_minor_units,
            CounterfactualAttributionModel.attribution_confidence,
            CounterfactualAttributionModel.attribution_status,
            CounterfactualAttributionModel.is_simulated_execution,
            CounterfactualAttributionModel.is_synthetic_baseline,
            InterventionExecutionAttemptModel.executor_name,
            InterventionExecutionAttemptModel.is_simulation,
            ledger_currency.c.currency,
        )
        .select_from(InterventionDecisionModel)
        .outerjoin(InterventionCommandModel, InterventionCommandModel.decision_id == InterventionDecisionModel.decision_id)
        # Latest versions only, so a re-observed or re-attributed command stays one ledger row.
        .outerjoin(
            PaymentOutcomeObservationModel,
            and_(PaymentOutcomeObservationModel.command_id == InterventionCommandModel.command_id, latest_observation),
        )
        .outerjoin(
            CounterfactualAttributionModel,
            and_(CounterfactualAttributionModel.decision_id == InterventionDecisionModel.decision_id, latest_attribution),
        )
        .outerjoin(
            InterventionExecutionAttemptModel,
            and_(InterventionExecutionAttemptModel.command_id == InterventionCommandModel.command_id, latest_attempt),
        )
        .outerjoin(ledger_currency, ledger_currency.c.payment_id == InterventionDecisionModel.payment_attempt_id)
    )

    count_query = select(func.count(InterventionDecisionModel.decision_id))

    if decision_type:
        dt_upper = decision_type.upper()
        base_query = base_query.where(InterventionDecisionModel.decision_type == dt_upper)
        count_query = count_query.where(InterventionDecisionModel.decision_type == dt_upper)

    total_ledger_count = await session.scalar(count_query) or 0

    ledger_stmt = base_query.order_by(desc(InterventionDecisionModel.decided_at)).offset(offset).limit(limit)
    ledger_res = await session.execute(ledger_stmt)
    rows = ledger_res.all()

    ledger = []
    for r in rows:
        payload = r.evaluation_audit_payload if isinstance(r.evaluation_audit_payload, dict) else {}
        upstream = payload.get("upstream_context", {})
        ep_id = upstream.get("episode_id")

        outcome_status = r.payment_outcome or "NOT_OBSERVED"

        if r.attributed_protected_gmv_minor_units and r.attributed_protected_gmv_minor_units > 0:
            attr_status = "ATTRIBUTED"
        elif r.attribution_status:
            attr_status = "NOT_ATTRIBUTED"
        else:
            attr_status = "NOT_ATTRIBUTABLE"

        ledger.append({
            "decision_id": str(r.decision_id),
            "payment_attempt_id": r.payment_attempt_id,
            "episode_id": ep_id,
            "decision_type": r.decision_type,
            "intervention_route": r.selected_route_id,
            "decided_at": r.decided_at.isoformat(),
            "execution_status": r.command_status or "NOT_DISPATCHED",
            "executed_at": r.command_created_at.isoformat() if r.command_created_at else None,
            "executor_name": r.executor_name,
            "is_simulation": r.is_simulation,
            "observed_outcome": outcome_status,
            "observed_at": r.observed_at.isoformat() if r.observed_at else None,
            "currency": r.currency,
            "attributed_protected_gmv_minor_units": r.attributed_protected_gmv_minor_units or 0,
            "attribution_confidence": float(r.attribution_confidence) if r.attribution_confidence is not None else None,
            "attribution_status": attr_status,
            "is_simulated_execution": r.is_simulated_execution,
            "is_synthetic_baseline": r.is_synthetic_baseline,
        })

    return {
        "summary": summary,
        "ledger": ledger,
        "total_count": total_ledger_count,
        "limit": limit,
        "offset": offset,
    }

@router.get("/payments")
async def get_payments_list(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: Optional[str] = Query(default=None, description="Search by payment_id"),
    filter_type: Optional[str] = Query(default=None, description="ALL, INTERVENED, ATTRIBUTED"),
    status: Optional[str] = Query(default=None, description="captured, failed, authorized"),
    session: AsyncSession = Depends(get_db_session)
) -> Dict[str, Any]:
    """
    Returns a searchable and filterable list of payments for the Payment Audit Inspector.
    Safe bounded read-only inspection endpoint.
    """
    terminal_subq = (
        select(
            PaymentEventModel.payment_id,
            func.max(PaymentEventModel.payment_status).label("terminal_status")
        )
        .where(
            PaymentEventModel.event_type.in_(["payment.captured", "payment.failed"]),
            PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES),
        )
        .group_by(PaymentEventModel.payment_id)
        .subquery()
    )

    canonical_events_stmt = (
        select(PaymentEventModel.event_id)
        .where(PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES))
        .distinct(PaymentEventModel.payment_id)
        .order_by(PaymentEventModel.payment_id, PaymentEventModel.timestamp.asc())
    )

    base_query = (
        select(
            PaymentEventModel.payment_id,
            PaymentEventModel.timestamp,
            PaymentEventModel.amount_minor_units,
            PaymentEventModel.currency,
            PaymentEventModel.payment_method,
            PaymentEventModel.bank,
            PaymentEventModel.wallet,
            PaymentEventModel.payment_status,
            terminal_subq.c.terminal_status,
            InterventionDecisionModel.decision_type,
            InterventionDecisionModel.decision_id,
            CounterfactualAttributionModel.attribution_id,
            CounterfactualAttributionModel.attributed_protected_gmv_minor_units,
            CounterfactualAttributionModel.attribution_confidence,
            CounterfactualAttributionModel.attribution_status,
            CounterfactualAttributionModel.is_simulated_execution,
            CounterfactualAttributionModel.is_synthetic_baseline,
        )
        .select_from(PaymentEventModel)
    )
    count_query = select(func.count(PaymentEventModel.event_id)).select_from(PaymentEventModel)

    # One row per payment, as in the timeline: its latest decision and that decision's latest attribution.
    latest_decision = InterventionDecisionModel.decision_id.in_(_latest_decision_ids())
    latest_attribution = CounterfactualAttributionModel.attribution_id.in_(_latest_attribution_ids())
    base_query, count_query = (
        query
        .outerjoin(terminal_subq, terminal_subq.c.payment_id == PaymentEventModel.payment_id)
        .outerjoin(
            InterventionDecisionModel,
            and_(InterventionDecisionModel.payment_attempt_id == PaymentEventModel.payment_id, latest_decision),
        )
        .outerjoin(
            CounterfactualAttributionModel,
            and_(CounterfactualAttributionModel.decision_id == InterventionDecisionModel.decision_id, latest_attribution),
        )
        .where(PaymentEventModel.event_id.in_(canonical_events_stmt))
        for query in (base_query, count_query)
    )

    if search:
        s_clean = search.strip()
        base_query = base_query.where(PaymentEventModel.payment_id.ilike(f"%{s_clean}%"))
        count_query = count_query.where(PaymentEventModel.payment_id.ilike(f"%{s_clean}%"))

    if filter_type:
        ft_upper = filter_type.upper()
        if ft_upper == "INTERVENED":
            base_query = base_query.where(InterventionDecisionModel.decision_type.isnot(None))
            count_query = count_query.where(InterventionDecisionModel.decision_type.isnot(None))
        elif ft_upper == "ATTRIBUTED":
            base_query = base_query.where(
                CounterfactualAttributionModel.attribution_id.isnot(None),
                CounterfactualAttributionModel.attribution_status == "ATTRIBUTED"
            )
            count_query = count_query.where(
                CounterfactualAttributionModel.attribution_id.isnot(None),
                CounterfactualAttributionModel.attribution_status == "ATTRIBUTED"
            )

    if status:
        st_lower = status.lower()
        base_query = base_query.where(
            func.coalesce(terminal_subq.c.terminal_status, PaymentEventModel.payment_status) == st_lower
        )
        count_query = count_query.where(
            func.coalesce(terminal_subq.c.terminal_status, PaymentEventModel.payment_status) == st_lower
        )

    total_count = await session.scalar(count_query) or 0

    stmt = base_query.order_by(desc(PaymentEventModel.timestamp)).offset(offset).limit(limit)
    res = await session.execute(stmt)
    rows = res.all()

    items = []
    for r in rows:
        final_st = r.terminal_status or r.payment_status
        is_attr = r.attribution_id is not None and r.attribution_status == "ATTRIBUTED"
        items.append({
            "payment_id": r.payment_id,
            "timestamp": r.timestamp.isoformat(),
            "amount_minor_units": r.amount_minor_units,
            "currency": r.currency,
            "payment_method": r.payment_method,
            "bank": r.bank,
            "wallet": r.wallet,
            "payment_status": final_st,
            "decision_type": r.decision_type,
            "decision_id": str(r.decision_id) if r.decision_id else None,
            "is_intervened": r.decision_type is not None,
            "is_attributed": is_attr,
            "attributed_protected_gmv_minor_units": (r.attributed_protected_gmv_minor_units or 0) if is_attr else 0,
            "attribution_confidence": float(r.attribution_confidence) if (is_attr and r.attribution_confidence is not None) else None,
            "is_simulated_execution": r.is_simulated_execution if is_attr else None,
            "is_synthetic_baseline": r.is_synthetic_baseline if is_attr else None,
        })

    return {
        "items": items,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }

@router.get("/timeline/{payment_id}")
async def get_payment_timeline(payment_id: str, session: AsyncSession = Depends(get_db_session)) -> Dict[str, Any]:
    """
    Returns an end-to-end chronological lifecycle trace across Stages 1–8 for a specific payment.
    Read-only inspection endpoint.
    Preserves canonical payment identity strictly originating in PaymentEventModel.
    """
    clean_pid = payment_id.strip()

    # 1. Canonical Payment Events Lookup:
    # A payment's domain identity strictly originates in PaymentEventModel.payment_id.
    events_stmt = (
        select(PaymentEventModel)
        .where(
            PaymentEventModel.payment_id == clean_pid,
            PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES),
        )
        .order_by(PaymentEventModel.timestamp)
    )
    events_res = await session.execute(events_stmt)
    events = events_res.scalars().all()

    if not events:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment {payment_id} not found"
        )

    first_event = events[0]
    canonical_pid = first_event.payment_id
    payment_info = {
        "amount_minor_units": first_event.amount_minor_units,
        "currency": first_event.currency,
        "payment_method": first_event.payment_method,
        "bank": first_event.bank,
        "wallet": first_event.wallet,
        "terminal_status": events[-1].payment_status,
    }

    # 2. Prediction
    pred_stmt = (
        select(FailurePredictionModel)
        .where(FailurePredictionModel.payment_attempt_id == clean_pid)
        .order_by(desc(FailurePredictionModel.prediction_version))
        .limit(1)
    )
    pred_res = await session.execute(pred_stmt)
    prediction = pred_res.scalar_one_or_none()

    # 3. Decision
    dec_stmt = (
        select(InterventionDecisionModel)
        .where(InterventionDecisionModel.payment_attempt_id == clean_pid)
        .order_by(desc(InterventionDecisionModel.decision_version))
        .limit(1)
    )
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
            exec_stmt = (
                select(InterventionExecutionAttemptModel)
                .where(InterventionExecutionAttemptModel.command_id == command.command_id)
                .order_by(desc(InterventionExecutionAttemptModel.attempt_number))
                .limit(1)
            )
            exec_res = await session.execute(exec_stmt)
            execution = exec_res.scalar_one_or_none()

            # 6. Observation
            obs_stmt = (
                select(PaymentOutcomeObservationModel)
                .where(PaymentOutcomeObservationModel.command_id == command.command_id)
                .order_by(desc(PaymentOutcomeObservationModel.observation_version))
                .limit(1)
            )
            obs_res = await session.execute(obs_stmt)
            observation = obs_res.scalar_one_or_none()

            # 7. Attribution (from command)
            attr_stmt = (
                select(CounterfactualAttributionModel)
                .where(CounterfactualAttributionModel.command_id == command.command_id)
                .order_by(desc(CounterfactualAttributionModel.attribution_version))
                .limit(1)
            )
            attr_res = await session.execute(attr_stmt)
            attribution = attr_res.scalar_one_or_none()

    return {
        "payment_id": canonical_pid,
        "payment_info": payment_info,
        "events": [
            {
                "event_type": e.event_type,
                "timestamp": e.timestamp.isoformat(),
                "status": e.payment_status,
                "amount_minor_units": e.amount_minor_units
            } for e in events
        ],
        "prediction": {
            "prediction_id": str(prediction.prediction_id),
            "status": prediction.prediction_status,
            "failure_probability": prediction.failure_probability,
            "risk_band": prediction.risk_band,
            "predicted_at": prediction.predicted_at.isoformat(),
            "model_name": prediction.model_name,
            "model_version": prediction.model_version,
            "model_provenance": classify_model_provenance(prediction.model_name, prediction.model_version).value,
        } if prediction else None,
        "decision": {
            "decision_id": str(decision.decision_id),
            "type": decision.decision_type,
            "route": decision.selected_route_id,
            "gate_verdict": decision.gate_verdict,
            "decided_at": decision.decided_at.isoformat(),
            "failure_probability": decision.failure_probability,
            "episode_id": decision.evaluation_audit_payload.get("upstream_context", {}).get("episode_id") if decision.evaluation_audit_payload else None,
        } if decision else None,
        "command": {
            "command_id": str(command.command_id),
            "status": command.command_status,
            "route": command.route_key,
            "created_at": command.created_at.isoformat()
        } if command else None,
        "execution": {
            "attempt_id": str(execution.attempt_id),
            "status": execution.execution_status,
            "duration_ms": execution.duration_ms,
            "executed_at": execution.completed_at.isoformat() if execution.completed_at else None,
            "executor_name": execution.executor_name,
            "is_simulation": execution.is_simulation,
        } if execution else None,
        "observation": {
            "observation_id": str(observation.observation_id),
            "outcome": observation.payment_outcome,
            "observed_at": observation.observed_at.isoformat() if observation.observed_at else None
        } if observation else None,
        "attribution": {
            "attribution_id": str(attribution.attribution_id),
            "status": attribution.attribution_status,
            "protected_gmv": attribution.attributed_protected_gmv_minor_units if attribution.attribution_status == "ATTRIBUTED" else 0,
            "confidence": float(attribution.attribution_confidence) if (attribution.attribution_status == "ATTRIBUTED" and attribution.attribution_confidence is not None) else None,
            "counterfactual_outcome": "WOULD_HAVE_FAILED" if attribution.attribution_status == "ATTRIBUTED" else attribution.attribution_status,
            "counterfactual_failure_probability": float(attribution.counterfactual_failure_probability) if attribution.counterfactual_failure_probability is not None else None,
            "attributed_at": attribution.attributed_at.isoformat() if attribution.attributed_at else None,
            "attribution_version": attribution.attribution_version,
            "is_simulated_execution": attribution.is_simulated_execution,
            "is_synthetic_baseline": attribution.is_synthetic_baseline,
            "stage5_model_provenance": (attribution.attribution_audit_payload or {}).get("stage5_model_provenance"),
            "methodology_name": attribution.methodology_name,
            "methodology_version": attribution.methodology_version,
            "confidence_components": attribution.attribution_audit_payload.get("evidence_score_components", {}) if attribution.attribution_audit_payload else {}
        } if attribution else None
    }
