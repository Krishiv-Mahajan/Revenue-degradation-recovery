"""add stage 3 degradation models

Revision ID: abd7b5ce088a
Revises: 151b772aecb9
Create Date: 2026-09-01 19:41:30.201095

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'abd7b5ce088a'
down_revision: Union[str, Sequence[str], None] = '151b772aecb9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('degradation_signals',
        sa.Column('signal_id', sa.UUID(), nullable=False),
        sa.Column('snapshot_id', sa.UUID(), nullable=False),
        sa.Column('segment_dimension', sa.String(length=50), nullable=False),
        sa.Column('segment_value', sa.String(length=255), nullable=False),
        sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('evaluation_version', sa.Integer(), nullable=False),
        sa.Column('signal_type', sa.String(length=20), nullable=False),
        sa.Column('baseline_success_rate', sa.Float(), nullable=True),
        sa.Column('absolute_drop', sa.Float(), nullable=True),
        sa.Column('relative_drop', sa.Float(), nullable=True),
        sa.Column('evaluation_timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('signal_id'),
        sa.UniqueConstraint('segment_dimension', 'segment_value', 'window_start', 'evaluation_version', name='uq_signal_evaluation_version')
    )
    
    op.create_table('degradation_episodes',
        sa.Column('episode_id', sa.UUID(), nullable=False),
        sa.Column('segment_dimension', sa.String(length=50), nullable=False),
        sa.Column('segment_value', sa.String(length=255), nullable=False),
        sa.Column('started_at_window', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at_window', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('peak_absolute_drop', sa.Float(), nullable=False),
        sa.Column('affected_window_count', sa.Integer(), nullable=False),
        sa.Column('severity', sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint('episode_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('degradation_episodes')
    op.drop_table('degradation_signals')
