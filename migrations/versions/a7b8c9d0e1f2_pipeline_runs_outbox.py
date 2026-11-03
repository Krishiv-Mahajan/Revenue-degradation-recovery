"""pipeline_runs_outbox

Phase 3 Revision D. Creates pipeline_runs, the transactional outbox of Stages 2-8 runs: ingestion
writes one row per event that needs the pipeline in the same transaction as the event, and the
reconciler replays rows a crash left behind. Purely additive; no existing table changes and no
backfill (whether earlier events were processed is not known, so nothing is assumed).

Revision ID: a7b8c9d0e1f2
Revises: f4a5b6c7d8e9
Create Date: 2026-11-03 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f4a5b6c7d8e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pipeline_runs',
        sa.Column('event_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('source_system', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_failed_stage', sa.String(length=64), nullable=True),
        sa.Column('last_error_type', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('event_id'),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')", name='ck_pipeline_run_status'
        ),
        sa.CheckConstraint('attempts >= 0', name='ck_pipeline_run_attempts'),
        sa.ForeignKeyConstraint(
            ['event_id', 'source_system'],
            ['payment_events.event_id', 'payment_events.source_system'],
            name='fk_pipeline_run_event',
        ),
    )
    op.create_index('ix_pipeline_runs_status_updated', 'pipeline_runs', ['status', 'updated_at'])


def downgrade() -> None:
    op.drop_index('ix_pipeline_runs_status_updated', table_name='pipeline_runs')
    op.drop_table('pipeline_runs')
