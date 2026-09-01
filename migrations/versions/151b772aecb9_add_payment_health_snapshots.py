"""add payment health snapshots

Revision ID: 151b772aecb9
Revises: 5a6ff487fe3c
Create Date: 2026-09-01 19:15:07.343056

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '151b772aecb9'
down_revision: Union[str, Sequence[str], None] = '5a6ff487fe3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('payment_health_snapshots',
        sa.Column('snapshot_id', sa.UUID(), nullable=False),
        sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('segment_dimension', sa.String(length=50), nullable=False),
        sa.Column('segment_value', sa.String(length=255), nullable=False),
        sa.Column('transaction_count', sa.Integer(), nullable=False),
        sa.Column('successful_transaction_count', sa.Integer(), nullable=False),
        sa.Column('failed_transaction_count', sa.Integer(), nullable=False),
        sa.Column('success_rate', sa.Float(), nullable=True),
        sa.Column('failure_rate', sa.Float(), nullable=True),
        sa.Column('total_gmv_minor_units', sa.Integer(), nullable=False),
        sa.Column('successful_gmv_minor_units', sa.Integer(), nullable=False),
        sa.Column('failed_gmv_minor_units', sa.Integer(), nullable=False),
        sa.Column('baseline_success_rate', sa.Float(), nullable=True),
        sa.Column('insufficient_volume', sa.Boolean(), nullable=False),
        sa.Column('calculated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('snapshot_id'),
        sa.UniqueConstraint('window_start', 'window_end', 'segment_dimension', 'segment_value', name='uq_snapshot_identity')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('payment_health_snapshots')
