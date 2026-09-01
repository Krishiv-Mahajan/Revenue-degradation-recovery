from sqlalchemy import Column, String, Integer, DateTime, JSON, UniqueConstraint, Boolean, Float
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base
import uuid

Base = declarative_base()

class RawIngestionRecordModel(Base):
    __tablename__ = "raw_ingestion_records"

    # We use a synthetic UUID primary key for the table to keep clustering simple,
    # but the domain identity is (source_system, source_event_id).
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_system = Column(String(50), nullable=False)
    source_event_id = Column(String(255), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    raw_payload = Column(JSON, nullable=False)
    payload_hash = Column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint('source_system', 'source_event_id', name='uq_raw_ingestion_source_identity'),
    )


class PaymentEventModel(Base):
    __tablename__ = "payment_events"

    # The canonical domain event_id
    event_id = Column(UUID(as_uuid=True), primary_key=True)

    # Domain identity mapped from source
    source_system = Column(String(50), nullable=False)
    source_event_id = Column(String(255), nullable=False)

    # Core payment facts
    payment_id = Column(String(255), nullable=False)
    order_id = Column(String(255), nullable=True)

    timestamp = Column(DateTime(timezone=True), nullable=False)
    event_type = Column(String(100), nullable=False)

    currency = Column(String(3), nullable=False)
    amount_minor_units = Column(Integer, nullable=False)

    payment_status = Column(String(50), nullable=False)

    payment_method = Column(String(50), nullable=True)
    bank = Column(String(50), nullable=True)
    wallet = Column(String(50), nullable=True)

    # Razorpay-specific / Provider error details, normalized loosely
    error_code = Column(String(255), nullable=True)
    error_description = Column(String(1024), nullable=True)
    error_source = Column(String(255), nullable=True)
    error_step = Column(String(255), nullable=True)
    error_reason = Column(String(255), nullable=True)

    ingested_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint('source_system', 'source_event_id', name='uq_payment_event_source_identity'),
    )

class PaymentHealthSnapshotModel(Base):
    __tablename__ = "payment_health_snapshots"

    snapshot_id = Column(UUID(as_uuid=True), primary_key=True)

    window_start = Column(DateTime(timezone=True), nullable=False)
    window_end = Column(DateTime(timezone=True), nullable=False)

    segment_dimension = Column(String(50), nullable=False)
    segment_value = Column(String(255), nullable=False)

    transaction_count = Column(Integer, nullable=False)
    successful_transaction_count = Column(Integer, nullable=False)
    failed_transaction_count = Column(Integer, nullable=False)

    success_rate = Column(Float, nullable=True)
    failure_rate = Column(Float, nullable=True)

    total_gmv_minor_units = Column(Integer, nullable=False)
    successful_gmv_minor_units = Column(Integer, nullable=False)
    failed_gmv_minor_units = Column(Integer, nullable=False)

    baseline_success_rate = Column(Float, nullable=True)
    insufficient_volume = Column(Boolean, nullable=False)

    calculated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint('window_start', 'window_end', 'segment_dimension', 'segment_value', name='uq_snapshot_identity'),
    )

class DegradationSignalModel(Base):
    __tablename__ = "degradation_signals"

    signal_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id = Column(UUID(as_uuid=True), nullable=False)

    segment_dimension = Column(String(50), nullable=False)
    segment_value = Column(String(255), nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=False)

    evaluation_version = Column(Integer, nullable=False)

    signal_type = Column(String(20), nullable=False)  # NORMAL, BAD, LOW_VOLUME, NO_BASELINE
    baseline_success_rate = Column(Float, nullable=True)
    absolute_drop = Column(Float, nullable=True)
    relative_drop = Column(Float, nullable=True)

    evaluation_timestamp = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint('segment_dimension', 'segment_value', 'window_start', 'evaluation_version', name='uq_signal_evaluation_version'),
    )

class DegradationEpisodeModel(Base):
    __tablename__ = "degradation_episodes"

    episode_id = Column(UUID(as_uuid=True), primary_key=True)

    segment_dimension = Column(String(50), nullable=False)
    segment_value = Column(String(255), nullable=False)

    started_at_window = Column(DateTime(timezone=True), nullable=False)
    ended_at_window = Column(DateTime(timezone=True), nullable=True)

    status = Column(String(20), nullable=False)  # ACTIVE, RECOVERED, INVALIDATED

    peak_absolute_drop = Column(Float, nullable=False)
    affected_window_count = Column(Integer, nullable=False)

    severity = Column(String(20), nullable=False) # MODERATE, HIGH, CRITICAL


# ---------------------------------------------------------------------------
# Stage 4 — Root Cause Analysis (append-only tables)
# ---------------------------------------------------------------------------

class RCAEvaluationModel(Base):
    """
    Immutable, versioned RCA evaluation for a degradation episode.

    APPEND-ONLY: This table must NEVER be mutated via UPDATE or DELETE.
    Current truth is derived dynamically via MAX(evaluation_version) per episode_id.
    Immutability is enforced at the repository layer (RCARepository exposes
    only append methods) and verified by integration tests.
    """
    __tablename__ = "rca_evaluations"

    evaluation_id = Column(UUID(as_uuid=True), primary_key=True)

    # FK reference — episode must exist in degradation_episodes
    episode_id = Column(UUID(as_uuid=True), nullable=False)

    # Monotonically increasing per episode_id.
    # Unique constraint enforced by the DB to prevent concurrent duplicate versions.
    evaluation_version = Column(Integer, nullable=False)

    # SYSTEMIC | SEGMENT_SPECIFIC | UNKNOWN
    classification = Column(String(20), nullable=False)

    analysis_window_start = Column(DateTime(timezone=True), nullable=False)
    analysis_window_end = Column(DateTime(timezone=True), nullable=False)

    # SHA-256 fingerprint of deterministic input state (§24 of design).
    # Used to detect whether re-evaluation is necessary.
    input_fingerprint = Column(String(71), nullable=False)  # 'sha256:' + 64 hex chars

    # Wall-clock UTC insertion time. Informational only — never used for
    # re-evaluation decisions (fingerprint is used instead).
    generated_at = Column(DateTime(timezone=True), nullable=False)

    # Full reproducibility JSON snapshot (§20 of design).
    # Stored immutably; must contain enough data to reconstruct the evaluation
    # result without reading source code.
    evidence_audit_payload = Column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "episode_id",
            "evaluation_version",
            name="uq_rca_evaluation_version",
        ),
    )


class CandidateCauseModel(Base):
    """
    A structural dimension/value pair that qualified as a candidate root cause
    in an RCA evaluation.

    APPEND-ONLY: Rows are never updated or deleted.
    Each row is permanently owned by exactly one RCAEvaluationModel via evaluation_id.
    Version N-1 rows are never reused or modified when version N is created.
    """
    __tablename__ = "rca_candidate_causes"

    candidate_id = Column(UUID(as_uuid=True), primary_key=True)

    # Immutable FK to the owning evaluation.
    evaluation_id = Column(UUID(as_uuid=True), nullable=False)

    # Structural dimension only (BANK, CURRENCY, PAYMENT_METHOD, WALLET).
    # Supporting dimensions (ERROR_CODE, etc.) do NOT produce CandidateCause rows.
    candidate_dimension = Column(String(50), nullable=False)
    candidate_value = Column(String(255), nullable=False)

    # STRONG | MODERATE | WEAK
    evidence_strength = Column(String(20), nullable=False)

    # Contribution score (float, unrounded)
    excess_failure_contribution = Column(Float, nullable=False)

    # Segment failure counts
    actual_segment_failures = Column(Integer, nullable=False)
    expected_segment_failures = Column(Float, nullable=False)  # retained as float
    excess_segment_failures = Column(Float, nullable=False)    # retained as float

    # 1-based rank within this evaluation (1 = highest contribution)
    rank = Column(Integer, nullable=False)
