"""create_stage8_counterfactual_attributions_table

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-09-02 21:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8b9c0d1e2f3a'
down_revision: Union[str, Sequence[str], None] = '7a8b9c0d1e2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema to include Stage 8 counterfactual attributions table."""
    op.create_table(
        'counterfactual_attributions',
        sa.Column('attribution_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('command_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('payment_attempt_id', sa.String(length=255), nullable=False),
        sa.Column('decision_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('attribution_version', sa.Integer(), nullable=False),
        sa.Column('attributed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('as_of_timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attribution_status', sa.String(length=50), nullable=False),
        sa.Column('methodology_name', sa.String(length=50), nullable=False),
        sa.Column('methodology_version', sa.String(length=50), nullable=False),
        sa.Column('treatment_status', sa.String(length=30), nullable=False),
        sa.Column('observed_payment_outcome', sa.String(length=30), nullable=False),
        sa.Column('payment_amount_minor_units', sa.Integer(), nullable=False),
        sa.Column('counterfactual_failure_probability', sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column('counterfactual_loss_exposure_minor_units', sa.Integer(), nullable=False),
        sa.Column('counterfactual_natural_success_gmv_minor_units', sa.Integer(), nullable=False),
        sa.Column('attributed_protected_gmv_minor_units', sa.Integer(), nullable=False),
        sa.Column('attribution_confidence', sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column('is_synthetic_baseline', sa.Boolean(), nullable=False),
        sa.Column('is_simulated_execution', sa.Boolean(), nullable=False),
        sa.Column('attribution_audit_payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('attribution_id'),
        sa.ForeignKeyConstraint(['command_id'], ['intervention_commands.command_id'], name='fk_attribution_command'),
        sa.ForeignKeyConstraint(['decision_id'], ['intervention_decisions.decision_id'], name='fk_attribution_decision'),
        sa.UniqueConstraint('command_id', 'attribution_version', name='uq_attribution_version'),
        sa.UniqueConstraint('command_id', 'as_of_timestamp', name='uq_attribution_temporal')
    )

    op.create_index(
        'ix_attr_attempt_lookup',
        'counterfactual_attributions',
        ['payment_attempt_id', 'as_of_timestamp'],
        unique=False
    )
    op.create_index(
        'ix_attr_status',
        'counterfactual_attributions',
        ['attribution_status', 'attributed_at'],
        unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_attr_status', table_name='counterfactual_attributions', if_exists=True)
    op.drop_index('ix_attr_attempt_lookup', table_name='counterfactual_attributions', if_exists=True)
    op.drop_table('counterfactual_attributions')
