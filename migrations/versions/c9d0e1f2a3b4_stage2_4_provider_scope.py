"""stage2_4_provider_scope

Phase 3 Revision F. Partitions Stages 2-4 by analytics scope (src/core/domain/analytics_scopes.py) so
PayPal gets its own health snapshots, degradation signals, episodes and episode history.

Every existing row belongs to the original Razorpay/synthetic scope, so the new NOT NULL provider_scope
column is added with server default 'razorpay_pipeline'; no row is rewritten otherwise. The snapshot and
signal identity unique constraints gain provider_scope (unchanged within a scope).

Downgrade refuses, changing nothing, while any non-default scope row exists: those rows could collide
with Razorpay rows once the scope column is gone, and are never deleted automatically.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-11-03 23:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("payment_health_snapshots", "degradation_signals", "degradation_episodes", "episode_state_history")
DEFAULT_SCOPE = "razorpay_pipeline"
SCOPE_CHECK = "provider_scope IN ('razorpay_pipeline', 'paypal')"

SNAPSHOT_IDENTITY = ['window_start', 'window_end', 'segment_dimension', 'segment_value']
SIGNAL_IDENTITY = ['segment_dimension', 'segment_value', 'window_start', 'evaluation_version']

REFUSE_DOWNGRADE_WITH_SCOPED_ROWS = """
DO $$
DECLARE scoped bigint;
BEGIN
  SELECT (SELECT count(*) FROM payment_health_snapshots WHERE provider_scope <> 'razorpay_pipeline')
       + (SELECT count(*) FROM degradation_signals WHERE provider_scope <> 'razorpay_pipeline')
       + (SELECT count(*) FROM degradation_episodes WHERE provider_scope <> 'razorpay_pipeline')
       + (SELECT count(*) FROM episode_state_history WHERE provider_scope <> 'razorpay_pipeline')
    INTO scoped;
  IF scoped > 0 THEN
    RAISE EXCEPTION 'provider_scope downgrade refused: % row(s) belong to a non-default analytics scope', scoped
      USING HINT = 'Nothing was changed. Resolve or archive those rows explicitly, then re-run.';
  END IF;
END
$$
"""


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table, sa.Column('provider_scope', sa.String(length=32), nullable=False, server_default=DEFAULT_SCOPE)
        )
        op.create_check_constraint(f'ck_{table}_provider_scope', table, SCOPE_CHECK)

    op.drop_constraint('uq_snapshot_identity', 'payment_health_snapshots', type_='unique')
    op.create_unique_constraint(
        'uq_snapshot_identity', 'payment_health_snapshots', SNAPSHOT_IDENTITY + ['provider_scope']
    )
    op.drop_constraint('uq_signal_evaluation_version', 'degradation_signals', type_='unique')
    op.create_unique_constraint(
        'uq_signal_evaluation_version', 'degradation_signals', SIGNAL_IDENTITY + ['provider_scope']
    )


def downgrade() -> None:
    op.execute(sa.text(REFUSE_DOWNGRADE_WITH_SCOPED_ROWS))

    op.drop_constraint('uq_signal_evaluation_version', 'degradation_signals', type_='unique')
    op.create_unique_constraint('uq_signal_evaluation_version', 'degradation_signals', SIGNAL_IDENTITY)
    op.drop_constraint('uq_snapshot_identity', 'payment_health_snapshots', type_='unique')
    op.create_unique_constraint('uq_snapshot_identity', 'payment_health_snapshots', SNAPSHOT_IDENTITY)

    for table in reversed(TABLES):
        op.drop_constraint(f'ck_{table}_provider_scope', table, type_='check')
        op.drop_column(table, 'provider_scope')
