"""zscore_detection_evidence

Phase 3 Revision E. Adds the statistics Stage 3 needs for Z-score evidence: the mean, sample standard
deviation and sample count of each snapshot's historical windows (payment_health_snapshots), and the
Z-score and its status on each degradation signal. Nullable columns only; existing rows keep NULL
(they were evaluated before this evidence existed) and nothing is backfilled or rewritten.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-03 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SNAPSHOT_COLUMNS = (
    ('baseline_mean_success_rate', sa.Float()),
    ('baseline_stddev_success_rate', sa.Float()),
    ('baseline_sample_count', sa.Integer()),
)
SIGNAL_COLUMNS = (
    ('z_score', sa.Float()),
    ('z_score_status', sa.String(length=32)),
)


def upgrade() -> None:
    for name, type_ in SNAPSHOT_COLUMNS:
        op.add_column('payment_health_snapshots', sa.Column(name, type_, nullable=True))
    for name, type_ in SIGNAL_COLUMNS:
        op.add_column('degradation_signals', sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(SIGNAL_COLUMNS):
        op.drop_column('degradation_signals', name)
    for name, _ in reversed(SNAPSHOT_COLUMNS):
        op.drop_column('payment_health_snapshots', name)
