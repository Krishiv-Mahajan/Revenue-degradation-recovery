"""
Stage 4 — RCA service.

Orchestrates: query Stage 1/Stage 2 data → run calculator → fingerprint check
              → conditionally persist new evaluation version.

No prediction, no automated action, no revenue-at-risk.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.domain.rca_models import (
    HISTORICAL_WEEKS_BACK,
    MIN_BASELINE_VOLUME,
    MIN_CONTRIBUTION_THRESHOLD,
    MIN_ERROR_DEVIATION_THRESHOLD,
    MIN_FAILURES_FOR_RCA_EVALUATION,
    MIN_VALID_HISTORICAL_WINDOWS,
    STRUCTURAL_DIMENSION_FIELD_MAP,
    STRUCTURAL_DIMENSIONS,
    SUPPORTING_DIMENSION_FIELD_MAP,
    SUPPORTING_DIMENSIONS,
    CandidateCause,
    EvidenceStrength,
    RCAClassification,
    RootCauseAnalysisEvaluation,
    compute_input_fingerprint,
    make_candidate_id,
    make_evaluation_id,
)
from src.core.domain.degradation_models import DegradationEpisode, EpisodeStatus
from src.core.rca.calculator import (
    assign_evidence_strength,
    build_audit_payload,
    build_unknown_payload,
    compute_analysis_window,
    compute_candidate_metrics,
    compute_episode_total_excess_failures,
    compute_error_deviation,
    compute_excess_failure_contribution,
    compute_volume_weighted_failure_rate,
    get_all_historical_starts,
    rank_candidates,
)
from src.infrastructure.models import (
    DegradationSignalModel,
    PaymentEventModel,
    PaymentHealthSnapshotModel,
)
from src.infrastructure.rca_repository import RCARepository

# Stage 2 dimensions covered by payment_health_snapshots
_SNAPSHOT_DIMENSIONS = {"payment_method", "bank", "wallet", "currency", "error_source"}


class RCAService:
    """
    Orchestrates Stage 4 RCA evaluation for a single DegradationEpisode.

    Responsibilities:
    - Determine the analysis window.
    - Query Stage 1 (payment_events) and Stage 2 (payment_health_snapshots).
    - Run deterministic RCA math (calculator.py).
    - Compute input fingerprint.
    - If fingerprint changed (or no prior evaluation), append new version to DB.
    """

    def __init__(self, session: AsyncSession, rca_repository: RCARepository) -> None:
        self.session = session
        self.repo = rca_repository

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def evaluate_episode(self, episode: DegradationEpisode) -> None:
        """
        Evaluate RCA for the given episode. If the deterministic input fingerprint
        matches the last persisted evaluation, no new version is created (§24B).
        """

        # --- INVALIDATED: immediately append UNKNOWN without querying events ---
        if episode.status == EpisodeStatus.INVALIDATED:
            await self._persist_unknown(
                episode=episode,
                analysis_window_start=episode.started_at_window,
                analysis_window_end=episode.started_at_window + timedelta(minutes=5),
                reason="Episode invalidated by Stage 3.",
                episode_transaction_count=0,
                episode_actual_failures=0,
                hist_window_tuples=[],
            )
            return

        # --- Determine analysis window ---
        latest_bad_window_start = await self._get_latest_bad_window_start(episode)
        analysis_window_start, analysis_window_end = compute_analysis_window(
            started_at_window=episode.started_at_window,
            ended_at_window=episode.ended_at_window,
            latest_bad_window_start=latest_bad_window_start,
            episode_status=episode.status.value,
        )

        # --- Query episode-window events from Stage 1 ---
        episode_events = await self._get_events_in_window(
            analysis_window_start, analysis_window_end
        )

        # Count terminal event types only
        episode_transactions = sum(
            1
            for e in episode_events
            if e["event_type"] in ("payment.captured", "payment.failed")
        )
        episode_actual_failures = sum(
            1 for e in episode_events if e["event_type"] == "payment.failed"
        )

        # --- Pre-flight: insufficient episode volume → UNKNOWN ---
        if episode_actual_failures < MIN_FAILURES_FOR_RCA_EVALUATION:
            # Fingerprint still computed so identical-input idempotency works
            hist_tuples = []
            fingerprint = compute_input_fingerprint(
                episode_id=episode.episode_id,
                episode_status=episode.status.value,
                analysis_window_start=analysis_window_start,
                analysis_window_end=analysis_window_end,
                episode_actual_failures=episode_actual_failures,
                episode_transaction_count=episode_transactions,
                historical_window_tuples=hist_tuples,
            )
            last = await self.repo.get_latest_evaluation_for_episode(episode.episode_id)
            if last and last.input_fingerprint == fingerprint:
                return  # Unchanged — no new version needed
            await self._persist_unknown(
                episode=episode,
                analysis_window_start=analysis_window_start,
                analysis_window_end=analysis_window_end,
                reason=(
                    f"Insufficient episode failures: {episode_actual_failures} < "
                    f"{MIN_FAILURES_FOR_RCA_EVALUATION} (MIN_FAILURES_FOR_RCA_EVALUATION)."
                ),
                episode_transaction_count=episode_transactions,
                episode_actual_failures=episode_actual_failures,
                hist_window_tuples=hist_tuples,
            )
            return

        # --- Compute historical window starts ---
        all_hist_starts = get_all_historical_starts(
            analysis_window_start, analysis_window_end, HISTORICAL_WEEKS_BACK
        )

        # --- Evaluate each structural dimension ---
        structural_results: List[dict] = []
        all_hist_tuples: List[tuple] = []

        for dim in STRUCTURAL_DIMENSIONS:
            field_name = STRUCTURAL_DIMENSION_FIELD_MAP[dim]
            dimension_lower = dim.lower()
            snapshot_dim = field_name  # Stage 2 uses field names as dimension strings

            # Gather all distinct values for this dimension in the episode window
            dim_values = set(
                e[field_name] for e in episode_events
                if e.get(field_name) is not None
                and e["event_type"] in ("payment.captured", "payment.failed")
            )

            for val in sorted(dim_values):
                result = await self._evaluate_structural_candidate(
                    episode_events=episode_events,
                    episode_transactions=episode_transactions,
                    episode_actual_failures=episode_actual_failures,
                    analysis_window_start=analysis_window_start,
                    analysis_window_end=analysis_window_end,
                    dimension=dim,
                    field_name=field_name,
                    snapshot_dim=snapshot_dim,
                    value=val,
                    all_hist_starts=all_hist_starts,
                )
                structural_results.append(result)
                # Collect fingerprint tuples
                for hw in result.get("_hist_tuples", []):
                    all_hist_tuples.append(hw)

        # --- Evaluate supporting error evidence ---
        supporting_evidence: List[dict] = []
        for support_dim in SUPPORTING_DIMENSIONS:
            field_name = SUPPORTING_DIMENSION_FIELD_MAP[support_dim]
            support_results = await self._evaluate_supporting_dimension(
                episode_events=episode_events,
                episode_actual_failures=episode_actual_failures,
                analysis_window_start=analysis_window_start,
                analysis_window_end=analysis_window_end,
                support_dim=support_dim,
                field_name=field_name,
                all_hist_starts=all_hist_starts,
            )
            supporting_evidence.extend(support_results)

        # --- Fingerprint ---
        fingerprint = compute_input_fingerprint(
            episode_id=episode.episode_id,
            episode_status=episode.status.value,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            episode_actual_failures=episode_actual_failures,
            episode_transaction_count=episode_transactions,
            historical_window_tuples=all_hist_tuples,
        )

        # Idempotency check (§24B)
        last = await self.repo.get_latest_evaluation_for_episode(episode.episode_id)
        if last and last.input_fingerprint == fingerprint:
            return  # Identical input state — no new evaluation needed

        # --- Episode-level expected failures (canonical denominator) ---
        # Fetch exhaustive GLOBAL snapshots across the historical windows
        global_snapshots = await self._get_hist_snapshot_for_dim(
            all_hist_starts, "GLOBAL", "GLOBAL"
        )
        
        valid_global_failures = []
        valid_global_transactions = []
        valid_global_windows = 0
        for snap in global_snapshots:
            if not snap.insufficient_volume and snap.transaction_count >= MIN_BASELINE_VOLUME:
                valid_global_failures.append(snap.failed_transaction_count)
                valid_global_transactions.append(snap.transaction_count)
                valid_global_windows += 1
                
        ep_baseline_rate = None
        if valid_global_windows >= MIN_VALID_HISTORICAL_WINDOWS:
            ep_baseline_rate = compute_volume_weighted_failure_rate(
                valid_global_failures, valid_global_transactions
            )
            
        ep_expected_failures = (
            episode_transactions * ep_baseline_rate if ep_baseline_rate is not None else 0.0
        )

        # --- Compute episode_total_excess_failures ---
        episode_total_excess = max(0.0, episode_actual_failures - ep_expected_failures)

        # --- Guard: episode_total_excess <= 0 → UNKNOWN ---
        if episode_total_excess <= 0:
            await self._persist_unknown_with_fingerprint(
                episode=episode,
                analysis_window_start=analysis_window_start,
                analysis_window_end=analysis_window_end,
                reason=(
                    "No net excess failures detected: observed failures did not exceed "
                    "historical baseline across any structural dimension."
                ),
                episode_transaction_count=episode_transactions,
                episode_actual_failures=episode_actual_failures,
                fingerprint=fingerprint,
                structural_results=structural_results,
                supporting_evidence=supporting_evidence,
                hist_starts=all_hist_starts,
            )
            return

        # --- Compute contributions and qualify candidates ---
        qualified_candidates: List[dict] = []
        for r in structural_results:
            if not r.get("baseline_sufficient", False):
                continue
            excess = r["excess_segment_failures"]
            if excess <= 0:
                r["excess_failure_contribution"] = 0.0
                r["evidence_strength"] = None
                r["qualified_as_candidate"] = False
                r["disqualification_reason"] = "excess_segment_failures <= 0"
                continue

            contribution = compute_excess_failure_contribution(excess, episode_total_excess)
            r["excess_failure_contribution"] = contribution if contribution is not None else 0.0

            # Qualification check (§13)
            actual = r["actual_segment_failures"]
            if (
                actual >= MIN_FAILURES_FOR_RCA_EVALUATION
                and excess > 0
                and contribution is not None
                and contribution >= MIN_CONTRIBUTION_THRESHOLD
            ):
                strength = assign_evidence_strength(contribution, actual)
                r["evidence_strength"] = strength.value
                r["qualified_as_candidate"] = True
                r["disqualification_reason"] = None
                qualified_candidates.append(r)
            else:
                r["evidence_strength"] = None
                r["qualified_as_candidate"] = False
                if actual < MIN_FAILURES_FOR_RCA_EVALUATION:
                    r["disqualification_reason"] = (
                        f"actual_segment_failures {actual} < "
                        f"MIN_FAILURES_FOR_RCA_EVALUATION {MIN_FAILURES_FOR_RCA_EVALUATION}"
                    )
                elif contribution is None or contribution < MIN_CONTRIBUTION_THRESHOLD:
                    r["disqualification_reason"] = (
                        f"excess_failure_contribution {contribution:.4f} < "
                        f"MIN_CONTRIBUTION_THRESHOLD {MIN_CONTRIBUTION_THRESHOLD}"
                    )
                else:
                    r["disqualification_reason"] = "Did not meet all candidate thresholds"

        # --- Classify ---
        if qualified_candidates:
            classification = RCAClassification.SEGMENT_SPECIFIC
            classification_reason = (
                "At least one structural candidate passed all deterministic thresholds."
            )
        else:
            classification = RCAClassification.SYSTEMIC
            classification_reason = (
                "Episode total excess failures > 0, historical baseline valid, "
                "but no structural dimension exceeded the contribution threshold. "
                "Degradation appears to be broadly distributed across all segments."
            )

        # --- Rank qualified candidates ---
        ranked = rank_candidates(qualified_candidates)
        overlap_note: Optional[str] = None
        if len(ranked) > 1:
            overlap_note = (
                "Multiple structural candidates detected. Candidates may be correlated "
                "— e.g., BANK failures may be a subset of PAYMENT_METHOD failures. "
                "Treat as correlated hypotheses, not independent proven causes."
            )

        # --- Historical comparison windows for audit payload ---
        hist_window_audit = _build_hist_window_audit(all_hist_starts, structural_results)

        # --- Build audit payload ---
        audit_payload = build_audit_payload(
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            historical_comparison_windows=hist_window_audit,
            episode_id=str(episode.episode_id),
            segment_dimension=episode.segment_dimension,
            segment_value=episode.segment_value,
            episode_status=episode.status.value,
            episode_transaction_count=episode_transactions,
            episode_actual_failures=episode_actual_failures,
            episode_historical_baseline_failure_rate=ep_baseline_rate,
            episode_expected_failures=ep_expected_failures,
            episode_total_excess_failures=episode_total_excess,
            structural_candidates_evaluated=_clean_structural_results(structural_results),
            supporting_error_evidence=supporting_evidence,
            classification=classification,
            classification_reason=classification_reason,
            overlap_note=overlap_note,
            input_fingerprint=fingerprint,
        )

        # --- Determine new evaluation version ---
        # Acquire a transaction-level advisory lock to serialize version allocation
        import struct
        k1, k2 = struct.unpack("<ii", episode.episode_id.bytes[:8])
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:k1, :k2)"), {"k1": k1, "k2": k2}
        )

        max_version = await self.repo.get_max_evaluation_version(episode.episode_id)
        new_version = max_version + 1
        evaluation_id = make_evaluation_id(episode.episode_id, new_version)

        evaluation = RootCauseAnalysisEvaluation(
            evaluation_id=evaluation_id,
            episode_id=episode.episode_id,
            evaluation_version=new_version,
            classification=classification,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            input_fingerprint=fingerprint,
            generated_at=datetime.now(timezone.utc),
            evidence_audit_payload=audit_payload,
        )
        await self.repo.append_evaluation(evaluation)

        # --- Persist CandidateCause rows ---
        candidate_domain_objects: List[CandidateCause] = []
        for r in ranked:
            candidate_id = make_candidate_id(
                evaluation_id, r["dimension"], r["value"]
            )
            candidate_domain_objects.append(
                CandidateCause(
                    candidate_id=candidate_id,
                    evaluation_id=evaluation_id,
                    candidate_dimension=r["dimension"],
                    candidate_value=r["value"],
                    evidence_strength=EvidenceStrength(r["evidence_strength"]),
                    excess_failure_contribution=r["excess_failure_contribution"],
                    actual_segment_failures=r["actual_segment_failures"],
                    expected_segment_failures=r["expected_segment_failures"],
                    excess_segment_failures=r["excess_segment_failures"],
                    rank=r["rank"],
                )
            )
        await self.repo.append_candidate_causes(candidate_domain_objects)
        await self.session.commit()

    # ------------------------------------------------------------------
    # Private helpers — data queries
    # ------------------------------------------------------------------

    async def _get_latest_bad_window_start(
        self, episode: DegradationEpisode
    ) -> Optional[datetime]:
        """
        For ACTIVE episodes, find the window_start of the most recent BAD signal
        for this segment using the latest evaluation version.
        """
        if episode.status == EpisodeStatus.RECOVERED:
            return None  # Not needed for RECOVERED

        stmt = (
            select(DegradationSignalModel.window_start)
            .where(
                DegradationSignalModel.segment_dimension == episode.segment_dimension,
                DegradationSignalModel.segment_value == episode.segment_value,
                DegradationSignalModel.signal_type == "BAD",
                DegradationSignalModel.window_start >= episode.started_at_window,
            )
            .order_by(
                DegradationSignalModel.window_start.desc(),
                DegradationSignalModel.evaluation_version.desc(),
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return row

    async def _get_events_in_window(
        self, window_start: datetime, window_end: datetime
    ) -> List[dict]:
        """
        Fetch all payment events in [window_start, window_end) from Stage 1.
        Returns lightweight dicts to avoid full ORM overhead.
        """
        stmt = (
            select(
                PaymentEventModel.event_type,
                PaymentEventModel.payment_method,
                PaymentEventModel.bank,
                PaymentEventModel.wallet,
                PaymentEventModel.currency,
                PaymentEventModel.error_code,
                PaymentEventModel.error_source,
                PaymentEventModel.error_step,
            )
            .where(
                PaymentEventModel.timestamp >= window_start,
                PaymentEventModel.timestamp < window_end,
            )
        )
        result = await self.session.execute(stmt)
        rows = result.mappings().all()
        return [dict(r) for r in rows]

    async def _get_hist_snapshot_for_dim(
        self,
        hist_starts: List[datetime],
        snapshot_dim: str,
        value: str,
    ) -> List[PaymentHealthSnapshotModel]:
        """
        Fetch Stage 2 snapshots for a structural dimension/value at historical window starts.
        """
        if not hist_starts:
            return []
        stmt = select(PaymentHealthSnapshotModel).where(
            PaymentHealthSnapshotModel.window_start.in_(hist_starts),
            PaymentHealthSnapshotModel.segment_dimension == snapshot_dim,
            PaymentHealthSnapshotModel.segment_value == value,
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def _get_hist_events_for_dim(
        self,
        hist_starts: List[datetime],
        field_name: str,
        value: str,
    ) -> Tuple[int, int]:
        """
        Directly query Stage 1 payment_events for supporting evidence dimensions
        that are not covered by Stage 2 snapshots.
        Returns (segment_failures, segment_transactions) across all valid hist windows.
        """
        if not hist_starts:
            return 0, 0

        hist_ends = [s + timedelta(minutes=5) for s in hist_starts]
        # Build OR conditions across all historical windows
        from sqlalchemy import or_, and_
        window_conditions = or_(
            *[
                and_(
                    PaymentEventModel.timestamp >= s,
                    PaymentEventModel.timestamp < e,
                )
                for s, e in zip(hist_starts, hist_ends)
            ]
        )

        field_col = getattr(PaymentEventModel, field_name)

        # Failures where field == value
        fail_stmt = select(func.count()).where(
            window_conditions,
            PaymentEventModel.event_type == "payment.failed",
            field_col == value,
        )
        # All terminal transactions where field == value
        txn_stmt = select(func.count()).where(
            window_conditions,
            PaymentEventModel.event_type.in_(["payment.captured", "payment.failed"]),
            field_col == value,
        )
        fail_result = await self.session.execute(fail_stmt)
        txn_result = await self.session.execute(txn_stmt)
        return fail_result.scalar() or 0, txn_result.scalar() or 0

    # ------------------------------------------------------------------
    # Private helpers — structural dimension evaluation
    # ------------------------------------------------------------------

    async def _evaluate_structural_candidate(
        self,
        episode_events: List[dict],
        episode_transactions: int,
        episode_actual_failures: int,
        analysis_window_start: datetime,
        analysis_window_end: datetime,
        dimension: str,
        field_name: str,
        snapshot_dim: str,
        value: str,
        all_hist_starts: List[datetime],
    ) -> dict:
        """
        Evaluate a single structural dimension/value pair.
        Returns a dict with all metrics (for audit payload + candidate qualification).
        """
        # Episode segment counts
        seg_events = [
            e for e in episode_events
            if e.get(field_name) == value
            and e["event_type"] in ("payment.captured", "payment.failed")
        ]
        seg_transactions = len(seg_events)
        seg_failures = sum(1 for e in seg_events if e["event_type"] == "payment.failed")

        traffic_share = seg_transactions / episode_transactions if episode_transactions > 0 else 0.0
        failure_share = seg_failures / episode_actual_failures if episode_actual_failures > 0 else 0.0

        # --- Historical baseline from Stage 2 snapshots ---
        snap_models = await self._get_hist_snapshot_for_dim(
            all_hist_starts, snapshot_dim, value
        )

        valid_hist_failures: List[int] = []
        valid_hist_transactions: List[int] = []
        hist_window_tuples: List[tuple] = []
        valid_window_count = 0

        for snap in snap_models:
            if snap.insufficient_volume or snap.transaction_count < MIN_BASELINE_VOLUME:
                continue  # Exclude low-volume historical windows
            valid_hist_failures.append(snap.failed_transaction_count)
            valid_hist_transactions.append(snap.transaction_count)
            valid_window_count += 1
            hist_window_tuples.append((
                snap.window_start.isoformat(),
                snap.failed_transaction_count,
                snap.transaction_count,
                dimension,
                value,
            ))

        baseline_sufficient = valid_window_count >= MIN_VALID_HISTORICAL_WINDOWS

        hist_seg_failure_rate: Optional[float] = None
        hist_seg_failures_total = sum(valid_hist_failures)
        hist_seg_transactions_total = sum(valid_hist_transactions)

        if baseline_sufficient:
            hist_seg_failure_rate = compute_volume_weighted_failure_rate(
                valid_hist_failures, valid_hist_transactions
            )
            if hist_seg_failure_rate is None:
                baseline_sufficient = False  # SUM(hist_transactions) == 0

        expected: float = 0.0
        excess: float = 0.0
        if baseline_sufficient and hist_seg_failure_rate is not None:
            expected, excess = compute_candidate_metrics(
                seg_transactions, seg_failures, hist_seg_failure_rate
            )

        return {
            "dimension": dimension,
            "value": value,
            "episode_segment_transaction_count": seg_transactions,
            "historical_segment_transaction_count": hist_seg_transactions_total,
            "historical_segment_failures_total": hist_seg_failures_total,
            "historical_segment_failure_rate": hist_seg_failure_rate,
            "expected_segment_failures": expected,
            "actual_segment_failures": seg_failures,
            "excess_segment_failures": excess,
            "traffic_share": traffic_share,
            "failure_share": failure_share,
            "excess_failure_contribution": 0.0,  # filled in later
            "evidence_strength": None,
            "qualified_as_candidate": False,
            "disqualification_reason": (
                None if baseline_sufficient
                else f"Insufficient historical baseline: only {valid_window_count} valid window(s) "
                     f"(need >= {MIN_VALID_HISTORICAL_WINDOWS})."
            ),
            "baseline_sufficient": baseline_sufficient,
            "_hist_tuples": hist_window_tuples,
        }

    # ------------------------------------------------------------------
    # Private helpers — supporting error evidence
    # ------------------------------------------------------------------

    async def _evaluate_supporting_dimension(
        self,
        episode_events: List[dict],
        episode_actual_failures: int,
        analysis_window_start: datetime,
        analysis_window_end: datetime,
        support_dim: str,
        field_name: str,
        all_hist_starts: List[datetime],
    ) -> List[dict]:
        """
        Evaluate all values for a supporting evidence dimension.
        Returns list of supporting evidence dicts (NOT CandidateCause rows).
        """
        if episode_actual_failures == 0:
            return []

        failed_events = [
            e for e in episode_events if e["event_type"] == "payment.failed"
        ]
        dim_values = set(
            e.get(field_name) for e in failed_events if e.get(field_name) is not None
        )

        results: List[dict] = []
        for val in sorted(dim_values):
            episode_count = sum(
                1 for e in failed_events if e.get(field_name) == val
            )
            ep_freq = episode_count / episode_actual_failures

            # Historical error frequency via Stage 1 direct query
            hist_error_failures, hist_total_failures = await self._get_hist_error_counts(
                all_hist_starts, field_name, val
            )
            if hist_total_failures == 0:
                baseline_freq = 0.0
            else:
                baseline_freq = hist_error_failures / hist_total_failures

            deviation = ep_freq - baseline_freq
            qualifies = deviation >= MIN_ERROR_DEVIATION_THRESHOLD

            if qualifies:
                results.append({
                    "dimension": support_dim,
                    "value": val,
                    "episode_error_frequency": ep_freq,
                    "baseline_error_frequency": baseline_freq,
                    "error_deviation": deviation,
                    "qualifies_as_supporting": True,
                })

        return results

    async def _get_hist_error_counts(
        self,
        hist_starts: List[datetime],
        field_name: str,
        value: str,
    ) -> Tuple[int, int]:
        """
        Return (hist_error_count, hist_total_failures) for a supporting dimension value.
        """
        if not hist_starts:
            return 0, 0

        from sqlalchemy import or_, and_
        hist_ends = [s + timedelta(minutes=5) for s in hist_starts]
        window_conditions = or_(
            *[
                and_(
                    PaymentEventModel.timestamp >= s,
                    PaymentEventModel.timestamp < e,
                )
                for s, e in zip(hist_starts, hist_ends)
            ]
        )

        field_col = getattr(PaymentEventModel, field_name)

        total_fail_stmt = select(func.count()).where(
            window_conditions,
            PaymentEventModel.event_type == "payment.failed",
        )
        error_fail_stmt = select(func.count()).where(
            window_conditions,
            PaymentEventModel.event_type == "payment.failed",
            field_col == value,
        )

        total_r = await self.session.execute(total_fail_stmt)
        error_r = await self.session.execute(error_fail_stmt)
        return error_r.scalar() or 0, total_r.scalar() or 0

    # ------------------------------------------------------------------
    # Private helpers — UNKNOWN persistence
    # ------------------------------------------------------------------

    async def _persist_unknown(
        self,
        episode: DegradationEpisode,
        analysis_window_start: datetime,
        analysis_window_end: datetime,
        reason: str,
        episode_transaction_count: int,
        episode_actual_failures: int,
        hist_window_tuples: List[tuple],
    ) -> None:
        fingerprint = compute_input_fingerprint(
            episode_id=episode.episode_id,
            episode_status=episode.status.value,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            episode_actual_failures=episode_actual_failures,
            episode_transaction_count=episode_transaction_count,
            historical_window_tuples=hist_window_tuples,
        )
        last = await self.repo.get_latest_evaluation_for_episode(episode.episode_id)
        if last and last.input_fingerprint == fingerprint:
            return

        await self._persist_unknown_with_fingerprint(
            episode=episode,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            reason=reason,
            episode_transaction_count=episode_transaction_count,
            episode_actual_failures=episode_actual_failures,
            fingerprint=fingerprint,
            structural_results=[],
            supporting_evidence=[],
            hist_starts=[],
        )

    async def _persist_unknown_with_fingerprint(
        self,
        episode: DegradationEpisode,
        analysis_window_start: datetime,
        analysis_window_end: datetime,
        reason: str,
        episode_transaction_count: int,
        episode_actual_failures: int,
        fingerprint: str,
        structural_results: List[dict],
        supporting_evidence: List[dict],
        hist_starts: List[datetime],
    ) -> None:
        payload = build_unknown_payload(
            reason=reason,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            episode_id=str(episode.episode_id),
            segment_dimension=episode.segment_dimension,
            segment_value=episode.segment_value,
            episode_status=episode.status.value,
            input_fingerprint=fingerprint,
        )
        # Add structural and supporting context if available
        if structural_results:
            payload["structural_candidates_evaluated"] = _clean_structural_results(
                structural_results
            )
        if supporting_evidence:
            payload["supporting_error_evidence"] = supporting_evidence

        import struct
        k1, k2 = struct.unpack("<ii", episode.episode_id.bytes[:8])
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:k1, :k2)"), {"k1": k1, "k2": k2}
        )

        max_version = await self.repo.get_max_evaluation_version(episode.episode_id)
        new_version = max_version + 1
        evaluation_id = make_evaluation_id(episode.episode_id, new_version)

        evaluation = RootCauseAnalysisEvaluation(
            evaluation_id=evaluation_id,
            episode_id=episode.episode_id,
            evaluation_version=new_version,
            classification=RCAClassification.UNKNOWN,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            input_fingerprint=fingerprint,
            generated_at=datetime.now(timezone.utc),
            evidence_audit_payload=payload,
        )
        await self.repo.append_evaluation(evaluation)
        await self.session.commit()


# ---------------------------------------------------------------------------
# Private module-level helpers
# ---------------------------------------------------------------------------

def _build_hist_window_audit(
    hist_starts: List[datetime],
    structural_results: List[dict],
) -> List[dict]:
    """Build the historical_comparison_windows list for the audit payload."""
    seen: dict = {}
    for r in structural_results:
        for tup in r.get("_hist_tuples", []):
            start_iso = str(tup[0])
            if start_iso not in seen:
                seen[start_iso] = {"start": start_iso, "valid": True}
    for s in hist_starts:
        key = s.isoformat()
        if key not in seen:
            seen[key] = {"start": key, "valid": False, "exclusion_reason": "No snapshot found"}
    return list(seen.values())


def _clean_structural_results(results: List[dict]) -> List[dict]:
    """Remove internal keys from structural results before storing in audit payload."""
    clean = []
    for r in results:
        c = {k: v for k, v in r.items() if not k.startswith("_")}
        c.pop("baseline_sufficient", None)
        clean.append(c)
    return clean
