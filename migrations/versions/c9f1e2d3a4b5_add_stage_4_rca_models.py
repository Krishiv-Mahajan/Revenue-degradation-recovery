"""add stage 4 rca models

Revision ID: c9f1e2d3a4b5
Revises: abd7b5ce088a
Create Date: 2026-09-01 23:35:00.000000

Creates:
  - rca_evaluations (append-only, versioned RCA evaluations)
  - rca_candidate_causes (append-only, structural candidates per evaluation)
  - idx_payment_events_timestamp_event_type (composite index for time-range queries)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c9f1e2d3a4b5"
down_revision: Union[str, Sequence[str], None] = "abd7b5ce088a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # Performance index on payment_events for time-range queries (§27 of design).
    # Stage 4 queries payment_events filtered by (timestamp, event_type).
    op.create_index(
        "idx_payment_events_timestamp_event_type",
        "payment_events",
        ["timestamp", "event_type"],
    )

    # rca_evaluations — append-only, versioned RCA evaluation table.
    # NEVER subject to UPDATE or DELETE.
    op.create_table(
        "rca_evaluations",
        sa.Column("evaluation_id", sa.UUID(), nullable=False),
        sa.Column("episode_id", sa.UUID(), nullable=False),
        sa.Column("evaluation_version", sa.Integer(), nullable=False),
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("analysis_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("analysis_window_end", sa.DateTime(timezone=True), nullable=False),
        # 'sha256:' prefix + 64 hex chars = 71 chars
        sa.Column("input_fingerprint", sa.String(length=71), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_audit_payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("evaluation_id"),
        sa.UniqueConstraint(
            "episode_id",
            "evaluation_version",
            name="uq_rca_evaluation_version",
        ),
    )

    # rca_candidate_causes — append-only structural candidates per evaluation.
    # Each row is permanently owned by exactly one rca_evaluations row.
    # NEVER subject to UPDATE or DELETE.
    op.create_table(
        "rca_candidate_causes",
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("evaluation_id", sa.UUID(), nullable=False),
        sa.Column("candidate_dimension", sa.String(length=50), nullable=False),
        sa.Column("candidate_value", sa.String(length=255), nullable=False),
        sa.Column("evidence_strength", sa.String(length=20), nullable=False),
        sa.Column("excess_failure_contribution", sa.Float(), nullable=False),
        sa.Column("actual_segment_failures", sa.Integer(), nullable=False),
        sa.Column("expected_segment_failures", sa.Float(), nullable=False),
        sa.Column("excess_segment_failures", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("candidate_id"),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["rca_evaluations.evaluation_id"],
            name="fk_candidate_cause_evaluation",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("rca_candidate_causes")
    op.drop_table("rca_evaluations")
    op.drop_index(
        "idx_payment_events_timestamp_event_type",
        table_name="payment_events",
    )
