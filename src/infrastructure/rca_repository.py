"""
Stage 4 — RCA repository.

APPEND-ONLY: exposes only INSERT operations on Stage 4 tables.
No UPDATE, no DELETE. Immutability is enforced here at the repository boundary.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.rca_models import (
    CandidateCause,
    EvidenceStrength,
    RCAClassification,
    RootCauseAnalysisEvaluation,
)
from src.infrastructure.models import CandidateCauseModel, RCAEvaluationModel


class RCARepository:
    """
    Append-only repository for Stage 4 RCA tables.

    Contract:
    - append_evaluation() and append_candidate_causes() are the ONLY write methods.
    - No update_evaluation(), delete_evaluation(), or equivalent methods exist.
    - Immutability beyond the repository boundary is verified by integration tests.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    async def get_max_evaluation_version(self, episode_id: uuid.UUID) -> int:
        """
        Returns the current maximum evaluation_version for an episode.
        Returns 0 if no evaluations exist yet.
        """
        stmt = select(func.max(RCAEvaluationModel.evaluation_version)).where(
            RCAEvaluationModel.episode_id == episode_id
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def get_latest_evaluation_for_episode(
        self, episode_id: uuid.UUID
    ) -> Optional[RootCauseAnalysisEvaluation]:
        """
        Returns the latest (highest evaluation_version) RCA evaluation for an episode.
        Returns None if no evaluation exists.
        """
        stmt = (
            select(RCAEvaluationModel)
            .where(RCAEvaluationModel.episode_id == episode_id)
            .order_by(RCAEvaluationModel.evaluation_version.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._map_evaluation(model)

    async def get_evaluation_by_id(
        self, evaluation_id: uuid.UUID
    ) -> Optional[RootCauseAnalysisEvaluation]:
        """
        Retrieve a specific evaluation by its primary key.
        Used by immutability integration tests to assert rows are byte-for-byte
        unchanged after subsequent replays.
        """
        stmt = select(RCAEvaluationModel).where(
            RCAEvaluationModel.evaluation_id == evaluation_id
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._map_evaluation(model)

    async def get_candidates_for_evaluation(
        self, evaluation_id: uuid.UUID
    ) -> List[CandidateCause]:
        """Returns all CandidateCause rows for a given evaluation, ordered by rank."""
        stmt = (
            select(CandidateCauseModel)
            .where(CandidateCauseModel.evaluation_id == evaluation_id)
            .order_by(CandidateCauseModel.rank)
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()
        return [self._map_candidate(m) for m in models]

    # ------------------------------------------------------------------
    # Write methods — APPEND ONLY
    # ------------------------------------------------------------------

    async def append_evaluation(
        self, evaluation: RootCauseAnalysisEvaluation
    ) -> None:
        """
        Insert a new RCA evaluation row. NEVER updates an existing row.
        The unique constraint (episode_id, evaluation_version) prevents
        duplicate version insertion under concurrent conditions.
        """
        model = RCAEvaluationModel(
            evaluation_id=evaluation.evaluation_id,
            episode_id=evaluation.episode_id,
            evaluation_version=evaluation.evaluation_version,
            classification=evaluation.classification.value,
            analysis_window_start=evaluation.analysis_window_start,
            analysis_window_end=evaluation.analysis_window_end,
            input_fingerprint=evaluation.input_fingerprint,
            generated_at=evaluation.generated_at,
            evidence_audit_payload=evaluation.evidence_audit_payload,
        )
        self.session.add(model)
        await self.session.flush()

    async def append_candidate_causes(
        self, candidates: List[CandidateCause]
    ) -> None:
        """
        Insert new CandidateCause rows. NEVER updates existing rows.
        Each candidate is permanently owned by its evaluation_id.
        """
        for candidate in candidates:
            model = CandidateCauseModel(
                candidate_id=candidate.candidate_id,
                evaluation_id=candidate.evaluation_id,
                candidate_dimension=candidate.candidate_dimension,
                candidate_value=candidate.candidate_value,
                evidence_strength=candidate.evidence_strength.value,
                excess_failure_contribution=candidate.excess_failure_contribution,
                actual_segment_failures=candidate.actual_segment_failures,
                expected_segment_failures=candidate.expected_segment_failures,
                excess_segment_failures=candidate.excess_segment_failures,
                rank=candidate.rank,
            )
            self.session.add(model)
        if candidates:
            await self.session.flush()

    # ------------------------------------------------------------------
    # Private mapping helpers
    # ------------------------------------------------------------------

    def _map_evaluation(
        self, model: RCAEvaluationModel
    ) -> RootCauseAnalysisEvaluation:
        return RootCauseAnalysisEvaluation(
            evaluation_id=model.evaluation_id,
            episode_id=model.episode_id,
            evaluation_version=model.evaluation_version,
            classification=RCAClassification(model.classification),
            analysis_window_start=model.analysis_window_start,
            analysis_window_end=model.analysis_window_end,
            input_fingerprint=model.input_fingerprint,
            generated_at=model.generated_at,
            evidence_audit_payload=model.evidence_audit_payload,
        )

    def _map_candidate(self, model: CandidateCauseModel) -> CandidateCause:
        return CandidateCause(
            candidate_id=model.candidate_id,
            evaluation_id=model.evaluation_id,
            candidate_dimension=model.candidate_dimension,
            candidate_value=model.candidate_value,
            evidence_strength=EvidenceStrength(model.evidence_strength),
            excess_failure_contribution=model.excess_failure_contribution,
            actual_segment_failures=model.actual_segment_failures,
            expected_segment_failures=model.expected_segment_failures,
            excess_segment_failures=model.excess_segment_failures,
            rank=model.rank,
        )
