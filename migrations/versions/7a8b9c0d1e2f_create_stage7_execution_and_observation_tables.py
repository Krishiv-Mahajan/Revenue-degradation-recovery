"""create_stage7_execution_and_observation_tables

Revision ID: 7a8b9c0d1e2f
Revises: 0e657951899f
Create Date: 2026-09-02 21:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a8b9c0d1e2f'
down_revision: Union[str, Sequence[str], None] = '0e657951899f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. intervention_commands
    op.create_table(
        'intervention_commands',
        sa.Column('command_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('decision_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('payment_attempt_id', sa.String(length=255), nullable=False),
        sa.Column('decision_version', sa.Integer(), nullable=False),
        sa.Column('route_key', sa.String(length=50), nullable=False),
        sa.Column('policy_id', sa.String(length=50), nullable=False),
        sa.Column('policy_version', sa.String(length=50), nullable=False),
        sa.Column('command_status', sa.String(length=20), nullable=False),
        sa.Column('status_reason', sa.String(length=255), nullable=True),
        sa.Column('idempotency_key', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('command_id'),
        sa.UniqueConstraint('decision_id', name='uq_cmd_decision'),
        sa.UniqueConstraint('idempotency_key', name='uq_cmd_idempotency_key'),
        sa.UniqueConstraint('payment_attempt_id', 'decision_version', name='uq_cmd_attempt_version')
    )

    op.create_index(
        'ix_cmd_status_expiry',
        'intervention_commands',
        ['command_status', 'expires_at'],
        unique=False
    )
    op.create_index(
        'ix_cmd_attempt_lookup',
        'intervention_commands',
        ['payment_attempt_id', 'created_at'],
        unique=False
    )

    # 2. intervention_execution_attempts
    op.create_table(
        'intervention_execution_attempts',
        sa.Column('attempt_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('command_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('attempt_number', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('execution_status', sa.String(length=20), nullable=False),
        sa.Column('executor_name', sa.String(length=100), nullable=False),
        sa.Column('is_simulation', sa.Boolean(), nullable=False),
        sa.Column('provider_action_type', sa.String(length=50), nullable=False),
        sa.Column('provider_response_code', sa.String(length=50), nullable=True),
        sa.Column('provider_response_payload', sa.JSON(), nullable=False),
        sa.Column('error_message', sa.String(length=1024), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('attempt_id'),
        sa.UniqueConstraint('command_id', 'attempt_number', name='uq_exec_attempt_number')
    )

    op.create_index(
        'ix_exec_attempt_cmd',
        'intervention_execution_attempts',
        ['command_id'],
        unique=False
    )

    # 3. payment_outcome_observations
    op.create_table(
        'payment_outcome_observations',
        sa.Column('observation_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('command_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('payment_attempt_id', sa.String(length=255), nullable=False),
        sa.Column('observation_version', sa.Integer(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('as_of_timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payment_outcome', sa.String(length=30), nullable=False),
        sa.Column('terminal_event_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('terminal_event_type', sa.String(length=100), nullable=True),
        sa.Column('terminal_event_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('time_to_outcome_ms', sa.Integer(), nullable=True),
        sa.Column('observation_audit_payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('observation_id'),
        sa.UniqueConstraint('command_id', 'observation_version', name='uq_outcome_observation_version'),
        sa.UniqueConstraint('command_id', 'as_of_timestamp', name='uq_outcome_observation_temporal')
    )

    op.create_index(
        'ix_obs_attempt_lookup',
        'payment_outcome_observations',
        ['payment_attempt_id', 'as_of_timestamp'],
        unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_obs_attempt_lookup', table_name='payment_outcome_observations')
    op.drop_table('payment_outcome_observations')

    op.drop_index('ix_exec_attempt_cmd', table_name='intervention_execution_attempts')
    op.drop_table('intervention_execution_attempts')

    op.drop_index('ix_cmd_attempt_lookup', table_name='intervention_commands')
    op.drop_index('ix_cmd_status_expiry', table_name='intervention_commands')
    op.drop_table('intervention_commands')
