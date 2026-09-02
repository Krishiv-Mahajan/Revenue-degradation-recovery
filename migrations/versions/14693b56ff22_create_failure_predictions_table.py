"""create_failure_predictions_table

Revision ID: 14693b56ff22
Revises: c9f1e2d3a4b5
Create Date: 2026-09-02 11:49:56.807249

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '14693b56ff22'
down_revision: Union[str, Sequence[str], None] = 'c9f1e2d3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'failure_predictions',
        sa.Column('prediction_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('payment_attempt_id', sa.String(length=255), nullable=False),
        sa.Column('prediction_version', sa.Integer(), nullable=False),
        sa.Column('predicted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('prediction_horizon', sa.String(length=50), nullable=False),
        sa.Column('failure_probability', sa.Float(), nullable=True),
        sa.Column('risk_band', sa.String(length=20), nullable=True),
        sa.Column('prediction_status', sa.String(length=20), nullable=False),
        sa.Column('model_name', sa.String(length=100), nullable=False),
        sa.Column('model_version', sa.String(length=50), nullable=False),
        sa.Column('feature_schema_version', sa.String(length=50), nullable=False),
        sa.Column('feature_snapshot', sa.JSON(), nullable=False),
        sa.Column('input_fingerprint', sa.String(length=71), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('prediction_id'),
        sa.UniqueConstraint('payment_attempt_id', 'prediction_version', name='uq_failure_prediction_version')
    )
    
    op.create_index('ix_payment_events_eligibility', 'payment_events', ['payment_id', 'ingested_at'], unique=False)
    op.create_index('ix_payment_events_hist_pm', 'payment_events', ['payment_method', 'ingested_at', 'event_type'], unique=False)
    op.create_index('ix_payment_events_hist_bank', 'payment_events', ['bank', 'ingested_at', 'event_type'], unique=False)
    op.create_index('ix_payment_events_hist_wallet', 'payment_events', ['wallet', 'ingested_at', 'event_type'], unique=False)
    op.create_index('ix_payment_events_hist_curr', 'payment_events', ['currency', 'ingested_at', 'event_type'], unique=False)
    
    op.create_index('ix_degradation_signals_stage3_as_of', 'degradation_signals', ['segment_dimension', 'segment_value', 'evaluation_timestamp'], unique=False)
    
    op.create_index('ix_rca_evaluations_as_of', 'rca_evaluations', ['episode_id', 'generated_at', 'evaluation_version'], unique=False)

def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_rca_evaluations_as_of', table_name='rca_evaluations')
    op.drop_index('ix_degradation_signals_stage3_as_of', table_name='degradation_signals')
    
    op.drop_index('ix_payment_events_hist_curr', table_name='payment_events')
    op.drop_index('ix_payment_events_hist_wallet', table_name='payment_events')
    op.drop_index('ix_payment_events_hist_bank', table_name='payment_events')
    op.drop_index('ix_payment_events_hist_pm', table_name='payment_events')
    op.drop_index('ix_payment_events_eligibility', table_name='payment_events')
    
    op.drop_table('failure_predictions')
