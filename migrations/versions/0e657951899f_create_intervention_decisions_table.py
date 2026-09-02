"""create_intervention_decisions_table

Revision ID: 0e657951899f
Revises: 6b158741b993
Create Date: 2026-09-02 19:29:33.903189

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0e657951899f'
down_revision: Union[str, Sequence[str], None] = '6b158741b993'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'intervention_decisions',
        sa.Column('decision_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('payment_attempt_id', sa.String(length=255), nullable=False),
        sa.Column('decision_version', sa.Integer(), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decision_type', sa.String(length=20), nullable=False),
        sa.Column('selected_route_id', sa.String(length=50), nullable=True),
        sa.Column('failure_probability', sa.Float(), nullable=True),
        sa.Column('stage5_prediction_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('diagnosis_confidence', sa.String(length=20), nullable=True),
        sa.Column('decision_confidence', sa.Float(), nullable=False),
        sa.Column('gate_verdict', sa.String(length=50), nullable=False),
        sa.Column('policy_id', sa.String(length=50), nullable=False),
        sa.Column('policy_version', sa.String(length=50), nullable=False),
        sa.Column('input_fingerprint', sa.String(length=71), nullable=False),
        sa.Column('evaluation_audit_payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('decision_id'),
        sa.UniqueConstraint('payment_attempt_id', 'decision_version', name='uq_intervention_decision_version')
    )

    op.create_index(
        'ix_intervention_decisions_attempt_lookup',
        'intervention_decisions',
        ['payment_attempt_id', 'decision_version'],
        unique=False
    )
    op.create_index(
        'ix_intervention_decisions_fingerprint',
        'intervention_decisions',
        ['input_fingerprint'],
        unique=False
    )
    op.create_index(
        'ix_intervention_decisions_rate_limiting',
        'intervention_decisions',
        ['decision_type', 'decided_at'],
        unique=False
    )
    op.create_index(
        'ix_intervention_decisions_route_cooldown',
        'intervention_decisions',
        ['payment_attempt_id', 'selected_route_id', 'decided_at'],
        unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_intervention_decisions_route_cooldown', table_name='intervention_decisions')
    op.drop_index('ix_intervention_decisions_rate_limiting', table_name='intervention_decisions')
    op.drop_index('ix_intervention_decisions_fingerprint', table_name='intervention_decisions')
    op.drop_index('ix_intervention_decisions_attempt_lookup', table_name='intervention_decisions')
    op.drop_table('intervention_decisions')

