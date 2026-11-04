"""payment_method_switch_offers

Phase 3 Revision G. Customer-approved payment method switch offers (PROMPT_PAYMENT_METHOD_SWITCH): one
offer per intervention command, bound by composite foreign keys to the original payment attempt, its
decision and command, and the real execution attempt that created the PayPal sandbox checkout, plus an
append-only offer event trail. New tables only; no existing row is read or changed.

Downgrade refuses, changing nothing, while any offer or offer event exists; offers are never deleted
automatically.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-11-04 13:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OFFER_STATUSES = "('OFFERED', 'ACCEPTED', 'DECLINED', 'EXPIRED', 'ABANDONED', 'COMPLETED', 'PAYMENT_FAILED')"

REFUSE_DOWNGRADE_WITH_OFFERS = """
DO $$
DECLARE remaining bigint;
BEGIN
  SELECT (SELECT count(*) FROM payment_method_switch_offers)
       + (SELECT count(*) FROM payment_method_switch_offer_events)
    INTO remaining;
  IF remaining > 0 THEN
    RAISE EXCEPTION 'payment method switch offer downgrade refused: % row(s) exist', remaining
      USING HINT = 'Nothing was changed. Archive the offers and their events explicitly, then re-run.';
  END IF;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        'payment_method_switch_offers',
        sa.Column('offer_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('command_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('decision_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('execution_attempt_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('payment_attempt_id', sa.String(length=255), nullable=False),
        sa.Column('source_system', sa.String(length=50), nullable=False),
        sa.Column('episode_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('diagnosis_dimension', sa.String(length=50), nullable=True),
        sa.Column('diagnosis_value', sa.String(length=255), nullable=True),
        sa.Column('diagnosis_strength', sa.String(length=20), nullable=True),
        sa.Column('alternative_provider', sa.String(length=20), nullable=False),
        sa.Column('environment', sa.String(length=20), nullable=False),
        sa.Column('checkout_order_id', sa.String(length=128), nullable=False),
        sa.Column('approve_url', sa.String(length=1024), nullable=False),
        sa.Column('amount_minor_units', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('offered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completion_deadline', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completion_event_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('presentation', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('offer_id'),
        sa.UniqueConstraint('command_id', name='uq_switch_offer_command'),
        sa.UniqueConstraint('checkout_order_id', name='uq_switch_offer_checkout_order'),
        sa.ForeignKeyConstraint(
            ['payment_attempt_id', 'source_system'],
            ['payment_attempts.payment_attempt_id', 'payment_attempts.source_system'],
            name='fk_switch_offer_payment_attempt',
        ),
        sa.ForeignKeyConstraint(
            ['command_id', 'payment_attempt_id', 'source_system'],
            [
                'intervention_commands.command_id',
                'intervention_commands.payment_attempt_id',
                'intervention_commands.source_system',
            ],
            name='fk_switch_offer_command_attempt',
        ),
        sa.ForeignKeyConstraint(
            ['decision_id', 'payment_attempt_id', 'source_system'],
            [
                'intervention_decisions.decision_id',
                'intervention_decisions.payment_attempt_id',
                'intervention_decisions.source_system',
            ],
            name='fk_switch_offer_decision_attempt',
        ),
        sa.ForeignKeyConstraint(
            ['execution_attempt_id'], ['intervention_execution_attempts.attempt_id'],
            name='fk_switch_offer_execution_attempt',
        ),
        sa.ForeignKeyConstraint(
            ['completion_event_id'], ['payment_events.event_id'], name='fk_switch_offer_completion_event',
        ),
        sa.CheckConstraint(f"status IN {OFFER_STATUSES}", name='ck_switch_offer_status'),
        sa.CheckConstraint("alternative_provider = 'paypal'", name='ck_switch_offer_alternative_provider'),
        sa.CheckConstraint("source_system <> alternative_provider", name='ck_switch_offer_changes_provider'),
        sa.CheckConstraint("environment = 'sandbox'", name='ck_switch_offer_sandbox_only'),
        sa.CheckConstraint("amount_minor_units > 0", name='ck_switch_offer_amount_positive'),
        sa.CheckConstraint("expires_at > offered_at", name='ck_switch_offer_expiry_after_offer'),
        sa.CheckConstraint(
            "(status IN ('ACCEPTED', 'COMPLETED', 'PAYMENT_FAILED', 'ABANDONED')) = "
            "(accepted_at IS NOT NULL AND completion_deadline IS NOT NULL)",
            name='ck_switch_offer_acceptance',
        ),
        sa.CheckConstraint(
            "(status = 'COMPLETED') = (completion_event_id IS NOT NULL)", name='ck_switch_offer_completion_event',
        ),
        sa.CheckConstraint(
            "(status IN ('DECLINED', 'EXPIRED', 'ABANDONED', 'COMPLETED', 'PAYMENT_FAILED')) = (resolved_at IS NOT NULL)",
            name='ck_switch_offer_resolution',
        ),
    )
    op.create_index('ix_switch_offer_status_expiry', 'payment_method_switch_offers', ['status', 'expires_at'])
    op.create_index(
        'ix_switch_offer_attempt', 'payment_method_switch_offers', ['payment_attempt_id', 'source_system'],
    )

    op.create_table(
        'payment_method_switch_offer_events',
        sa.Column('offer_event_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('offer_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=30), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('from_status', sa.String(length=20), nullable=True),
        sa.Column('to_status', sa.String(length=20), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payment_event_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('reason', sa.String(length=512), nullable=True),
        sa.Column('details', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('offer_event_id'),
        sa.UniqueConstraint('offer_id', 'sequence', name='uq_switch_offer_event_sequence'),
        sa.ForeignKeyConstraint(
            ['offer_id'], ['payment_method_switch_offers.offer_id'], name='fk_switch_offer_event_offer',
        ),
        sa.ForeignKeyConstraint(
            ['payment_event_id'], ['payment_events.event_id'], name='fk_switch_offer_event_payment_event',
        ),
        sa.CheckConstraint(
            "event_type IN ('OFFER_CREATED', 'CUSTOMER_ACCEPTED', 'CUSTOMER_DECLINED', 'OFFER_EXPIRED', "
            "'OFFER_ABANDONED', 'PAYMENT_COMPLETED', 'PAYMENT_FAILED', 'PAYMENT_OUTCOME_HELD', 'EVENT_IGNORED')",
            name='ck_switch_offer_event_type',
        ),
        sa.CheckConstraint(
            "source IN ('EXECUTOR', 'PAYPAL_WEBHOOK', 'MERCHANT_CHANNEL', 'EXPIRY_SWEEP')",
            name='ck_switch_offer_event_source',
        ),
        sa.CheckConstraint(f"to_status IN {OFFER_STATUSES}", name='ck_switch_offer_event_to_status'),
    )
    op.create_index(
        'uq_switch_offer_event_payment_event', 'payment_method_switch_offer_events',
        ['offer_id', 'payment_event_id', 'event_type'], unique=True,
        postgresql_where=sa.text('payment_event_id IS NOT NULL'),
    )


def downgrade() -> None:
    op.execute(sa.text(REFUSE_DOWNGRADE_WITH_OFFERS))
    op.drop_index('uq_switch_offer_event_payment_event', table_name='payment_method_switch_offer_events')
    op.drop_table('payment_method_switch_offer_events')
    op.drop_index('ix_switch_offer_attempt', table_name='payment_method_switch_offers')
    op.drop_index('ix_switch_offer_status_expiry', table_name='payment_method_switch_offers')
    op.drop_table('payment_method_switch_offers')
