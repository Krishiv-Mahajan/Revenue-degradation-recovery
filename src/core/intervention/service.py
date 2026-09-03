"""
Stage 6 — Intervention Decisioning Service.

Orchestrates deterministic decisioning:
1. Gathers upstream Stage 1, 3, 4, 5 inputs strictly as-of T_decide
2. Evaluates hard safety gates (kill switch, invariance, prediction validity, etc.)
3. Generates and filters permitted candidate routes
4. Computes deterministic policy utility (Decimal/minor units) and ranks candidates
5. Decides among NO_ACTION, MONITOR, and ACT
6. Atomically checks global rate limit under a global lock if ACT
7. Persists immutable, versioned InterventionDecision
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.failure_prediction_models import FailurePrediction
from src.core.domain.intervention_models import (
    CandidateRouteScore,
    DecisionType,
    GateVerdict,
    InterventionDecision,
    InterventionRouteKey,
    compute_decision_fingerprint,
    make_decision_id,
)
from src.core.intervention.calculator import (
    calculate_policy_utility,
    calculate_route_alignment,
    determine_verdict,
    score_and_rank_candidates,
)
from src.core.intervention.gates import (
    evaluate_amount_and_currency,
    evaluate_episode_and_traffic_shift,
    evaluate_kill_switch,
    evaluate_pre_terminal_invariance,
    evaluate_route_eligibility,
    evaluate_stage5_prediction_validity,
)
from src.core.intervention.policy import InterventionPolicy, get_default_policy
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.infrastructure.intervention_repository import InterventionRepository


class InterventionDecisionService:
    """
    Orchestrates Stage 6 Intervention Decisioning for payment attempts.
    """

    def __init__(
        self,
        session: AsyncSession,
        intervention_repository: InterventionRepository,
        feature_reconstruction_repository: FeatureReconstructionRepository,
        policy: Optional[InterventionPolicy] = None,
    ) -> None:
        self.session = session
        self.intervention_repo = intervention_repository
        self.feature_repo = feature_reconstruction_repository
        self.policy = policy or get_default_policy()

    async def decide(
        self,
        payment_attempt_id: str,
        t_decide: datetime,
    ) -> InterventionDecision:
        """
        Produce a deterministic intervention decision for payment_attempt_id as-of t_decide.
        Idempotent: Identical inputs + t_decide + policy produce identical decision.
        """
        # Ensure timezone-aware UTC datetime
        if t_decide.tzinfo is None:
            t_decide = t_decide.replace(tzinfo=timezone.utc)

        # -------------------------------------------------------------------
        # 1. Fetch Upstream Context Strictly As-Of T_decide
        # -------------------------------------------------------------------
        auth_event = await self.feature_repo.get_payment_event_by_id(payment_attempt_id, t_decide)
        terminal_exists = await self.intervention_repo.check_terminal_event_exists(payment_attempt_id, t_decide)
        stage5_pred = await self.intervention_repo.get_stage5_prediction_as_of(payment_attempt_id, t_decide)

        # Extract payment facts
        amount_minor_units = auth_event.amount_minor_units if auth_event else 0
        currency = auth_event.currency if auth_event else ""
        payment_method = auth_event.payment_method if auth_event else None
        bank = auth_event.bank if auth_event else None
        wallet = auth_event.wallet if auth_event else None

        # Stage 3 degradation & Stage 4 RCA context
        active_episode = None
        episode_id = None
        episode_status = None
        rca_classification = None
        rca_candidate_dim = None
        rca_candidate_val = None
        rca_evidence_strength = None
        rca_audit_payload = None
        rca_candidate_matches_payment_segment = None
        
        if stage5_pred and stage5_pred.feature_snapshot:
            rca_candidate_matches_payment_segment = stage5_pred.feature_snapshot.get("rca_candidate_matches_payment_segment")

        if auth_event:
            active_episode = await self.feature_repo.get_most_severe_active_episode(auth_event, t_decide)

            if active_episode:
                episode_id = active_episode.episode_id
                episode_status = active_episode.status
                rca_tuple = await self.feature_repo.get_rca_context(active_episode.episode_id, t_decide)
                if rca_tuple:
                    rca_eval, candidate_model = rca_tuple
                    rca_classification = rca_eval.classification
                    rca_audit_payload = rca_eval.evidence_audit_payload
                    if candidate_model:
                        rca_candidate_dim = candidate_model.candidate_dimension
                        rca_candidate_val = candidate_model.candidate_value
                        rca_evidence_strength = candidate_model.evidence_strength

        # -------------------------------------------------------------------
        # 2. Compute Input Fingerprint
        # -------------------------------------------------------------------
        fingerprint = compute_decision_fingerprint(
            payment_attempt_id=payment_attempt_id,
            t_decide=t_decide,
            amount_minor_units=amount_minor_units,
            currency=currency,
            stage5_prediction_id=stage5_pred.prediction_id if stage5_pred else None,
            failure_probability=stage5_pred.failure_probability if stage5_pred else None,
            prediction_status=stage5_pred.prediction_status if stage5_pred else "MISSING",
            episode_id=episode_id,
            episode_status=episode_status,
            rca_classification=rca_classification,
            rca_candidate_dimension=rca_candidate_dim,
            rca_candidate_value=rca_candidate_val,
            rca_evidence_strength=rca_evidence_strength,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.policy_version,
        )

        # -------------------------------------------------------------------
        # 3. Fast Idempotency Check (before lock)
        # -------------------------------------------------------------------
        latest = await self.intervention_repo.get_latest_decision(payment_attempt_id)
        if latest is not None and latest.input_fingerprint == fingerprint:
            return latest

        # -------------------------------------------------------------------
        # 4. Acquire Per-Attempt Version Lock & Recheck Idempotency
        # -------------------------------------------------------------------
        await self.intervention_repo.acquire_version_allocation_lock(payment_attempt_id)
        latest = await self.intervention_repo.get_latest_decision(payment_attempt_id)
        if latest is not None and latest.input_fingerprint == fingerprint:
            return latest

        # -------------------------------------------------------------------
        # 5. Evaluate Sequential Hard Safety Gates
        # -------------------------------------------------------------------
        audit_gates: Dict[str, Any] = {}

        # Gate 1: Kill switch
        g1 = evaluate_kill_switch(self.policy)
        audit_gates["kill_switch"] = {"passed": g1.passed, "details": g1.details}
        if not g1.passed:
            return await self._persist_no_action(
                payment_attempt_id, t_decide, stage5_pred, fingerprint,
                g1.verdict, audit_gates, {}, rca_evidence_strength
            )

        # Gate 2: Amount & currency
        g2 = evaluate_amount_and_currency(amount_minor_units, currency)
        audit_gates["amount_and_currency"] = {"passed": g2.passed, "details": g2.details}
        if not g2.passed:
            return await self._persist_no_action(
                payment_attempt_id, t_decide, stage5_pred, fingerprint,
                g2.verdict, audit_gates, {}, rca_evidence_strength
            )

        # Gate 3: Pre-terminal outcome invariance
        g3 = evaluate_pre_terminal_invariance(terminal_exists)
        audit_gates["pre_terminal_invariance"] = {"passed": g3.passed, "details": g3.details}
        if not g3.passed:
            return await self._persist_no_action(
                payment_attempt_id, t_decide, stage5_pred, fingerprint,
                g3.verdict, audit_gates, {}, rca_evidence_strength
            )

        # Gate 4: Stage 5 prediction validity
        g4 = evaluate_stage5_prediction_validity(stage5_pred)
        audit_gates["stage5_prediction"] = {"passed": g4.passed, "details": g4.details}
        if not g4.passed:
            return await self._persist_no_action(
                payment_attempt_id, t_decide, stage5_pred, fingerprint,
                g4.verdict, audit_gates, {}, rca_evidence_strength
            )

        # Gate 5: Episode & traffic shift
        g5 = evaluate_episode_and_traffic_shift(episode_status, rca_audit_payload)
        audit_gates["episode_and_traffic_shift"] = {"passed": g5.passed, "details": g5.details}
        if not g5.passed:
            return await self._persist_no_action(
                payment_attempt_id, t_decide, stage5_pred, fingerprint,
                g5.verdict, audit_gates, {}, rca_evidence_strength
            )

        # -------------------------------------------------------------------
        # 6. Candidate Route Generation & Policy Scoring
        # -------------------------------------------------------------------
        failure_prob = stage5_pred.failure_probability if (stage5_pred and stage5_pred.failure_probability is not None) else 0.0
        candidate_scores: List[CandidateRouteScore] = []

        for route_key, route_def in self.policy.routes.items():
            is_on_cooldown = await self.intervention_repo.has_recent_route_decision(
                payment_attempt_id,
                route_key.value,
                since_timestamp=t_decide - timedelta(seconds=route_def.cooldown_seconds),
            )

            is_eligible, eligibility_reason = evaluate_route_eligibility(
                route=route_def,
                rca_classification=rca_classification,
                rca_evidence_strength=rca_evidence_strength,
                rca_candidate_dimension=rca_candidate_dim,
                rca_candidate_value=rca_candidate_val,
                rca_candidate_matches_payment_segment=rca_candidate_matches_payment_segment,
                payment_method=payment_method,
                bank=bank,
                is_on_cooldown=is_on_cooldown,
            )

            if is_eligible:
                expected_benefit, utility = calculate_policy_utility(
                    amount_minor_units=amount_minor_units,
                    failure_probability=failure_prob,
                    recovery_rate=route_def.policy_recovery_rate,
                    cost_minor_units=route_def.policy_cost_minor_units,
                )
                alignment = calculate_route_alignment(
                    route=route_def,
                    rca_candidate_dimension=rca_candidate_dim,
                    rca_candidate_value=rca_candidate_val,
                    payment_method=payment_method,
                    bank=bank,
                    wallet=wallet,
                )
            else:
                expected_benefit = 0
                utility = 0
                alignment = 0.0

            candidate_scores.append(
                CandidateRouteScore(
                    route_key=route_key,
                    is_eligible=is_eligible,
                    eligibility_reason=eligibility_reason,
                    expected_recovery_benefit=expected_benefit,
                    policy_cost_minor_units=route_def.policy_cost_minor_units,
                    policy_utility=utility,
                    route_alignment_score=alignment,
                    rank=0,
                )
            )

        eligible_candidates = [c for c in candidate_scores if c.is_eligible]
        ranked_candidates = score_and_rank_candidates(eligible_candidates)
        best_candidate = ranked_candidates[0] if ranked_candidates else None

        # Build candidate audit payload
        audit_candidates = [
            {
                "route_key": c.route_key.value,
                "is_eligible": c.is_eligible,
                "eligibility_reason": c.eligibility_reason,
                "expected_recovery_benefit_minor_units": int(c.expected_recovery_benefit),
                "policy_cost_minor_units": c.policy_cost_minor_units,
                "policy_utility_minor_units": int(c.policy_utility),
                "route_alignment_score": c.route_alignment_score,
                "rank": c.rank,
            }
            for c in ranked_candidates
        ]

        if not ranked_candidates and failure_prob >= self.policy.act_risk_threshold:
            # If high risk but no routes eligible
            return await self._persist_no_action(
                payment_attempt_id, t_decide, stage5_pred, fingerprint,
                GateVerdict.FAILED_NO_ELIGIBLE_ROUTES, audit_gates,
                {"candidates": audit_candidates}, rca_evidence_strength
            )

        # Determine tentative verdict
        tentative_verdict, selected_route, decision_confidence, gate_verdict = determine_verdict(
            best_candidate=best_candidate,
            failure_probability=failure_prob,
            policy=self.policy,
            rca_evidence_strength=rca_evidence_strength,
        )

        # -------------------------------------------------------------------
        # 7. Authoritative Global ACT Admission Sequence (Under Global Lock)
        # -------------------------------------------------------------------
        if tentative_verdict == DecisionType.ACT:
            # Acquire global lock to serialize ACT admission check + insertion
            await self.intervention_repo.acquire_global_rate_limit_lock()

            recent_act_count = await self.intervention_repo.count_recent_act_decisions(
                since_timestamp=t_decide - timedelta(seconds=60)
            )

            if recent_act_count >= self.policy.global_rate_limit_per_minute:
                tentative_verdict = DecisionType.NO_ACTION
                selected_route = None
                decision_confidence = 1.0
                gate_verdict = GateVerdict.FAILED_GLOBAL_RATE_LIMIT
                audit_gates["global_rate_limit"] = {
                    "passed": False,
                    "recent_act_count": recent_act_count,
                    "limit": self.policy.global_rate_limit_per_minute,
                }
            else:
                audit_gates["global_rate_limit"] = {
                    "passed": True,
                    "recent_act_count": recent_act_count,
                    "limit": self.policy.global_rate_limit_per_minute,
                }

        # -------------------------------------------------------------------
        # 8. Persist Immutable Decision Record
        # -------------------------------------------------------------------
        max_version = await self.intervention_repo.get_max_decision_version(payment_attempt_id)
        new_version = max_version + 1

        audit_payload = {
            "payment_facts": {
                "amount_minor_units": amount_minor_units,
                "currency": currency,
                "payment_method": payment_method,
                "bank": bank,
                "wallet": wallet,
            },
            "upstream_context": {
                "stage5_prediction_id": str(stage5_pred.prediction_id) if stage5_pred else None,
                "stage5_status": stage5_pred.prediction_status if stage5_pred else None,
                "failure_probability": failure_prob,
                "episode_id": str(episode_id) if episode_id else None,
                "episode_status": episode_status,
                "rca_classification": rca_classification,
                "rca_candidate_dimension": rca_candidate_dim,
                "rca_candidate_value": rca_candidate_val,
                "rca_evidence_strength": rca_evidence_strength,
            },
            "gates": audit_gates,
            "candidates": audit_candidates,
            "policy": {
                "policy_id": self.policy.policy_id,
                "policy_version": self.policy.policy_version,
                "act_risk_threshold": self.policy.act_risk_threshold,
                "monitor_risk_threshold": self.policy.monitor_risk_threshold,
                "min_policy_utility_threshold_minor_units": int(self.policy.min_policy_utility_threshold),
                "global_rate_limit_per_minute": self.policy.global_rate_limit_per_minute,
            },
        }

        decision = InterventionDecision(
            decision_id=make_decision_id(payment_attempt_id, new_version),
            payment_attempt_id=payment_attempt_id,
            decision_version=new_version,
            decided_at=t_decide,
            decision_type=tentative_verdict,
            selected_route_id=selected_route,
            failure_probability=failure_prob if (stage5_pred and stage5_pred.failure_probability is not None) else None,
            stage5_prediction_id=stage5_pred.prediction_id if stage5_pred else None,
            diagnosis_confidence=rca_evidence_strength,
            decision_confidence=decision_confidence,
            gate_verdict=gate_verdict,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.policy_version,
            input_fingerprint=fingerprint,
            evaluation_audit_payload=audit_payload,
            created_at=datetime.now(timezone.utc),
        )

        await self.intervention_repo.append_decision(decision)
        return decision

    async def _persist_no_action(
        self,
        payment_attempt_id: str,
        t_decide: datetime,
        stage5_pred: Optional[FailurePrediction],
        fingerprint: str,
        gate_verdict: GateVerdict,
        audit_gates: Dict[str, Any],
        extra_audit: Dict[str, Any],
        diagnosis_confidence: Optional[str],
    ) -> InterventionDecision:
        """
        Helper to construct and persist a NO_ACTION decision when a hard gate triggers.
        """
        max_version = await self.intervention_repo.get_max_decision_version(payment_attempt_id)
        new_version = max_version + 1

        audit_payload = {
            "gates": audit_gates,
            "extra": extra_audit,
            "policy": {
                "policy_id": self.policy.policy_id,
                "policy_version": self.policy.policy_version,
            },
        }

        decision = InterventionDecision(
            decision_id=make_decision_id(payment_attempt_id, new_version),
            payment_attempt_id=payment_attempt_id,
            decision_version=new_version,
            decided_at=t_decide,
            decision_type=DecisionType.NO_ACTION,
            selected_route_id=None,
            failure_probability=stage5_pred.failure_probability if (stage5_pred and stage5_pred.failure_probability is not None) else None,
            stage5_prediction_id=stage5_pred.prediction_id if stage5_pred else None,
            diagnosis_confidence=diagnosis_confidence,
            decision_confidence=1.0,
            gate_verdict=gate_verdict,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.policy_version,
            input_fingerprint=fingerprint,
            evaluation_audit_payload=audit_payload,
            created_at=datetime.now(timezone.utc),
        )

        await self.intervention_repo.append_decision(decision)
        return decision
