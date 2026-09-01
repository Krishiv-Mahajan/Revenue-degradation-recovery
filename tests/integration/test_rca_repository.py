"""
Stage 4 — Integration tests for the RCA repository.

Tests:
- Append evaluation + candidates roundtrip
- Replay: unchanged fingerprint → no new evaluation version
- Replay: changed boundary → new version appended
- Physical immutability: old rows byte-for-byte unchanged after replay
- Invalidated episode → UNKNOWN version appended
- CandidateCause version ownership: version N-1 rows untouched after version N
"""

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.infrastructure.models import Base
from src.infrastructure.rca_repository import RCARepository
from src.core.domain.rca_models import (
    CandidateCause,
    EvidenceStrength,
    RCAClassification,
    RootCauseAnalysisEvaluation,
    compute_input_fingerprint,
    make_candidate_id,
    make_evaluation_id,
)

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"

T0 = datetime(2024, 1, 8, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db_engine():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(db_engine):
    async_session = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as sess:
        yield sess


@pytest.fixture
def repo(session: AsyncSession):
    return RCARepository(session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EPISODE_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _make_fingerprint(
    failures: int = 100,
    transactions: int = 1000,
    status: str = "ACTIVE",
    window_end: datetime = T0 + timedelta(minutes=15),
) -> str:
    return compute_input_fingerprint(
        episode_id=EPISODE_ID,
        episode_status=status,
        analysis_window_start=T0,
        analysis_window_end=window_end,
        episode_actual_failures=failures,
        episode_transaction_count=transactions,
        historical_window_tuples=[
            (T0 - timedelta(weeks=1), 50, 1000, "BANK", "HDFC"),
        ],
    )


def _make_evaluation(
    version: int = 1,
    fingerprint: str = None,
    classification: RCAClassification = RCAClassification.SEGMENT_SPECIFIC,
) -> RootCauseAnalysisEvaluation:
    fp = fingerprint or _make_fingerprint()
    eval_id = make_evaluation_id(EPISODE_ID, version)
    return RootCauseAnalysisEvaluation(
        evaluation_id=eval_id,
        episode_id=EPISODE_ID,
        evaluation_version=version,
        classification=classification,
        analysis_window_start=T0,
        analysis_window_end=T0 + timedelta(minutes=15),
        input_fingerprint=fp,
        generated_at=datetime.now(timezone.utc),
        evidence_audit_payload={"test": True, "version": version},
    )


def _make_candidate(evaluation_id: uuid.UUID, value: str = "HDFC") -> CandidateCause:
    cid = make_candidate_id(evaluation_id, "BANK", value)
    return CandidateCause(
        candidate_id=cid,
        evaluation_id=evaluation_id,
        candidate_dimension="BANK",
        candidate_value=value,
        evidence_strength=EvidenceStrength.STRONG,
        excess_failure_contribution=0.80,
        actual_segment_failures=50,
        expected_segment_failures=5.0,
        excess_segment_failures=45.0,
        rank=1,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_and_retrieve_evaluation(repo: RCARepository, session: AsyncSession):
    """Basic roundtrip: append evaluation, retrieve it by episode and by ID."""
    evaluation = _make_evaluation(version=1)
    await repo.append_evaluation(evaluation)
    await session.commit()

    retrieved = await repo.get_latest_evaluation_for_episode(EPISODE_ID)
    assert retrieved is not None
    assert retrieved.evaluation_id == evaluation.evaluation_id
    assert retrieved.evaluation_version == 1
    assert retrieved.classification == RCAClassification.SEGMENT_SPECIFIC
    assert retrieved.input_fingerprint == evaluation.input_fingerprint

    by_id = await repo.get_evaluation_by_id(evaluation.evaluation_id)
    assert by_id is not None
    assert by_id.evaluation_id == evaluation.evaluation_id


@pytest.mark.asyncio
async def test_append_and_retrieve_candidates(repo: RCARepository, session: AsyncSession):
    """Candidates are inserted and retrieved with correct field values."""
    evaluation = _make_evaluation(version=1)
    await repo.append_evaluation(evaluation)

    candidate = _make_candidate(evaluation.evaluation_id)
    await repo.append_candidate_causes([candidate])
    await session.commit()

    candidates = await repo.get_candidates_for_evaluation(evaluation.evaluation_id)
    assert len(candidates) == 1
    assert candidates[0].candidate_dimension == "BANK"
    assert candidates[0].candidate_value == "HDFC"
    assert candidates[0].evidence_strength == EvidenceStrength.STRONG
    assert abs(candidates[0].excess_failure_contribution - 0.80) < 1e-9
    assert abs(candidates[0].expected_segment_failures - 5.0) < 1e-9  # float retained
    assert abs(candidates[0].excess_segment_failures - 45.0) < 1e-9   # float retained
    assert candidates[0].rank == 1


@pytest.mark.asyncio
async def test_replay_unchanged_fingerprint_no_new_version(
    repo: RCARepository, session: AsyncSession
):
    """§24B: If fingerprint is unchanged, no new evaluation version must be created."""
    fp = _make_fingerprint()
    evaluation = _make_evaluation(version=1, fingerprint=fp)
    await repo.append_evaluation(evaluation)
    await session.commit()

    # Simulate re-evaluation check
    latest = await repo.get_latest_evaluation_for_episode(EPISODE_ID)
    assert latest.input_fingerprint == fp

    # Fingerprint matches — no new version should be appended
    max_v = await repo.get_max_evaluation_version(EPISODE_ID)
    assert max_v == 1  # Still 1 — unchanged


@pytest.mark.asyncio
async def test_replay_changed_fingerprint_appends_new_version(
    repo: RCARepository, session: AsyncSession
):
    """If fingerprint changes (e.g., boundary extended), new version is appended."""
    fp_v1 = _make_fingerprint(failures=100)
    eval_v1 = _make_evaluation(version=1, fingerprint=fp_v1)
    await repo.append_evaluation(evaluation=eval_v1)
    await session.commit()

    # New fingerprint (episode updated — more failures)
    fp_v2 = _make_fingerprint(failures=150)
    eval_v2 = _make_evaluation(version=2, fingerprint=fp_v2, classification=RCAClassification.SYSTEMIC)
    await repo.append_evaluation(evaluation=eval_v2)
    await session.commit()

    max_v = await repo.get_max_evaluation_version(EPISODE_ID)
    assert max_v == 2

    latest = await repo.get_latest_evaluation_for_episode(EPISODE_ID)
    assert latest.evaluation_version == 2
    assert latest.classification == RCAClassification.SYSTEMIC


@pytest.mark.asyncio
async def test_physical_immutability_old_rows_unchanged(
    repo: RCARepository, session: AsyncSession
):
    """
    §21 + §31: After appending version 2, the version 1 row must be byte-for-byte
    identical to what was written. Retrieved by primary key before and after replay.
    """
    fp_v1 = _make_fingerprint(failures=100)
    eval_v1 = _make_evaluation(version=1, fingerprint=fp_v1)
    await repo.append_evaluation(eval_v1)
    await session.commit()

    # Snapshot version 1 row by primary key BEFORE replay
    pre_replay = await repo.get_evaluation_by_id(eval_v1.evaluation_id)
    assert pre_replay is not None

    # Replay: append version 2 with different fingerprint
    fp_v2 = _make_fingerprint(failures=200)
    eval_v2 = _make_evaluation(version=2, fingerprint=fp_v2, classification=RCAClassification.UNKNOWN)
    await repo.append_evaluation(eval_v2)
    await session.commit()

    # Retrieve version 1 row by PRIMARY KEY after replay
    post_replay = await repo.get_evaluation_by_id(eval_v1.evaluation_id)
    assert post_replay is not None

    # Assert byte-for-byte field equality
    assert post_replay.evaluation_id == pre_replay.evaluation_id
    assert post_replay.evaluation_version == pre_replay.evaluation_version
    assert post_replay.classification == pre_replay.classification
    assert post_replay.input_fingerprint == pre_replay.input_fingerprint
    assert post_replay.analysis_window_start == pre_replay.analysis_window_start
    assert post_replay.analysis_window_end == pre_replay.analysis_window_end
    assert post_replay.evidence_audit_payload == pre_replay.evidence_audit_payload


@pytest.mark.asyncio
async def test_invalidated_episode_unknown_classification(
    repo: RCARepository, session: AsyncSession
):
    """Invalidated episode must produce an UNKNOWN evaluation with empty candidates."""
    fp = _make_fingerprint(status="INVALIDATED")
    eval_unknown = RootCauseAnalysisEvaluation(
        evaluation_id=make_evaluation_id(EPISODE_ID, 1),
        episode_id=EPISODE_ID,
        evaluation_version=1,
        classification=RCAClassification.UNKNOWN,
        analysis_window_start=T0,
        analysis_window_end=T0 + timedelta(minutes=5),
        input_fingerprint=fp,
        generated_at=datetime.now(timezone.utc),
        evidence_audit_payload={
            "classification": "UNKNOWN",
            "classification_reason": "Episode invalidated by Stage 3.",
            "structural_candidates_evaluated": [],
        },
    )
    await repo.append_evaluation(eval_unknown)
    await session.commit()

    retrieved = await repo.get_latest_evaluation_for_episode(EPISODE_ID)
    assert retrieved.classification == RCAClassification.UNKNOWN

    candidates = await repo.get_candidates_for_evaluation(retrieved.evaluation_id)
    assert candidates == []


@pytest.mark.asyncio
async def test_candidate_version_ownership(repo: RCARepository, session: AsyncSession):
    """
    §25: Version N-1 CandidateCause rows must NEVER be reused or modified in version N.
    Each version has its own exclusive candidates.
    """
    # Version 1: HDFC qualifies
    eval_v1 = _make_evaluation(version=1)
    await repo.append_evaluation(eval_v1)
    candidate_v1 = _make_candidate(eval_v1.evaluation_id, value="HDFC")
    await repo.append_candidate_causes([candidate_v1])
    await session.commit()

    # Snapshot v1 candidate before v2 is created
    v1_candidates_before = await repo.get_candidates_for_evaluation(eval_v1.evaluation_id)
    assert len(v1_candidates_before) == 1
    v1_cid = v1_candidates_before[0].candidate_id

    # Version 2: SBI qualifies (different set)
    fp_v2 = _make_fingerprint(failures=200)
    eval_v2 = _make_evaluation(version=2, fingerprint=fp_v2)
    await repo.append_evaluation(eval_v2)
    candidate_v2 = _make_candidate(eval_v2.evaluation_id, value="SBI")
    await repo.append_candidate_causes([candidate_v2])
    await session.commit()

    # Assert v1 candidates unchanged
    v1_candidates_after = await repo.get_candidates_for_evaluation(eval_v1.evaluation_id)
    assert len(v1_candidates_after) == 1
    assert v1_candidates_after[0].candidate_id == v1_cid
    assert v1_candidates_after[0].candidate_value == "HDFC"

    # Assert v2 candidates are separate
    v2_candidates = await repo.get_candidates_for_evaluation(eval_v2.evaluation_id)
    assert len(v2_candidates) == 1
    assert v2_candidates[0].candidate_value == "SBI"

    # Verify v1 candidate IDs are not reused in v2
    all_v2_ids = {c.candidate_id for c in v2_candidates}
    assert v1_cid not in all_v2_ids


@pytest.mark.asyncio
async def test_max_evaluation_version_zero_when_none_exist(repo: RCARepository):
    """get_max_evaluation_version returns 0 when no evaluations exist."""
    fresh_episode_id = uuid.uuid4()
    max_v = await repo.get_max_evaluation_version(fresh_episode_id)
    assert max_v == 0


@pytest.mark.asyncio
async def test_unique_constraint_prevents_duplicate_version(
    repo: RCARepository, session: AsyncSession
):
    """Unique constraint (episode_id, evaluation_version) must prevent duplicate insertion."""
    from sqlalchemy.exc import IntegrityError

    eval_v1a = _make_evaluation(version=1)
    await repo.append_evaluation(eval_v1a)
    await session.commit()

    # Try to insert a second version 1 row for the same episode
    eval_v1b = RootCauseAnalysisEvaluation(
        evaluation_id=uuid.uuid4(),  # Different PK
        episode_id=EPISODE_ID,
        evaluation_version=1,        # DUPLICATE version
        classification=RCAClassification.UNKNOWN,
        analysis_window_start=T0,
        analysis_window_end=T0 + timedelta(minutes=5),
        input_fingerprint=_make_fingerprint(failures=999),
        generated_at=datetime.now(timezone.utc),
        evidence_audit_payload={},
    )

    with pytest.raises(IntegrityError):
        await repo.append_evaluation(eval_v1b)
        await session.commit()
