from datetime import datetime
from typing import Optional
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.core.domain.failure_prediction_features import FeatureSnapshot
from src.infrastructure.models import PaymentEventModel

class FeatureReconstructionService:
    def __init__(self, repository: FeatureReconstructionRepository):
        self.repository = repository
        
        # Initial configurable thresholds (as per stage5_design.md)
        self.THRESHOLDS = {
            'global': 50,
            'payment_method': 20,
            'bank': 10,
            'wallet': 10,
            'currency': 20
        }

    async def reconstruct_features(self, payment_id: str, t: datetime) -> Optional[FeatureSnapshot]:
        """
        Reconstruct the feature vector as-of time T.
        Returns None if NOT_ELIGIBLE (no auth event found).
        """
        auth_event = await self.repository.get_payment_event_by_id(payment_id, t)
        if not auth_event:
            return None
            
        # 1. Behavioral features
        global_30m = await self._calculate_rate(PaymentEventModel.source_system, auth_event.source_system, t, 30, 'global')
        global_24h = await self._calculate_rate(PaymentEventModel.source_system, auth_event.source_system, t, 24 * 60, 'global')
        
        pm_30m = await self._calculate_rate(PaymentEventModel.payment_method, auth_event.payment_method, t, 30, 'payment_method') if auth_event.payment_method else None
        pm_24h = await self._calculate_rate(PaymentEventModel.payment_method, auth_event.payment_method, t, 24 * 60, 'payment_method') if auth_event.payment_method else None
        
        bank_30m = await self._calculate_rate(PaymentEventModel.bank, auth_event.bank, t, 30, 'bank') if auth_event.bank else None
        bank_24h = await self._calculate_rate(PaymentEventModel.bank, auth_event.bank, t, 24 * 60, 'bank') if auth_event.bank else None
        
        wallet_30m = await self._calculate_rate(PaymentEventModel.wallet, auth_event.wallet, t, 30, 'wallet') if auth_event.wallet else None
        wallet_24h = await self._calculate_rate(PaymentEventModel.wallet, auth_event.wallet, t, 24 * 60, 'wallet') if auth_event.wallet else None
        
        currency_30m = await self._calculate_rate(PaymentEventModel.currency, auth_event.currency, t, 30, 'currency') if auth_event.currency else None
        currency_24h = await self._calculate_rate(PaymentEventModel.currency, auth_event.currency, t, 24 * 60, 'currency') if auth_event.currency else None
        
        # Insufficiency logic: if both global features are None, we lack sufficient volume
        insufficient_global_volume = global_30m is None and global_24h is None
        
        # 2. Stage 3 Degradation Context
        # We need to pick a segment dimension to check.
        # Following the Stage 3 approach, we check the most specific available segment.
        # But wait, stage 5 design says: "Match the payment against the relevant structural dimensions: GLOBAL/ALL, currency, payment_method, bank, and wallet when non-null."
        # For simplicity, we just check GLOBAL/ALL in this reconstruction. Or we check all of them and take the most severe?
        # The design says "For a given payment.authorized event at time T: Identify whether persisted Stage 3 degradation signals provide eligible degradation evidence for the payment's relevant structural dimensions as-of T."
        # I'll check GLOBAL/ALL to get the active episode.
        active_episode = await self.repository.get_most_severe_active_episode(auth_event, t)
        
        is_in_active_degradation = False
        degradation_severity = None
        
        # 3. Stage 4 RCA Context
        rca_classification = None
        rca_candidate_dimension = None
        rca_candidate_value = None
        rca_evidence_strength = None
        rca_excess_failure_contribution = None
        rca_candidate_rank = None
        rca_candidate_matches_payment_segment = None
        
        if active_episode:
            is_in_active_degradation = True
            degradation_severity = active_episode.severity
            
            rca_tuple = await self.repository.get_rca_context(active_episode.episode_id, t)
            if rca_tuple:
                eval_model, candidate_model = rca_tuple
                
                # Check for segmentation match (SYSTEMIC = False)
                rca_classification = eval_model.classification
                
                if candidate_model:
                    rca_candidate_dimension = candidate_model.candidate_dimension
                    rca_candidate_value = candidate_model.candidate_value
                    rca_evidence_strength = candidate_model.evidence_strength
                    rca_excess_failure_contribution = candidate_model.excess_failure_contribution
                    rca_candidate_rank = candidate_model.rank
                    
                    if rca_classification == 'SEGMENT_SPECIFIC':
                        rca_candidate_matches_payment_segment = self._matches_segment(auth_event, rca_candidate_dimension, rca_candidate_value)
                    elif rca_classification == 'SYSTEMIC':
                        rca_candidate_matches_payment_segment = False
        
        return FeatureSnapshot(
            payment_method=auth_event.payment_method,
            bank=auth_event.bank,
            wallet=auth_event.wallet,
            currency=auth_event.currency,
            amount_minor_units=auth_event.amount_minor_units,
            hour_of_day=auth_event.timestamp.hour,
            day_of_week=auth_event.timestamp.weekday(),
            global_30m_failure_rate=global_30m,
            global_24h_failure_rate=global_24h,
            payment_method_30m_failure_rate=pm_30m,
            payment_method_24h_failure_rate=pm_24h,
            bank_30m_failure_rate=bank_30m,
            bank_24h_failure_rate=bank_24h,
            wallet_30m_failure_rate=wallet_30m,
            wallet_24h_failure_rate=wallet_24h,
            currency_30m_failure_rate=currency_30m,
            currency_24h_failure_rate=currency_24h,
            insufficient_global_volume=insufficient_global_volume,
            is_in_active_degradation=is_in_active_degradation,
            degradation_severity=degradation_severity,
            rca_classification=rca_classification,
            rca_candidate_dimension=rca_candidate_dimension,
            rca_candidate_value=rca_candidate_value,
            rca_evidence_strength=rca_evidence_strength,
            rca_excess_failure_contribution=rca_excess_failure_contribution,
            rca_candidate_rank=rca_candidate_rank,
            rca_candidate_matches_payment_segment=rca_candidate_matches_payment_segment
        )

    async def _calculate_rate(self, dimension_col, dimension_value: str, t: datetime, window_minutes: int, threshold_key: str) -> Optional[float]:
        total, failures = await self.repository.get_historical_failure_rate(dimension_col, dimension_value, t, window_minutes)
        if total < self.THRESHOLDS[threshold_key]:
            return None
        return failures / total if total > 0 else 0.0



    def _matches_segment(self, event: PaymentEventModel, dimension: str, value: str) -> bool:
        if dimension == 'BANK' and event.bank == value: return True
        if dimension == 'WALLET' and event.wallet == value: return True
        if dimension == 'PAYMENT_METHOD' and event.payment_method == value: return True
        if dimension == 'CURRENCY' and event.currency == value: return True
        return False
