"""
scripts/generate_live_context.py

BUILDATHON CURRENT-TIME RECOVERY CONTEXT GENERATOR

Generates upstream synthetic background traffic around the current UTC time,
then executes the EXISTING Stage 2 -> Stage 3 -> Stage 4 canonical services so that
a real Razorpay TEST payment can arrive while an ACTIVE CRITICAL degradation episode
and STRONG RCA evaluation are organically present.

STRICT CONSTRAINTS:
- NEVER drops, truncates, or resets the database.
- NEVER alters any Stage 1-8 algorithms, thresholds, or mathematical behaviors.
- NEVER directly fabricates downstream records (episodes, RCA evaluations, predictions,
  decisions, commands, observations, attributions).
- Idempotent and safe to run against an active database.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Domain Models & Infrastructure
from src.core.analytics.service import PaymentHealthAnalyticsService
from src.core.degradation.service import DegradationService
from src.core.domain.analytics_models import PaymentHealthSnapshot
from src.core.domain.degradation_models import DegradationEpisode, EpisodeStatus, Severity
from src.core.normalizers.razorpay import generate_deterministic_hash
from src.core.rca.service import RCAService
from src.infrastructure.analytics_repository import AnalyticsRepository
from src.infrastructure.degradation_repository import DegradationRepository
from src.infrastructure.models import (
    CandidateCauseModel,
    DegradationEpisodeModel,
    PaymentEventModel,
    PaymentHealthSnapshotModel,
    RawIngestionRecordModel,
    RCAEvaluationModel,
)
from src.infrastructure.rca_repository import RCARepository

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generate_live_context")


def compute_tumbling_windows(
    now: datetime,
) -> Tuple[datetime, datetime, List[Tuple[datetime, datetime]]]:
    """
    Computes current tumbling window W0 and the three preceding completed windows W-3, W-2, W-1.
    Uses identical 5-minute boundary semantics as PaymentHealthAnalyticsService._get_window_boundaries.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    minute = now.minute
    w0_start_minute = (minute // 5) * 5
    w0_start = now.replace(minute=w0_start_minute, second=0, microsecond=0)
    w0_end = w0_start + timedelta(minutes=5)

    w_minus_1 = (w0_start - timedelta(minutes=5), w0_start)
    w_minus_2 = (w0_start - timedelta(minutes=10), w0_start - timedelta(minutes=5))
    w_minus_3 = (w0_start - timedelta(minutes=15), w0_start - timedelta(minutes=10))

    return w0_start, w0_end, [w_minus_3, w_minus_2, w_minus_1]


async def seed_historical_baselines(
    session: AsyncSession,
    windows: List[Tuple[datetime, datetime]],
    pm: str,
    now: datetime,
) -> int:
    """
    Seeds baseline health snapshots 1 week prior for GLOBAL=ALL and PAYMENT_METHOD=<pm>.
    DO NOT seed a CURRENCY baseline to prevent alphabetical tie-break collision in RCA.
    Idempotent via on_conflict_do_update.
    """
    logger.info("Seeding historical baseline snapshots (1 week prior for GLOBAL and PAYMENT_METHOD)...")
    count = 0
    for w_start, w_end in windows:
        hist_start = w_start - timedelta(weeks=1)
        hist_end = w_end - timedelta(weeks=1)

        for dim, val in [("GLOBAL", "ALL"), ("payment_method", pm)]:
            stmt = pg_insert(PaymentHealthSnapshotModel).values(
                snapshot_id=uuid.uuid4(),
                window_start=hist_start,
                window_end=hist_end,
                segment_dimension=dim,
                segment_value=val,
                transaction_count=200,
                successful_transaction_count=170,
                failed_transaction_count=30,
                success_rate=0.85,
                failure_rate=0.15,
                total_gmv_minor_units=5000000,
                successful_gmv_minor_units=4250000,
                failed_gmv_minor_units=750000,
                baseline_success_rate=0.85,
                insufficient_volume=False,
                calculated_at=now,
            ).on_conflict_do_update(
                index_elements=["window_start", "window_end", "segment_dimension", "segment_value"],
                set_={
                    "transaction_count": 200,
                    "successful_transaction_count": 170,
                    "failed_transaction_count": 30,
                    "success_rate": 0.85,
                    "failure_rate": 0.15,
                    "baseline_success_rate": 0.85,
                    "insufficient_volume": False,
                    "calculated_at": now,
                },
            )
            await session.execute(stmt)
            count += 1

    await session.commit()
    logger.info(f"Persisted {count} historical baseline snapshots.")
    return count


async def seed_upstream_synthetic_traffic(
    session: AsyncSession,
    windows: List[Tuple[datetime, datetime]],
    pm: str,
    curr: str,
    seed: int,
) -> int:
    """
    Generates 60 synthetic transactions (48 failed, 12 captured) per completed window.
    Total = 180 terminal transactions.
    All events use source_system = 'razorpay' so Stage 5 feature reconstruction matches.
    """
    logger.info("Generating upstream synthetic payment events for W-3, W-2, W-1...")
    rng = random.Random(seed)
    total_tx = 0
    raw_records = []
    payment_events = []

    for w_idx, (w_start, w_end) in enumerate(windows):
        # 48 failed (80%), 12 captured (20%)
        outcomes = [True] * 48 + [False] * 12
        rng.shuffle(outcomes)

        for i, is_fail in enumerate(outcomes):
            # Space authorizations between 0s and ~265s into the 300s window
            auth_sec = i * 4.4 + rng.uniform(0.1, 1.2)
            auth_time = w_start + timedelta(seconds=auth_sec)

            # Terminal outcome 5–15 seconds later, strictly within window
            term_sec = rng.uniform(5.0, 15.0)
            term_time = auth_time + timedelta(seconds=term_sec)
            if term_time >= w_end:
                term_time = w_end - timedelta(seconds=rng.uniform(0.5, 2.0))

            tx_uuid = uuid.uuid4().hex[:12]
            pid = f"pay_live_ctx_{tx_uuid}"
            oid = f"order_live_ctx_{tx_uuid}"
            auth_event_id = f"evt_live_auth_{tx_uuid}"
            term_event_id = f"evt_live_term_{tx_uuid}"
            amount = rng.randint(1000, 50000)

            # 1. Authorized event
            auth_payload = {
                "event": "payment.authorized",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": pid,
                            "order_id": oid,
                            "amount": amount,
                            "currency": curr,
                            "method": pm,
                            "status": "authorized",
                        }
                    }
                },
            }
            raw_auth = RawIngestionRecordModel(
                id=uuid.uuid4(),
                source_system="razorpay",
                source_event_id=auth_event_id,
                received_at=auth_time,
                raw_payload=auth_payload,
                payload_hash=generate_deterministic_hash(auth_payload),
            )
            evt_auth = PaymentEventModel(
                event_id=uuid.uuid4(),
                source_system="razorpay",
                source_event_id=auth_event_id,
                payment_id=pid,
                order_id=oid,
                timestamp=auth_time,
                event_type="payment.authorized",
                currency=curr,
                amount_minor_units=amount,
                payment_status="authorized",
                payment_method=pm,
                bank=None,
                wallet=None,
                ingested_at=auth_time,
            )

            # 2. Terminal event (payment.failed or payment.captured)
            term_type = "payment.failed" if is_fail else "payment.captured"
            term_status = "failed" if is_fail else "captured"
            term_payload = {
                "event": term_type,
                "payload": {
                    "payment": {
                        "entity": {
                            "id": pid,
                            "order_id": oid,
                            "amount": amount,
                            "currency": curr,
                            "method": pm,
                            "status": term_status,
                        }
                    }
                },
            }
            raw_term = RawIngestionRecordModel(
                id=uuid.uuid4(),
                source_system="razorpay",
                source_event_id=term_event_id,
                received_at=term_time,
                raw_payload=term_payload,
                payload_hash=generate_deterministic_hash(term_payload),
            )
            evt_term = PaymentEventModel(
                event_id=uuid.uuid4(),
                source_system="razorpay",
                source_event_id=term_event_id,
                payment_id=pid,
                order_id=oid,
                timestamp=term_time,
                event_type=term_type,
                currency=curr,
                amount_minor_units=amount,
                payment_status=term_status,
                payment_method=pm,
                bank=None,
                wallet=None,
                error_code="BAD_REQUEST_ERROR" if is_fail else None,
                error_description="Payment failed on issuer side" if is_fail else None,
                error_source="gateway" if is_fail else None,
                error_step="payment_authorization" if is_fail else None,
                error_reason="payment_failed" if is_fail else None,
                ingested_at=term_time,
            )

            raw_records.extend([raw_auth, raw_term])
            payment_events.extend([evt_auth, evt_term])
            total_tx += 1

    session.add_all(raw_records)
    session.add_all(payment_events)
    await session.commit()
    logger.info(f"Persisted {len(raw_records)} raw records and {len(payment_events)} payment events ({total_tx} transactions).")
    return total_tx


async def run_stage2_analytics(
    session: AsyncSession,
    windows: List[Tuple[datetime, datetime]],
) -> List[Dict[str, any]]:
    """
    Invokes existing PaymentHealthAnalyticsService.calculate_and_save_window()
    for each of W-3, W-2, W-1.
    """
    logger.info("Executing Stage 2 Analytics organically via PaymentHealthAnalyticsService...")
    analytics_repo = AnalyticsRepository(session)
    analytics_svc = PaymentHealthAnalyticsService(analytics_repo)

    results = []
    for w_idx, (w_start, w_end) in enumerate(windows):
        await analytics_svc.calculate_and_save_window(w_start, w_end)

        # Verify snapshots from database
        stmt = select(PaymentHealthSnapshotModel).where(
            PaymentHealthSnapshotModel.window_start == w_start,
            PaymentHealthSnapshotModel.window_end == w_end,
            PaymentHealthSnapshotModel.segment_dimension == "GLOBAL",
            PaymentHealthSnapshotModel.segment_value == "ALL",
        )
        res = await session.execute(stmt)
        snap = res.scalar_one_or_none()

        if not snap:
            raise RuntimeError(f"Stage 2 failed to produce GLOBAL/ALL snapshot for window {w_start.isoformat()}")

        results.append({
            "window_index": w_idx - 3,
            "window_start": w_start,
            "window_end": w_end,
            "transaction_count": snap.transaction_count,
            "failed_count": snap.failed_transaction_count,
            "failure_rate": snap.failure_rate,
            "baseline_success_rate": snap.baseline_success_rate,
            "insufficient_volume": snap.insufficient_volume,
        })
        logger.info(
            f"  Window W{w_idx - 3} [{w_start.strftime('%H:%M')}–{w_end.strftime('%H:%M')}]: "
            f"tx={snap.transaction_count} fail_rate={snap.failure_rate:.4f} "
            f"baseline={snap.baseline_success_rate} insufficient_vol={snap.insufficient_volume}"
        )

    return results


async def run_stage3_degradation(
    session: AsyncSession,
    windows: List[Tuple[datetime, datetime]],
) -> List[DegradationEpisode]:
    """
    Invokes existing DegradationService.process_snapshots() sequentially
    for W-3, W-2, W-1 using canonical grouping by segment.
    """
    logger.info("Executing Stage 3 Degradation Detection organically via DegradationService...")
    deg_repo = DegradationRepository(session)
    deg_svc = DegradationService(deg_repo)

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

    for w_start, w_end in windows:
        snap_res = await session.execute(snap_stmt, {"w_start": w_start, "w_end": w_end})
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
            await deg_svc.process_snapshots(seg_snapshots, evaluation_timestamp=w_end)

        await session.commit()

    # Query active episodes created by Stage 3
    ep_stmt = text(
        """
        SELECT episode_id, segment_dimension, segment_value, started_at_window, ended_at_window, status, peak_absolute_drop, affected_window_count, severity
        FROM degradation_episodes
        WHERE status = 'ACTIVE'
        """
    )
    ep_res = await session.execute(ep_stmt)
    active_episodes = [
        DegradationEpisode(
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
        for row in ep_res.fetchall()
    ]

    logger.info(f"Stage 3 completed. Found {len(active_episodes)} ACTIVE episode(s).")
    return active_episodes


async def run_stage4_rca(
    session: AsyncSession,
    active_episodes: List[DegradationEpisode],
    target_pm: str,
) -> Dict[str, any]:
    """
    Invokes existing RCAService.evaluate_episode() for active episodes.
    Verifies that rank-1 candidate cause is PAYMENT_METHOD=<target_pm> with STRONG evidence.
    """
    logger.info("Executing Stage 4 Root Cause Analysis organically via RCAService...")
    rca_repo = RCARepository(session)
    rca_svc = RCAService(session, rca_repo)

    for ep in active_episodes:
        await rca_svc.evaluate_episode(ep)

    await session.commit()

    # Verify RCA evaluation for the active episodes
    verified_rca = None
    for ep in active_episodes:
        eval_stmt = (
            select(RCAEvaluationModel)
            .where(RCAEvaluationModel.episode_id == ep.episode_id)
            .order_by(RCAEvaluationModel.evaluation_version.desc())
            .limit(1)
        )
        eval_res = await session.execute(eval_stmt)
        rca_eval = eval_res.scalar_one_or_none()

        if rca_eval:
            cand_stmt = (
                select(CandidateCauseModel)
                .where(
                    CandidateCauseModel.evaluation_id == rca_eval.evaluation_id,
                    CandidateCauseModel.rank == 1,
                )
            )
            cand_res = await session.execute(cand_stmt)
            cand = cand_res.scalar_one_or_none()

            if cand and cand.candidate_dimension == "PAYMENT_METHOD" and cand.candidate_value == target_pm:
                verified_rca = {
                    "episode_id": ep.episode_id,
                    "classification": rca_eval.classification,
                    "candidate_dimension": cand.candidate_dimension,
                    "candidate_value": cand.candidate_value,
                    "evidence_strength": cand.evidence_strength,
                    "excess_failure_contribution": cand.excess_failure_contribution,
                    "rank": cand.rank,
                }
                break

    if not verified_rca:
        # Check if any candidate was produced
        diag_stmt = text(
            """
            SELECT e.episode_id, e.classification, c.candidate_dimension, c.candidate_value, c.evidence_strength, c.rank
            FROM rca_evaluations e
            LEFT JOIN rca_candidate_causes c ON e.evaluation_id = c.evaluation_id
            ORDER BY e.generated_at DESC
            LIMIT 5
            """
        )
        diag_res = await session.execute(diag_stmt)
        rows = diag_res.fetchall()
        logger.warning(f"RCA candidate diagnostic rows: {rows}")
        raise RuntimeError(f"RCA did not produce rank-1 candidate PAYMENT_METHOD={target_pm}. Diagnostic: {rows}")

    logger.info(
        f"Stage 4 RCA verified: episode={verified_rca['episode_id']} "
        f"candidate={verified_rca['candidate_dimension']}={verified_rca['candidate_value']} "
        f"strength={verified_rca['evidence_strength']} rank={verified_rca['rank']}"
    )
    return verified_rca


async def check_lookback_terminal_volume(session: AsyncSession, now: datetime) -> int:
    """
    Verifies that the preceding 30 minutes contain >= 50 terminal transactions
    for source_system = 'razorpay'.
    """
    t_30m_ago = now - timedelta(minutes=30)
    stmt = (
        select(func.count())
        .select_from(PaymentEventModel)
        .where(
            PaymentEventModel.source_system == "razorpay",
            PaymentEventModel.event_type.in_(["payment.captured", "payment.failed"]),
            PaymentEventModel.timestamp >= t_30m_ago,
            PaymentEventModel.timestamp <= now,
        )
    )
    res = await session.execute(stmt)
    return res.scalar() or 0


async def main_async(pm: str, curr: str, seed: int) -> int:
    engine = create_async_engine(DATABASE_URL, echo=False)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    w0_start, w0_end, windows = compute_tumbling_windows(now)

    print("\n" + "=" * 60)
    print("  BUILDATHON CURRENT-TIME RECOVERY CONTEXT GENERATION")
    print("=" * 60)
    print(f"Execution Wall-Clock UTC : {now.isoformat()}")
    print(f"Target Payment Method   : {pm}")
    print(f"Target Currency         : {curr}")
    print(f"Current Window W0       : {w0_start.strftime('%H:%M')} – {w0_end.strftime('%H:%M')} UTC")
    print(f"Window W-3              : {windows[0][0].strftime('%H:%M')} – {windows[0][1].strftime('%H:%M')} UTC")
    print(f"Window W-2              : {windows[1][0].strftime('%H:%M')} – {windows[1][1].strftime('%H:%M')} UTC")
    print(f"Window W-1              : {windows[2][0].strftime('%H:%M')} – {windows[2][1].strftime('%H:%M')} UTC")
    print("=" * 60 + "\n")

    async with Session() as session:
        # Task 3: Seed historical baselines
        await seed_historical_baselines(session, windows, pm, now)

        # Task 4 & 5: Seed synthetic traffic
        total_tx = await seed_upstream_synthetic_traffic(session, windows, pm, curr, seed)

        # Task 6: Run Stage 2 Analytics
        s2_results = await run_stage2_analytics(session, windows)

        # Task 7: Run Stage 3 Degradation Detection
        active_episodes = await run_stage3_degradation(session, windows)
        if not active_episodes:
            raise RuntimeError("Stage 3 completed but produced 0 ACTIVE episodes.")

        # Find episode with CRITICAL severity
        critical_ep = next((ep for ep in active_episodes if ep.severity == Severity.CRITICAL), active_episodes[0])

        # Task 8: Run Stage 4 RCA
        rca_result = await run_stage4_rca(session, active_episodes, pm)

        # Lookback verification
        terminal_volume_30m = await check_lookback_terminal_volume(session, now)

    await engine.dispose()

    # Expiry calculation:
    # Lookback window is [T_live - 30m, T_live).
    # Oldest synthetic event is at windows[0][0] (W-3 start).
    # Traffic remains >= 50 until W-2 start + 30m (approx 15-20 min from now).
    expiry_estimate = windows[1][0] + timedelta(minutes=30)
    minutes_left = max(1, int((expiry_estimate - now).total_seconds() / 60))

    # Task 13: Print operator readiness summary
    print("\n" + "=" * 60)
    print("  LIVE RAZORPAY RECOVERY CONTEXT READY")
    print("=" * 60)
    print(f"Current UTC             : {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"Target Payment Method   : {pm}")
    print(f"Target Currency         : {curr}\n")

    for res in s2_results:
        w_name = f"Window W{res['window_index']}"
        print(f"{w_name:12}: {res['window_start'].strftime('%H:%M')}–{res['window_end'].strftime('%H:%M')} UTC")
        print(f"              {res['transaction_count']} transactions (~{res['failure_rate']*100:.1f}% failure)")
        print(f"              Stage 2: PASS (baseline={res['baseline_success_rate']:.2f}, insufficient_vol={res['insufficient_volume']})")

    print("\n" + "-" * 60)
    print(f"Active Episode ID       : {critical_ep.episode_id}")
    print(f"Severity                : {critical_ep.severity.value}")
    print(f"RCA Candidate           : {rca_result['candidate_dimension']} = {rca_result['candidate_value']}")
    print(f"RCA Evidence Strength   : {rca_result['evidence_strength']}")
    print(f"30-min Terminal Volume  : {terminal_volume_30m} (Threshold >= 50: PASS)")
    print(f"Prediction Context      : READY (High Risk + ACT expected on card)")
    print("-" * 60)
    print("  MAKE THE REAL RAZORPAY TEST PAYMENT NOW")
    print("-" * 60)
    print(f"1. Make the payment during the current W0 window [{w0_start.strftime('%H:%M')}–{w0_end.strftime('%H:%M')} UTC].")
    print(f"2. Context lookback traffic will remain valid for approximately {minutes_left} minutes.")
    print("3. Payment method must be CARD (matching payment_method = card).")
    print("4. Real webhook will enter POST /ingest/razorpay and trigger live pipeline.")
    print("=" * 60 + "\n")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Generate live recovery context for Razorpay buildathon test.")
    parser.add_argument("--pm", default="card", help="Payment method (default: card)")
    parser.add_argument("--curr", default="INR", help="Currency code (default: INR)")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic random seed (default: 42)")

    args = parser.parse_args()

    try:
        ret = asyncio.run(main_async(args.pm, args.curr, args.seed))
        sys.exit(ret)
    except Exception as e:
        logger.error(f"Execution failed: {type(e).__name__}: {str(e)}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
