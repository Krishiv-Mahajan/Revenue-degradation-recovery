"""stage5_8_provider_identity_contract

Phase 3 Revision B (contract). Enforces NOT NULL on source_system across the six Stage 5-8 tables,
transitions version unique constraints to include source_system, adds composite foreign keys
to the canonical payment-attempt registry, transitions attribution foreign keys safely, and
enforces provider consistency between related Stage 5-8 records.

Revision ID: e2d3c4b5a6f7
Revises: 9d2e4f6a8b1c
Create Date: 2026-10-21 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e2d3c4b5a6f7'
down_revision: Union[str, Sequence[str], None] = '9d2e4f6a8b1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.provider_identity_contract")

STAGE5_8_TABLES = (
    "failure_predictions",
    "intervention_decisions",
    "intervention_commands",
    "intervention_execution_attempts",
    "payment_outcome_observations",
    "counterfactual_attributions",
)

KEYED_TABLES = (
    ("failure_predictions", "prediction_id", "fk_failure_predictions_payment_attempt"),
    ("intervention_decisions", "decision_id", "fk_intervention_decisions_payment_attempt"),
    ("intervention_commands", "command_id", "fk_intervention_commands_payment_attempt"),
    ("payment_outcome_observations", "observation_id", "fk_payment_outcome_observations_payment_attempt"),
    ("counterfactual_attributions", "attribution_id", "fk_counterfactual_attributions_payment_attempt"),
)

CREATE_REQUIRE_NONE = """
CREATE FUNCTION pg_temp.p3b_require_none(check_code text, description text, offending_sql text)
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE
  offending bigint;
  sample text;
BEGIN
  EXECUTE format(
    'SELECT count(*), string_agg(ref, '', '' ORDER BY ref) FILTER (WHERE rn <= 10) '
    'FROM (SELECT ref, row_number() OVER (ORDER BY ref) AS rn FROM (%s) AS q(ref)) AS s',
    offending_sql)
  INTO offending, sample;
  IF offending > 0 THEN
    RAISE EXCEPTION 'provider_identity contract preflight % failed: %. % offending row(s); first 10: %',
      check_code, description, offending, sample
      USING HINT = 'Nothing was changed. Resolve the listed records explicitly, then re-run.';
  END IF;
END
$fn$
"""
DROP_REQUIRE_NONE = "DROP FUNCTION pg_temp.p3b_require_none(text, text, text)"

PREFLIGHT_CHECKS = [
    # 1. NOT NULL validation
    ("PF-B01", "every failure_predictions row must have a source_system",
     "SELECT prediction_id::text FROM failure_predictions WHERE source_system IS NULL"),
    ("PF-B02", "every intervention_decisions row must have a source_system",
     "SELECT decision_id::text FROM intervention_decisions WHERE source_system IS NULL"),
    ("PF-B03", "every intervention_commands row must have a source_system",
     "SELECT command_id::text FROM intervention_commands WHERE source_system IS NULL"),
    ("PF-B04", "every intervention_execution_attempts row must have a source_system",
     "SELECT attempt_id::text FROM intervention_execution_attempts WHERE source_system IS NULL"),
    ("PF-B05", "every payment_outcome_observations row must have a source_system",
     "SELECT observation_id::text FROM payment_outcome_observations WHERE source_system IS NULL"),
    ("PF-B06", "every counterfactual_attributions row must have a source_system",
     "SELECT attribution_id::text FROM counterfactual_attributions WHERE source_system IS NULL"),

    # 2. Registry referential integrity
    ("PF-B07", "every prediction (attempt_id, source_system) must exist in payment_attempts",
     """SELECT fp.prediction_id::text FROM failure_predictions fp
        WHERE NOT EXISTS (
            SELECT 1 FROM payment_attempts pa
            WHERE pa.payment_attempt_id = fp.payment_attempt_id AND pa.source_system = fp.source_system
        )"""),
    ("PF-B08", "every decision (attempt_id, source_system) must exist in payment_attempts",
     """SELECT id.decision_id::text FROM intervention_decisions id
        WHERE NOT EXISTS (
            SELECT 1 FROM payment_attempts pa
            WHERE pa.payment_attempt_id = id.payment_attempt_id AND pa.source_system = id.source_system
        )"""),
    ("PF-B09", "every command (attempt_id, source_system) must exist in payment_attempts",
     """SELECT ic.command_id::text FROM intervention_commands ic
        WHERE NOT EXISTS (
            SELECT 1 FROM payment_attempts pa
            WHERE pa.payment_attempt_id = ic.payment_attempt_id AND pa.source_system = ic.source_system
        )"""),
    ("PF-B10", "every observation (attempt_id, source_system) must exist in payment_attempts",
     """SELECT po.observation_id::text FROM payment_outcome_observations po
        WHERE NOT EXISTS (
            SELECT 1 FROM payment_attempts pa
            WHERE pa.payment_attempt_id = po.payment_attempt_id AND pa.source_system = po.source_system
        )"""),
    ("PF-B11", "every attribution (attempt_id, source_system) must exist in payment_attempts",
     """SELECT ca.attribution_id::text FROM counterfactual_attributions ca
        WHERE NOT EXISTS (
            SELECT 1 FROM payment_attempts pa
            WHERE pa.payment_attempt_id = ca.payment_attempt_id AND pa.source_system = ca.source_system
        )"""),

    # 3. Inter-stage provider consistency
    ("PF-B12", "every command must match its decision provider",
     """SELECT ic.command_id::text FROM intervention_commands ic
        JOIN intervention_decisions id ON id.decision_id = ic.decision_id
        WHERE ic.source_system <> id.source_system"""),
    ("PF-B13", "every execution attempt must match its command provider",
     """SELECT ea.attempt_id::text FROM intervention_execution_attempts ea
        JOIN intervention_commands ic ON ic.command_id = ea.command_id
        WHERE ea.source_system <> ic.source_system"""),
    ("PF-B14", "every observation must match its command provider",
     """SELECT po.observation_id::text FROM payment_outcome_observations po
        JOIN intervention_commands ic ON ic.command_id = po.command_id
        WHERE po.source_system <> ic.source_system"""),
    ("PF-B15", "every attribution must match its command provider",
     """SELECT ca.attribution_id::text FROM counterfactual_attributions ca
        JOIN intervention_commands ic ON ic.command_id = ca.command_id
        WHERE ca.source_system <> ic.source_system"""),
    ("PF-B16", "every attribution must match its decision provider",
     """SELECT ca.attribution_id::text FROM counterfactual_attributions ca
        JOIN intervention_decisions id ON id.decision_id = ca.decision_id
        WHERE ca.source_system <> id.source_system"""),

    # 4. Version uniqueness under composite key
    ("PF-B17", "prediction versions must be unique per (payment_attempt_id, source_system, prediction_version)",
     """SELECT payment_attempt_id || '/' || source_system || '/v' || prediction_version
        FROM failure_predictions
        GROUP BY payment_attempt_id, source_system, prediction_version
        HAVING count(*) > 1"""),
    ("PF-B18", "decision versions must be unique per (payment_attempt_id, source_system, decision_version)",
     """SELECT payment_attempt_id || '/' || source_system || '/v' || decision_version
        FROM intervention_decisions
        GROUP BY payment_attempt_id, source_system, decision_version
        HAVING count(*) > 1"""),
    ("PF-B19", "command versions must be unique per (payment_attempt_id, source_system, decision_version)",
     """SELECT payment_attempt_id || '/' || source_system || '/v' || decision_version
        FROM intervention_commands
        GROUP BY payment_attempt_id, source_system, decision_version
        HAVING count(*) > 1"""),
]


def _require_none(code: str, description: str, query: str) -> str:
    quoted_description = description.replace("'", "''")
    return f"SELECT pg_temp.p3b_require_none('{code}', '{quoted_description}', $q${query}$q$)"


def upgrade() -> None:
    """Validate Stage 5-8 provider contracts, enforce NOT NULL and create composite constraints."""
    # 1. Preflight checks
    op.execute(sa.text(CREATE_REQUIRE_NONE))
    for code, description, query in PREFLIGHT_CHECKS:
        op.execute(sa.text(_require_none(code, description, query)))
    op.execute(sa.text(DROP_REQUIRE_NONE))

    # 2. Drop old single-version unique constraints
    op.drop_constraint('uq_failure_prediction_version', 'failure_predictions', type_='unique')
    op.drop_constraint('uq_intervention_decision_version', 'intervention_decisions', type_='unique')
    op.drop_constraint('uq_cmd_attempt_version', 'intervention_commands', type_='unique')

    # 3. Enforce NOT NULL on source_system across all six Stage 5-8 tables
    for table in STAGE5_8_TABLES:
        op.alter_column(table, 'source_system', existing_type=sa.String(length=50), nullable=False)

    # 4. Create composite version unique constraints
    op.create_unique_constraint(
        'uq_failure_prediction_version',
        'failure_predictions',
        ['payment_attempt_id', 'source_system', 'prediction_version'],
    )
    op.create_unique_constraint(
        'uq_intervention_decision_version',
        'intervention_decisions',
        ['payment_attempt_id', 'source_system', 'decision_version'],
    )
    op.create_unique_constraint(
        'uq_cmd_attempt_version',
        'intervention_commands',
        ['payment_attempt_id', 'source_system', 'decision_version'],
    )

    # 5. Create composite unique constraints for FK targets
    op.create_unique_constraint(
        'uq_failure_prediction_provider',
        'failure_predictions',
        ['prediction_id', 'source_system'],
    )
    op.create_unique_constraint(
        'uq_intervention_decision_provider',
        'intervention_decisions',
        ['decision_id', 'source_system'],
    )
    op.create_unique_constraint(
        'uq_cmd_provider',
        'intervention_commands',
        ['command_id', 'source_system'],
    )

    # 6. Create composite foreign keys to payment_attempts
    for table, _, fk_name in KEYED_TABLES:
        op.create_foreign_key(
            fk_name,
            table,
            'payment_attempts',
            ['payment_attempt_id', 'source_system'],
            ['payment_attempt_id', 'source_system'],
        )

    # 7. Safely transition attribution foreign keys from single-column to composite
    op.drop_constraint('fk_attribution_command', 'counterfactual_attributions', type_='foreignkey')
    op.drop_constraint('fk_attribution_decision', 'counterfactual_attributions', type_='foreignkey')
    op.create_foreign_key(
        'fk_attribution_command',
        'counterfactual_attributions',
        'intervention_commands',
        ['command_id', 'source_system'],
        ['command_id', 'source_system'],
    )
    op.create_foreign_key(
        'fk_attribution_decision',
        'counterfactual_attributions',
        'intervention_decisions',
        ['decision_id', 'source_system'],
        ['decision_id', 'source_system'],
    )

    # 8. Create relational consistency foreign keys
    op.create_foreign_key(
        'fk_command_decision',
        'intervention_commands',
        'intervention_decisions',
        ['decision_id', 'source_system'],
        ['decision_id', 'source_system'],
    )
    op.create_foreign_key(
        'fk_exec_attempt_command',
        'intervention_execution_attempts',
        'intervention_commands',
        ['command_id', 'source_system'],
        ['command_id', 'source_system'],
    )
    op.create_foreign_key(
        'fk_observation_command',
        'payment_outcome_observations',
        'intervention_commands',
        ['command_id', 'source_system'],
        ['command_id', 'source_system'],
    )


def downgrade() -> None:
    """Restore the 9d2e4f6a8b1c (Revision A) schema exactly."""
    # 1. Drop relational consistency foreign keys
    op.drop_constraint('fk_observation_command', 'payment_outcome_observations', type_='foreignkey')
    op.drop_constraint('fk_exec_attempt_command', 'intervention_execution_attempts', type_='foreignkey')
    op.drop_constraint('fk_command_decision', 'intervention_commands', type_='foreignkey')

    # 2. Transition attribution foreign keys back to single-column
    op.drop_constraint('fk_attribution_decision', 'counterfactual_attributions', type_='foreignkey')
    op.drop_constraint('fk_attribution_command', 'counterfactual_attributions', type_='foreignkey')
    op.create_foreign_key(
        'fk_attribution_command',
        'counterfactual_attributions',
        'intervention_commands',
        ['command_id'],
        ['command_id'],
    )
    op.create_foreign_key(
        'fk_attribution_decision',
        'counterfactual_attributions',
        'intervention_decisions',
        ['decision_id'],
        ['decision_id'],
    )

    # 3. Drop composite foreign keys to payment_attempts
    for table, _, fk_name in reversed(KEYED_TABLES):
        op.drop_constraint(fk_name, table, type_='foreignkey')

    # 4. Drop composite unique constraints used as targets
    op.drop_constraint('uq_cmd_provider', 'intervention_commands', type_='unique')
    op.drop_constraint('uq_intervention_decision_provider', 'intervention_decisions', type_='unique')
    op.drop_constraint('uq_failure_prediction_provider', 'failure_predictions', type_='unique')

    # 5. Restore old version unique constraints
    op.drop_constraint('uq_cmd_attempt_version', 'intervention_commands', type_='unique')
    op.drop_constraint('uq_intervention_decision_version', 'intervention_decisions', type_='unique')
    op.drop_constraint('uq_failure_prediction_version', 'failure_predictions', type_='unique')

    op.create_unique_constraint(
        'uq_failure_prediction_version',
        'failure_predictions',
        ['payment_attempt_id', 'prediction_version'],
    )
    op.create_unique_constraint(
        'uq_intervention_decision_version',
        'intervention_decisions',
        ['payment_attempt_id', 'decision_version'],
    )
    op.create_unique_constraint(
        'uq_cmd_attempt_version',
        'intervention_commands',
        ['payment_attempt_id', 'decision_version'],
    )

    # 6. Alter source_system back to nullable=True
    for table in reversed(STAGE5_8_TABLES):
        op.alter_column(table, 'source_system', existing_type=sa.String(length=50), nullable=True)
