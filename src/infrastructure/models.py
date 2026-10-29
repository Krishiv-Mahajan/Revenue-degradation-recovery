from sqlalchemy import Column, String, Integer, DateTime, JSON, UniqueConstraint, Boolean, Float, Numeric, ForeignKey, Index, CheckConstraint, ForeignKeyConstraint
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
        # Target for composite foreign keys that pin a row to an event of the same provider.
        UniqueConstraint('event_id', 'source_system', name='uq_payment_event_provider'),
        Index('idx_payment_events_timestamp_event_type', 'timestamp', 'event_type'),
        Index('ix_payment_events_eligibility', 'payment_id', 'ingested_at'),
        Index('ix_payment_events_hist_pm', 'payment_method', 'ingested_at', 'event_type'),
        Index('ix_payment_events_hist_bank', 'bank', 'ingested_at', 'event_type'),
        Index('ix_payment_events_hist_wallet', 'wallet', 'ingested_at', 'event_type'),
        Index('ix_payment_events_hist_curr', 'currency', 'ingested_at', 'event_type'),
        Index('ix_payment_events_provider_order', 'source_system', 'order_id', 'ingested_at'),
    )


# Must match the Identifier grammar in src/core/parsers/paypal.py (enforced by a test).
PAYPAL_IDENTIFIER_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
PAYMENT_ATTEMPT_SOURCE_SYSTEM_CHECK = "source_system IN ('razorpay', 'synthetic', 'paypal')"
PAYMENT_ATTEMPT_NAMESPACE_CHECK = (
    "CASE WHEN source_system = 'paypal' "
    f"THEN provider_reference ~ '{PAYPAL_IDENTIFIER_PATTERN}' "
    "AND payment_attempt_id = 'paypal-order:' || provider_reference "
    "ELSE provider_reference <> '' AND payment_attempt_id = provider_reference "
    "AND left(payment_attempt_id, 13) <> 'paypal-order:' END"
)


class PaymentAttemptModel(Base):
    """
    Registry of Stage 5-8 payment attempts and the provider each belongs to.
    The provider is proven by the attempt's trigger event (same event_id and source_system).
    """
    __tablename__ = "payment_attempts"

    payment_attempt_id = Column(String(255), primary_key=True)
    source_system = Column(String(50), nullable=False)
    # Razorpay/synthetic payment_id, or PayPal order_id.
    provider_reference = Column(String(255), nullable=False)
    trigger_event_id = Column(UUID(as_uuid=True), nullable=False)
    registered_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint('payment_attempt_id', 'source_system', name='uq_payment_attempt_provider'),
        UniqueConstraint('source_system', 'provider_reference', name='uq_payment_attempt_reference'),
        CheckConstraint(PAYMENT_ATTEMPT_SOURCE_SYSTEM_CHECK, name='ck_payment_attempt_source_system'),
        CheckConstraint(PAYMENT_ATTEMPT_NAMESPACE_CHECK, name='ck_payment_attempt_namespace'),
        ForeignKeyConstraint(
            ['trigger_event_id', 'source_system'],
            ['payment_events.event_id', 'payment_events.source_system'],
            name='fk_payment_attempt_trigger',
        ),
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
        Index('ix_degradation_signals_stage3_as_of', 'segment_dimension', 'segment_value', 'evaluation_timestamp'),
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

class EpisodeStateHistoryModel(Base):
    """
    Immutable historical ledger of Stage 3 assertions.
    """
    __tablename__ = "episode_state_history"

    history_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reconciliation_run_id = Column(UUID(as_uuid=True), nullable=False)
    episode_id = Column(UUID(as_uuid=True), nullable=False)
    
    segment_dimension = Column(String(50), nullable=False)
    segment_value = Column(String(255), nullable=False)
    
    status = Column(String(20), nullable=False)
    severity = Column(String(20), nullable=False)
    
    effective_start_window = Column(DateTime(timezone=True), nullable=False)
    evaluation_timestamp = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index('ix_episode_state_history_as_of', 'segment_dimension', 'segment_value', 'evaluation_timestamp'),
        Index('ix_episode_state_history_run', 'reconciliation_run_id'),
    )


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
        Index("ix_rca_evaluations_as_of", "episode_id", "generated_at", "evaluation_version"),
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
    evaluation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("rca_evaluations.evaluation_id", name="fk_candidate_cause_evaluation"),
        nullable=False,
    )

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

# ---------------------------------------------------------------------------
# Stage 5 — Failure Prediction (append-only table)
# ---------------------------------------------------------------------------

class FailurePredictionModel(Base):
    """
    Immutable, versioned failure prediction for a payment attempt.
    
    APPEND-ONLY: This table must NEVER be mutated via UPDATE or DELETE.
    """
    __tablename__ = "failure_predictions"

    prediction_id = Column(UUID(as_uuid=True), primary_key=True)
    
    # The payment_id being predicted on
    payment_attempt_id = Column(String(255), nullable=False)

    # Provider of the attempt (payment_attempts.source_system); NOT NULL under Revision B.
    source_system = Column(String(50), nullable=False)
    
    # Monotonically increasing version per payment_attempt_id
    prediction_version = Column(Integer, nullable=False)
    
    # Prediction timestamp T (ingested_at of the triggering event)
    predicted_at = Column(DateTime(timezone=True), nullable=False)
    
    # E.g. "30m"
    prediction_horizon = Column(String(50), nullable=False)
    
    # Calibrated probability in [0,1]. Null if status != PREDICTED.
    failure_probability = Column(Float, nullable=True)
    
    # LOW / ELEVATED / HIGH. Null if status != PREDICTED.
    risk_band = Column(String(20), nullable=True)
    
    # PREDICTED / INSUFFICIENT_DATA / NOT_ELIGIBLE
    prediction_status = Column(String(20), nullable=False)
    
    model_name = Column(String(100), nullable=False)
    model_version = Column(String(50), nullable=False)
    feature_schema_version = Column(String(50), nullable=False)
    
    # Full serialized feature vector
    feature_snapshot = Column(JSON, nullable=False)
    
    # SHA-256 fingerprint of deterministic input state
    input_fingerprint = Column(String(71), nullable=False)
    
    # Wall-clock UTC insertion time
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "payment_attempt_id",
            "source_system",
            "prediction_version",
            name="uq_failure_prediction_version",
        ),
        UniqueConstraint("prediction_id", "source_system", name="uq_failure_prediction_provider"),
        ForeignKeyConstraint(
            ["payment_attempt_id", "source_system"],
            ["payment_attempts.payment_attempt_id", "payment_attempts.source_system"],
            name="fk_failure_predictions_payment_attempt",
        ),
    )


# ---------------------------------------------------------------------------
# Stage 6 — Intervention Decisioning (append-only table)
# ---------------------------------------------------------------------------

class InterventionDecisionModel(Base):
    """
    Immutable, versioned intervention decision for a payment attempt.

    APPEND-ONLY: This table must NEVER be mutated via UPDATE or DELETE.
    """
    __tablename__ = "intervention_decisions"

    decision_id = Column(UUID(as_uuid=True), primary_key=True)

    # The payment_attempt_id (or payment_id) being decided on
    payment_attempt_id = Column(String(255), nullable=False)

    # Provider of the attempt (payment_attempts.source_system); NOT NULL under Revision B.
    source_system = Column(String(50), nullable=False)

    # Monotonically increasing version per payment_attempt_id
    decision_version = Column(Integer, nullable=False)

    # Decision timestamp T_decide
    decided_at = Column(DateTime(timezone=True), nullable=False)

    # Decision verdict: NO_ACTION | MONITOR | ACT
    decision_type = Column(String(20), nullable=False)

    # Statically permitted route key. Null unless decision_type == ACT
    selected_route_id = Column(String(50), nullable=True)

    # Factual carry-forward from Stage 5 (calibrated failure probability)
    failure_probability = Column(Float, nullable=True)

    # Reference to underlying Stage 5 prediction
    stage5_prediction_id = Column(UUID(as_uuid=True), nullable=True)

    # Factual carry-forward from Stage 4 RCA (STRONG, MODERATE, WEAK, or None)
    diagnosis_confidence = Column(String(20), nullable=True)

    # Stage 6's confidence in its own decision verdict in [0.0, 1.0]
    decision_confidence = Column(Float, nullable=False)

    # Gate verdict: PASSED or specific failed gate code
    gate_verdict = Column(String(50), nullable=False)

    # Policy identification
    policy_id = Column(String(50), nullable=False)
    policy_version = Column(String(50), nullable=False)

    # SHA-256 fingerprint of deterministic input state
    input_fingerprint = Column(String(71), nullable=False)

    # Full audit payload for decision reproducibility
    evaluation_audit_payload = Column(JSON, nullable=False)

    # Wall-clock UTC insertion time
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "payment_attempt_id",
            "source_system",
            "decision_version",
            name="uq_intervention_decision_version",
        ),
        UniqueConstraint("decision_id", "source_system", name="uq_intervention_decision_provider"),
        ForeignKeyConstraint(
            ["payment_attempt_id", "source_system"],
            ["payment_attempts.payment_attempt_id", "payment_attempts.source_system"],
            name="fk_intervention_decisions_payment_attempt",
        ),
        Index("ix_intervention_decisions_attempt_lookup", "payment_attempt_id", "decision_version"),
        Index("ix_intervention_decisions_fingerprint", "input_fingerprint"),
        Index("ix_intervention_decisions_rate_limiting", "decision_type", "decided_at"),
        Index("ix_intervention_decisions_route_cooldown", "payment_attempt_id", "selected_route_id", "decided_at"),
    )


# ---------------------------------------------------------------------------
# Stage 7 — Intervention Execution & Outcome Observation
# ---------------------------------------------------------------------------

class InterventionCommandModel(Base):
    """
    Authoritative lifecycle model for an intervention command.
    Identity and payload are immutable; command_status, status_reason,
    and updated_at are the authoritative mutable lifecycle fields.
    """
    __tablename__ = "intervention_commands"

    command_id = Column(UUID(as_uuid=True), primary_key=True)
    decision_id = Column(UUID(as_uuid=True), nullable=False)
    payment_attempt_id = Column(String(255), nullable=False)
    # Provider of the attempt; NOT NULL under Revision B.
    source_system = Column(String(50), nullable=False)
    decision_version = Column(Integer, nullable=False)
    route_key = Column(String(50), nullable=False)
    policy_id = Column(String(50), nullable=False)
    policy_version = Column(String(50), nullable=False)
    command_status = Column(String(20), nullable=False)  # PENDING, EXECUTING, SUCCEEDED, FAILED, EXPIRED, NOT_NEEDED
    status_reason = Column(String(255), nullable=True)
    idempotency_key = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_cmd_decision"),
        UniqueConstraint("idempotency_key", name="uq_cmd_idempotency_key"),
        UniqueConstraint(
            "payment_attempt_id",
            "source_system",
            "decision_version",
            name="uq_cmd_attempt_version",
        ),
        UniqueConstraint("command_id", "source_system", name="uq_cmd_provider"),
        ForeignKeyConstraint(
            ["payment_attempt_id", "source_system"],
            ["payment_attempts.payment_attempt_id", "payment_attempts.source_system"],
            name="fk_intervention_commands_payment_attempt",
        ),
        ForeignKeyConstraint(
            ["decision_id", "source_system"],
            ["intervention_decisions.decision_id", "intervention_decisions.source_system"],
            name="fk_command_decision",
        ),
        Index("ix_cmd_status_expiry", "command_status", "expires_at"),
        Index("ix_cmd_attempt_lookup", "payment_attempt_id", "created_at"),
    )


class InterventionExecutionAttemptModel(Base):
    """
    Append-only factual record of an intervention dispatch attempt.
    """
    __tablename__ = "intervention_execution_attempts"

    attempt_id = Column(UUID(as_uuid=True), primary_key=True)
    command_id = Column(UUID(as_uuid=True), nullable=False)
    # Provider of the command's attempt; NOT NULL under Revision B.
    source_system = Column(String(50), nullable=False)
    attempt_number = Column(Integer, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=False)
    duration_ms = Column(Integer, nullable=False)
    execution_status = Column(String(20), nullable=False)  # SUCCESS, FAILURE, TIMEOUT
    executor_name = Column(String(100), nullable=False)
    is_simulation = Column(Boolean, nullable=False)
    provider_action_type = Column(String(50), nullable=False)
    provider_response_code = Column(String(50), nullable=True)
    provider_response_payload = Column(JSON, nullable=False)
    error_message = Column(String(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "command_id",
            "attempt_number",
            name="uq_exec_attempt_number",
        ),
        ForeignKeyConstraint(
            ["command_id", "source_system"],
            ["intervention_commands.command_id", "intervention_commands.source_system"],
            name="fk_exec_attempt_command",
        ),
        Index("ix_exec_attempt_cmd", "command_id"),
    )


class PaymentOutcomeObservationModel(Base):
    """
    Append-only factual observation of the payment attempt's canonical lifecycle outcome.
    Never inferred from execution success.
    """
    __tablename__ = "payment_outcome_observations"

    observation_id = Column(UUID(as_uuid=True), primary_key=True)
    command_id = Column(UUID(as_uuid=True), nullable=False)
    payment_attempt_id = Column(String(255), nullable=False)
    # Provider of the attempt; NOT NULL under Revision B.
    source_system = Column(String(50), nullable=False)
    observation_version = Column(Integer, nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    as_of_timestamp = Column(DateTime(timezone=True), nullable=False)
    payment_outcome = Column(String(30), nullable=False)  # CAPTURED, FAILED, UNKNOWN_IN_FLIGHT
    terminal_event_id = Column(UUID(as_uuid=True), nullable=True)
    terminal_event_type = Column(String(100), nullable=True)
    terminal_event_timestamp = Column(DateTime(timezone=True), nullable=True)
    time_to_outcome_ms = Column(Integer, nullable=True)
    observation_audit_payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "command_id",
            "observation_version",
            name="uq_outcome_observation_version",
        ),
        UniqueConstraint(
            "command_id",
            "as_of_timestamp",
            name="uq_outcome_observation_temporal",
        ),
        ForeignKeyConstraint(
            ["payment_attempt_id", "source_system"],
            ["payment_attempts.payment_attempt_id", "payment_attempts.source_system"],
            name="fk_payment_outcome_observations_payment_attempt",
        ),
        ForeignKeyConstraint(
            ["command_id", "source_system"],
            ["intervention_commands.command_id", "intervention_commands.source_system"],
            name="fk_observation_command",
        ),
        Index("ix_obs_attempt_lookup", "payment_attempt_id", "as_of_timestamp"),
    )


# ---------------------------------------------------------------------------
# Stage 8 — Counterfactual Attribution + Protected GMV
# ---------------------------------------------------------------------------

class CounterfactualAttributionModel(Base):
    """
    Append-only factual and risk-weighted counterfactual attribution record.
    Preserves integer minor-unit money and Numeric(6, 4) probability/confidence.
    """
    __tablename__ = "counterfactual_attributions"

    attribution_id = Column(UUID(as_uuid=True), primary_key=True)
    command_id = Column(UUID(as_uuid=True), nullable=False)
    payment_attempt_id = Column(String(255), nullable=False)
    # Provider of the attempt; NOT NULL under Revision B.
    source_system = Column(String(50), nullable=False)
    decision_id = Column(UUID(as_uuid=True), nullable=False)
    attribution_version = Column(Integer, nullable=False)
    attributed_at = Column(DateTime(timezone=True), nullable=False)
    as_of_timestamp = Column(DateTime(timezone=True), nullable=False)
    attribution_status = Column(String(50), nullable=False)
    methodology_name = Column(String(50), nullable=False)
    methodology_version = Column(String(50), nullable=False)
    treatment_status = Column(String(30), nullable=False)
    observed_payment_outcome = Column(String(30), nullable=False)
    payment_amount_minor_units = Column(Integer, nullable=False)
    counterfactual_failure_probability = Column(Numeric(6, 4), nullable=True)
    counterfactual_loss_exposure_minor_units = Column(Integer, nullable=False)
    counterfactual_natural_success_gmv_minor_units = Column(Integer, nullable=False)
    attributed_protected_gmv_minor_units = Column(Integer, nullable=False)
    attribution_confidence = Column(Numeric(6, 4), nullable=False)
    is_synthetic_baseline = Column(Boolean, nullable=False)
    is_simulated_execution = Column(Boolean, nullable=False)
    attribution_audit_payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "command_id",
            "attribution_version",
            name="uq_attribution_version",
        ),
        UniqueConstraint(
            "command_id",
            "as_of_timestamp",
            name="uq_attribution_temporal",
        ),
        ForeignKeyConstraint(
            ["payment_attempt_id", "source_system"],
            ["payment_attempts.payment_attempt_id", "payment_attempts.source_system"],
            name="fk_counterfactual_attributions_payment_attempt",
        ),
        ForeignKeyConstraint(
            ["command_id", "source_system"],
            ["intervention_commands.command_id", "intervention_commands.source_system"],
            name="fk_attribution_command",
        ),
        ForeignKeyConstraint(
            ["decision_id", "source_system"],
            ["intervention_decisions.decision_id", "intervention_decisions.source_system"],
            name="fk_attribution_decision",
        ),
        Index("ix_attr_attempt_lookup", "payment_attempt_id", "as_of_timestamp"),
        Index("ix_attr_status", "attribution_status", "attributed_at"),
    )



