"""stage5_8_same_attempt_links

Phase 3 Revision C. Adds the missing decision -> prediction foreign key and makes every link between
Stage 5-8 records attempt-exact: a decision's prediction, a command's decision, and an observation's
or attribution's command (and an attribution's decision) must belong to the same payment attempt and
provider as the referencing row. Purely additive: no existing constraint is dropped or changed.

Revision ID: f4a5b6c7d8e9
Revises: e2d3c4b5a6f7
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f4a5b6c7d8e9'
down_revision: Union[str, Sequence[str], None] = 'e2d3c4b5a6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CREATE_REQUIRE_NONE = """
CREATE FUNCTION pg_temp.p3c_require_none(check_code text, description text, offending_sql text)
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
    RAISE EXCEPTION 'same_attempt_links preflight % failed: %. % offending row(s); first 10: %',
      check_code, description, offending, sample
      USING HINT = 'Nothing was changed. Resolve the listed records explicitly, then re-run.';
  END IF;
END
$fn$
"""
DROP_REQUIRE_NONE = "DROP FUNCTION pg_temp.p3c_require_none(text, text, text)"

PREFLIGHT_CHECKS = [
    ("PF-C01", "every decision's stage5_prediction_id must reference an existing prediction",
     """SELECT d.decision_id::text FROM intervention_decisions d
        WHERE d.stage5_prediction_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM failure_predictions p WHERE p.prediction_id = d.stage5_prediction_id)"""),
    ("PF-C02", "every decision's prediction must belong to the decision's provider",
     """SELECT d.decision_id::text FROM intervention_decisions d
        JOIN failure_predictions p ON p.prediction_id = d.stage5_prediction_id
        WHERE p.source_system <> d.source_system"""),
    ("PF-C03", "every decision's prediction must belong to the decision's payment attempt",
     """SELECT d.decision_id::text FROM intervention_decisions d
        JOIN failure_predictions p ON p.prediction_id = d.stage5_prediction_id
        WHERE p.payment_attempt_id <> d.payment_attempt_id"""),
    ("PF-C04", "every command must belong to its decision's payment attempt",
     """SELECT c.command_id::text FROM intervention_commands c
        JOIN intervention_decisions d ON d.decision_id = c.decision_id
        WHERE c.payment_attempt_id <> d.payment_attempt_id"""),
    ("PF-C05", "every observation must belong to its command's payment attempt",
     """SELECT o.observation_id::text FROM payment_outcome_observations o
        JOIN intervention_commands c ON c.command_id = o.command_id
        WHERE o.payment_attempt_id <> c.payment_attempt_id"""),
    ("PF-C06", "every attribution must belong to its command's payment attempt",
     """SELECT a.attribution_id::text FROM counterfactual_attributions a
        JOIN intervention_commands c ON c.command_id = a.command_id
        WHERE a.payment_attempt_id <> c.payment_attempt_id"""),
    ("PF-C07", "every attribution must belong to its decision's payment attempt",
     """SELECT a.attribution_id::text FROM counterfactual_attributions a
        JOIN intervention_decisions d ON d.decision_id = a.decision_id
        WHERE a.payment_attempt_id <> d.payment_attempt_id"""),
]

# (name, table, columns): attempt-exact targets for the composite foreign keys below.
UNIQUE_TARGETS = (
    ("uq_failure_prediction_attempt_link", "failure_predictions",
     ["prediction_id", "payment_attempt_id", "source_system"]),
    ("uq_intervention_decision_attempt_link", "intervention_decisions",
     ["decision_id", "payment_attempt_id", "source_system"]),
    ("uq_cmd_attempt_link", "intervention_commands",
     ["command_id", "payment_attempt_id", "source_system"]),
)

# (name, source table, referent table, local columns, remote columns)
FOREIGN_KEYS = (
    ("fk_decision_prediction", "intervention_decisions", "failure_predictions",
     ["stage5_prediction_id", "source_system"], ["prediction_id", "source_system"]),
    ("fk_decision_prediction_attempt", "intervention_decisions", "failure_predictions",
     ["stage5_prediction_id", "payment_attempt_id", "source_system"],
     ["prediction_id", "payment_attempt_id", "source_system"]),
    ("fk_command_decision_attempt", "intervention_commands", "intervention_decisions",
     ["decision_id", "payment_attempt_id", "source_system"],
     ["decision_id", "payment_attempt_id", "source_system"]),
    ("fk_observation_command_attempt", "payment_outcome_observations", "intervention_commands",
     ["command_id", "payment_attempt_id", "source_system"],
     ["command_id", "payment_attempt_id", "source_system"]),
    ("fk_attribution_command_attempt", "counterfactual_attributions", "intervention_commands",
     ["command_id", "payment_attempt_id", "source_system"],
     ["command_id", "payment_attempt_id", "source_system"]),
    ("fk_attribution_decision_attempt", "counterfactual_attributions", "intervention_decisions",
     ["decision_id", "payment_attempt_id", "source_system"],
     ["decision_id", "payment_attempt_id", "source_system"]),
)


def _require_none(code: str, description: str, query: str) -> str:
    quoted_description = description.replace("'", "''")
    return f"SELECT pg_temp.p3c_require_none('{code}', '{quoted_description}', $q${query}$q$)"


def upgrade() -> None:
    """Validate existing links, then add attempt-exact unique targets and foreign keys."""
    op.execute(sa.text(CREATE_REQUIRE_NONE))
    for code, description, query in PREFLIGHT_CHECKS:
        op.execute(sa.text(_require_none(code, description, query)))
    op.execute(sa.text(DROP_REQUIRE_NONE))

    for name, table, columns in UNIQUE_TARGETS:
        op.create_unique_constraint(name, table, columns)

    for name, source, referent, local_cols, remote_cols in FOREIGN_KEYS:
        op.create_foreign_key(name, source, referent, local_cols, remote_cols)


def downgrade() -> None:
    """Restore the e2d3c4b5a6f7 (Revision B) schema exactly; no data is touched."""
    for name, source, _, _, _ in reversed(FOREIGN_KEYS):
        op.drop_constraint(name, source, type_='foreignkey')

    for name, table, _ in reversed(UNIQUE_TARGETS):
        op.drop_constraint(name, table, type_='unique')
