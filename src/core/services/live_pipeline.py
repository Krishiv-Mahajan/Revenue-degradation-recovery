"""
Stage 1–8 Live Pipeline Adapter.

Connects newly ingested Razorpay PaymentEvents to the existing frozen recovery pipeline.
Reuses canonical services, models, and repositories without altering any validated
mathematical behavior, thresholds, or domain logic.
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database import AsyncSessionLocal
from src.core.domain.models import PaymentEvent
from src.infrastructure.models import (
    InterventionCommandModel,
)

# Repositories
from src.infrastructure.analytics_repository import AnalyticsRepository
from src.infrastructure.degradation_repository import DegradationRepository
from src.infrastructure.rca_repository import RCARepository
from src.infrastructure.failure_prediction_repository import FailurePredictionRepository
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.infrastructure.intervention_repository import InterventionRepository
from src.infrastructure.execution_repository import ExecutionRepository
from src.infrastructure.attribution_repository import AttributionRepository

# Core Domain Services
from src.core.analytics.service import PaymentHealthAnalyticsService
from src.core.attribution.attribution_service import CounterfactualAttributionService
from src.core.degradation.service import DegradationService
from src.core.domain.analytics_models import PaymentHealthSnapshot
from src.core.domain.degradation_models import DegradationEpisode, EpisodeStatus, Severity
from src.core.execution.execution_service import InterventionExecutionService
from src.core.execution.observation_service import PaymentOutcomeObservationService
from src.core.execution.simulator import SimulatorInterventionExecutor, SimulatorMode
from src.core.intervention.policy import get_default_policy
from src.core.intervention.service import InterventionDecisionService
from src.core.ml.model import get_development_model
from src.core.rca.service import RCAService
from src.core.services.failure_prediction_service import FailurePredictionService
from src.core.services.feature_reconstruction_service import FeatureReconstructionService

logger = logging.getLogger("live_pipeline")


async def run_live_pipeline(
    payment_event: PaymentEvent,
    session: Optional[AsyncSession] = None,
) -> Dict[str, Any]:
    """
    Executes Stages 2–8 for a newly ingested PaymentEvent.
    If session is not provided, uses an isolated AsyncSessionLocal context.
    Ensures error isolation so pipeline exceptions do not corrupt or rollback
    the already-committed webhook ingestion record.
    """
    if session is not None:
        return await _execute_live_pipeline(session, payment_event)
    else:
        async with AsyncSessionLocal() as local_session:
            return await _execute_live_pipeline(local_session, payment_event)


async def _execute_live_pipeline(
    session: AsyncSession,
    payment_event: PaymentEvent,
) -> Dict[str, Any]:
    current_stage = "INIT"
    pid = payment_event.payment_id

    try:
        # 1. Repositories
        analytics_repo = AnalyticsRepository(session)
        degradation_repo = DegradationRepository(session)
        rca_repo = RCARepository(session)
        pred_repo = FailurePredictionRepository(session)
        feat_repo = FeatureReconstructionRepository(session)
        inter_repo = InterventionRepository(session)
        exec_repo = ExecutionRepository(session)
        attr_repo = AttributionRepository(session)

        # 2. Canonical Services
        analytics_svc = PaymentHealthAnalyticsService(analytics_repo)
        degradation_svc = DegradationService(degradation_repo)
        rca_svc = RCAService(session, rca_repo)

        model = get_development_model(mode="deterministic")
        feat_svc = FeatureReconstructionService(feat_repo)
        pred_svc = FailurePredictionService(session, feat_svc, pred_repo, model)

        demo_policy = get_default_policy()
        inter_decision_svc = InterventionDecisionService(session, inter_repo, feat_repo, demo_policy)

        # Stage 7 Simulator: strictly SIMULATOR_V1 / simulation mode
        executor = SimulatorInterventionExecutor(SimulatorMode.ALWAYS_SUCCEED)
        inter_exec_svc = InterventionExecutionService(session, exec_repo, executor)
        outcome_obs_svc = PaymentOutcomeObservationService(session, exec_repo)
        attr_svc = CounterfactualAttributionService(attr_repo)

        event_ts = payment_event.timestamp
        if event_ts.tzinfo is None:
            event_ts = event_ts.replace(tzinfo=timezone.utc)

        # Determine 5-minute tumbling window for Stage 2
        minute = event_ts.minute
        window_start_minute = (minute // 5) * 5
        window_start = event_ts.replace(minute=window_start_minute, second=0, microsecond=0)
        window_end = window_start + timedelta(minutes=5)

        # -------------------------------------------------------------------
        # Stage 2: Payment Health Analytics
        # -------------------------------------------------------------------
        current_stage = "STAGE_2_ANALYTICS"
        await analytics_svc.calculate_and_save_window(window_start, window_end)

        # -------------------------------------------------------------------
        # Stage 3: Degradation Detection
        # -------------------------------------------------------------------
        current_stage = "STAGE_3_DEGRADATION"
        snap_stmt = text(
            """
            SELECT snapshot_id, window_start, window_end, segment_dimension, segment_value, 
                   transaction_count, successful_transaction_count, failed_transaction_count, 
                   success_rate, failure_rate, total_gmv_minor_units, successful_gmv_minor_units, 
                   failed_gmv_minor_units, baseline_success_rate, insufficient_volume, calculated_at
            FROM payment_health_snapshots 
            WHERE window_start >= :w_start AND window_end <= :w_end
            """
        )
        snap_res = await session.execute(snap_stmt, {"w_start": window_start, "w_end": window_end})
        snapshots = [
            PaymentHealthSnapshot(
                snapshot_id=row.snapshot_id,
                window_start=row.window_start,
                window_end=row.window_end,
                segment_dimension=row.segment_dimension,
                segment_value=row.segment_value,
                transaction_count=row.transaction_count,
                successful_transaction_count=row.successful_transaction_count,
                failed_transaction_count=row.failed_transaction_count,
                success_rate=row.success_rate,
                failure_rate=row.failure_rate,
                total_gmv_minor_units=row.total_gmv_minor_units,
                successful_gmv_minor_units=row.successful_gmv_minor_units,
                failed_gmv_minor_units=row.failed_gmv_minor_units,
                baseline_success_rate=row.baseline_success_rate,
                insufficient_volume=row.insufficient_volume,
                calculated_at=row.calculated_at,
            )
            for row in snap_res.fetchall()
        ]

        by_segment: Dict[Tuple[str, str], List[PaymentHealthSnapshot]] = defaultdict(list)
        for s in snapshots:
            by_segment[(s.segment_dimension, s.segment_value)].append(s)
        for seg_snapshots in by_segment.values():
            await degradation_svc.process_snapshots(seg_snapshots, evaluation_timestamp=event_ts)

        # -------------------------------------------------------------------
        # Stage 4: Root Cause Analysis (RCA)
        # -------------------------------------------------------------------
        current_stage = "STAGE_4_RCA"
        ep_stmt = text(
            """
            SELECT episode_id, segment_dimension, segment_value, started_at_window, ended_at_window, status, peak_absolute_drop, affected_window_count, severity
            FROM degradation_episodes
            WHERE status = 'ACTIVE'
            """
        )
        ep_res = await session.execute(ep_stmt)
        for row in ep_res.fetchall():
            ep = DegradationEpisode(
                episode_id=row.episode_id,
                segment_dimension=row.segment_dimension,
                segment_value=row.segment_value,
                started_at_window=row.started_at_window,
                ended_at_window=row.ended_at_window,
                status=EpisodeStatus(row.status),
                peak_absolute_drop=row.peak_absolute_drop,
                affected_window_count=row.affected_window_count,
                severity=Severity(row.severity),
            )
            await rca_svc.evaluate_episode(ep)

        await session.commit()

        # -------------------------------------------------------------------
        # Stages 5–8: Failure Prediction, Decision, Execution, Attribution
        # -------------------------------------------------------------------
        t_eval = max(event_ts, payment_event.ingested_at)
        if t_eval.tzinfo is None:
            t_eval = t_eval.replace(tzinfo=timezone.utc)

        prediction = None
        decision = None
        command = None
        observation = None
        attribution = None

        if payment_event.event_type == "payment.authorized":
            # Stage 5: Failure Prediction
            current_stage = "STAGE_5_PREDICTION"
            prediction = await pred_svc.orchestrate_prediction(pid, T=t_eval)

            # Stage 6: Intervention Decisioning
            current_stage = "STAGE_6_DECISION"
            if prediction.failure_probability is not None and prediction.failure_probability > 0.5:
                decision = await inter_decision_svc.decide(pid, t_decide=t_eval)

                if decision.decision_type.value == "ACT":
                    # Stage 7A: Execution Command Creation & Dispatch (Claim immediately at t_eval)
                    current_stage = "STAGE_7_EXECUTION"
                    command = await inter_exec_svc.create_command(decision, t_create=t_eval)
                    await inter_exec_svc.execute_command(command.command_id, t_claim=t_eval)

                    # Note: Stage 7B observation and Stage 8 attribution are NOT performed here.
                    # An authorized payment is still in flight and must not be prematurely observed/attributed.

        elif payment_event.event_type in ("payment.captured", "payment.failed"):
            # Terminal event: observe outcome & attribute using authoritative terminal boundary
            cmd_stmt = select(InterventionCommandModel).where(
                InterventionCommandModel.payment_attempt_id == pid
            )
            cmd_res = await session.execute(cmd_stmt)
            existing_commands = cmd_res.scalars().all()
            for cmd_model in existing_commands:
                cmd_updated = cmd_model.updated_at
                if cmd_updated and cmd_updated.tzinfo is None:
                    cmd_updated = cmd_updated.replace(tzinfo=timezone.utc)
                terminal_ingested = payment_event.ingested_at
                if terminal_ingested.tzinfo is None:
                    terminal_ingested = terminal_ingested.replace(tzinfo=timezone.utc)

                as_of_terminal = max(terminal_ingested, cmd_updated) if cmd_updated else terminal_ingested

                current_stage = "STAGE_7_OBSERVATION_TERMINAL"
                observation = await outcome_obs_svc.observe(cmd_model.command_id, as_of_timestamp=as_of_terminal)
                current_stage = "STAGE_8_ATTRIBUTION_TERMINAL"
                attribution = await attr_svc.attribute_payment_intervention(cmd_model.command_id, as_of_timestamp=as_of_terminal)

        await session.commit()
        logger.info(
            f"[LivePipelineSuccess] source_event_id={payment_event.source_event_id} "
            f"payment_id={pid} event_type={payment_event.event_type} "
            f"pred={prediction.failure_probability if prediction else None} "
            f"act={decision.decision_type.value if decision else None} "
            f"cmd={command.command_id if command else None}"
        )
        return {
            "status": "success",
            "payment_id": pid,
            "prediction": prediction,
            "decision": decision,
            "command": command,
            "observation": observation,
            "attribution": attribution,
        }

    except Exception as e:
        await session.rollback()
        logger.error(
            f"[LivePipelineError] source_event_id={payment_event.source_event_id} "
            f"payment_id={pid} stage={current_stage} "
            f"exception={type(e).__name__}: {str(e)}",
            exc_info=True,
        )
        return {
            "status": "failed",
            "payment_id": pid,
            "failed_stage": current_stage,
            "error": str(e),
        }
