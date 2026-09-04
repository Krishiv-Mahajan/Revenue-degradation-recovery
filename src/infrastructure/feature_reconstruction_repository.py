import uuid
from typing import Optional, Tuple, Dict
from datetime import datetime, timedelta
from sqlalchemy import select, and_, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import (
    PaymentEventModel, 
    EpisodeStateHistoryModel, 
    CandidateCauseModel, 
    RCAEvaluationModel
)

class FeatureReconstructionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_payment_event_by_id(self, payment_id: str, t: datetime) -> Optional[PaymentEventModel]:
        """
        Get the triggering payment.authorized event, ensuring it was ingested before T.
        Actually T IS the ingested_at of the payment.authorized event, so we look for exactly that event.
        But for safety, we just fetch the payment.authorized event for payment_id where ingested_at <= T.
        """
        stmt = (
            select(PaymentEventModel)
            .where(
                and_(
                    PaymentEventModel.payment_id == payment_id,
                    PaymentEventModel.event_type == 'payment.authorized',
                    PaymentEventModel.ingested_at <= t
                )
            )
            .order_by(PaymentEventModel.ingested_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
        
    async def get_historical_failure_rate(self, dimension_col, dimension_value: str, t: datetime, window_minutes: int) -> Tuple[int, int]:
        """
        Returns (transaction_count, failure_count) for the given dimension and window, strictly before T.
        """
        window_start = t - timedelta(minutes=window_minutes)
        
        # We need to count the terminal events (captured and failed)
        # where timestamp is in the window, and ingested_at < T.
        # But wait, we count total terminal events to calculate failure rate.
        # Or do we count the authorized events and see if they failed? 
        # Stage 5 design says: "For same payment_method: proportion of failures in events before T".
        # This usually means counting terminal events (payment.failed vs payment.captured) 
        # that were ingested before T and occurred in the window.
        stmt = (
            select(
                func.count().label('total_terminals'),
                func.sum(
                    case(
                        (PaymentEventModel.event_type == 'payment.failed', 1),
                        else_=0
                    )
                ).label('failures')
            )
            .where(
                and_(
                    dimension_col == dimension_value,
                    PaymentEventModel.ingested_at < t,
                    PaymentEventModel.timestamp >= window_start,
                    PaymentEventModel.timestamp < t,
                    PaymentEventModel.event_type.in_(['payment.captured', 'payment.failed'])
                )
            )
        )
        result = await self.session.execute(stmt)
        row = result.first()
        if not row:
            return 0, 0
        total = row.total_terminals or 0
        failures = row.failures or 0
        return total, failures

    async def get_active_episode_context(self, segment_dimension: str, segment_value: str, t: datetime) -> Optional[EpisodeStateHistoryModel]:
        """
        Implements the strict 2-step Stage 3 historical reconstruction algorithm.
        1. Find latest reconciliation_run_id <= T
        2. Find ACTIVE episode within that run
        """
        # Step 1
        run_id_stmt = (
            select(EpisodeStateHistoryModel.reconciliation_run_id)
            .where(
                and_(
                    func.upper(EpisodeStateHistoryModel.segment_dimension) == segment_dimension.upper(),
                    EpisodeStateHistoryModel.segment_value == segment_value,
                    EpisodeStateHistoryModel.evaluation_timestamp <= t
                )
            )
            .order_by(EpisodeStateHistoryModel.evaluation_timestamp.desc())
            .limit(1)
        )
        
        run_id_result = await self.session.execute(run_id_stmt)
        run_id = run_id_result.scalar_one_or_none()
        
        if not run_id:
            return None
            
        # Step 2
        active_stmt = (
            select(EpisodeStateHistoryModel)
            .where(
                and_(
                    EpisodeStateHistoryModel.reconciliation_run_id == run_id,
                    func.upper(EpisodeStateHistoryModel.segment_dimension) == segment_dimension.upper(),
                    EpisodeStateHistoryModel.segment_value == segment_value,
                    EpisodeStateHistoryModel.status == 'ACTIVE'
                )
            )
            .limit(1)
        )
        
        active_result = await self.session.execute(active_stmt)
        return active_result.scalar_one_or_none()

    async def get_rca_context(self, episode_id: uuid.UUID, t: datetime) -> Optional[Tuple[RCAEvaluationModel, CandidateCauseModel]]:
        """
        Gets the latest RCA evaluation for an episode generated < T, and its rank-1 candidate cause.
        """
        eval_stmt = (
            select(RCAEvaluationModel)
            .where(
                and_(
                    RCAEvaluationModel.episode_id == episode_id,
                    RCAEvaluationModel.generated_at < t
                )
            )
            .order_by(RCAEvaluationModel.evaluation_version.desc())
            .limit(1)
        )
        
        eval_result = await self.session.execute(eval_stmt)
        rca_eval = eval_result.scalar_one_or_none()
        
        if not rca_eval:
            return None
            
        candidate_stmt = (
            select(CandidateCauseModel)
            .where(
                and_(
                    CandidateCauseModel.evaluation_id == rca_eval.evaluation_id,
                    CandidateCauseModel.rank == 1
                )
            )
        )
        
        candidate_result = await self.session.execute(candidate_stmt)
        candidate = candidate_result.scalar_one_or_none()
        
        return rca_eval, candidate

    async def get_most_severe_active_episode(
        self, auth_event: PaymentEventModel, t: datetime
    ) -> Optional[EpisodeStateHistoryModel]:
        """
        Deterministically resolves the most severe active episode applicable to the payment.
        Prioritizes by canonical severity, then older start window, then UUID.
        """
        candidates = []
        
        ep_global = await self.get_active_episode_context("GLOBAL", "ALL", t)
        if ep_global:
            candidates.append(ep_global)
            
        if auth_event.currency:
            ep_curr = await self.get_active_episode_context("CURRENCY", auth_event.currency, t)
            if ep_curr:
                candidates.append(ep_curr)
            
        if auth_event.payment_method:
            ep_pm = await self.get_active_episode_context("PAYMENT_METHOD", auth_event.payment_method, t)
            if ep_pm:
                candidates.append(ep_pm)
            
        if auth_event.bank:
            ep_bank = await self.get_active_episode_context("BANK", auth_event.bank, t)
            if ep_bank:
                candidates.append(ep_bank)
            
        if auth_event.wallet:
            ep_wallet = await self.get_active_episode_context("WALLET", auth_event.wallet, t)
            if ep_wallet:
                candidates.append(ep_wallet)
            
        if not candidates:
            return None
            
        # Tie-breaker ordering:
        # 1. Canonical severity (CRITICAL > HIGH > MODERATE)
        # 2. Older effective_start_window wins equal-severity ties (chronological precedence)
        # 3. episode_id string (deterministic fallback chosen for this function)
        severity_rank = {"CRITICAL": 3, "HIGH": 2, "MODERATE": 1}
        
        candidates.sort(
            key=lambda x: (
                severity_rank.get(x.severity, 0),
                -x.effective_start_window.timestamp(),
                str(x.episode_id)
            ), 
            reverse=True
        )
        return candidates[0]

