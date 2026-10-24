"""stage5_8_provider_identity_expand

Phase 3 Revision A (expand). Creates the payment_attempts registry, adds a nullable source_system
to the six Stage 5-8 tables, and backfills both from each attempt's trigger event.

Every attempt key referenced by Stage 5-8 rows must resolve to exactly one supported provider
through a valid trigger event, and every child row must agree with its parent; otherwise the
migration aborts with a diagnostic and changes nothing. It never deletes, rewrites or guesses.

This revision must not be left applied anywhere but the disposable test database; migrations/env.py
enforces that before COMMIT. Revision B (contract) enforces NOT NULL and the composite constraints.

Revision ID: 9d2e4f6a8b1c
Revises: 8b9c0d1e2f3a
Create Date: 2026-10-20 11:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = '9d2e4f6a8b1c'
down_revision: Union[str, Sequence[str], None] = '8b9c0d1e2f3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.provider_identity")

# Frozen copies: migrations never import application code.
PAYPAL_IDENTIFIER_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
SOURCE_SYSTEM_CHECK = "source_system IN ('razorpay', 'synthetic', 'paypal')"
NAMESPACE_CHECK = (
    "CASE WHEN source_system = 'paypal' "
    f"THEN provider_reference ~ '{PAYPAL_IDENTIFIER_PATTERN}' "
    "AND payment_attempt_id = 'paypal-order:' || provider_reference "
    "ELSE provider_reference <> '' AND payment_attempt_id = provider_reference "
    "AND left(payment_attempt_id, 13) <> 'paypal-order:' END"
)

# (table, primary key) for the five tables that store payment_attempt_id.
KEYED_TABLES = (
    ("failure_predictions", "prediction_id"),
    ("intervention_decisions", "decision_id"),
    ("intervention_commands", "command_id"),
    ("payment_outcome_observations", "observation_id"),
    ("counterfactual_attributions", "attribution_id"),
)
STAGE5_8_TABLES = KEYED_TABLES + (("intervention_execution_attempts", "attempt_id"),)

CREATE_REQUIRE_NONE = """
CREATE FUNCTION pg_temp.p3_require_none(check_code text, description text, offending_sql text)
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
    RAISE EXCEPTION 'provider_identity preflight % failed: %. % offending row(s); first 10: %',
      check_code, description, offending, sample
      USING HINT = 'Nothing was changed. This migration never deletes, rewrites or reclassifies rows; resolve the listed records explicitly, then re-run.';
  END IF;
END
$fn$
"""
DROP_REQUIRE_NONE = "DROP FUNCTION pg_temp.p3_require_none(text, text, text)"

# S1: every key referenced by the five keyed tables, where it appears, and its earliest record time.
CREATE_KEYS = """
CREATE TEMP TABLE p3_keys ON COMMIT DROP AS
SELECT payment_attempt_id AS attempt_key,
       bool_or(src = 'P') AS in_predictions,
       bool_or(src = 'D') AS in_decisions,
       bool_or(src = 'C') AS in_commands,
       bool_or(src = 'O') AS in_observations,
       bool_or(src = 'A') AS in_attributions,
       min(created_at) AS first_created_at
FROM (
  SELECT payment_attempt_id, created_at, 'P' AS src FROM failure_predictions
  UNION ALL SELECT payment_attempt_id, created_at, 'D' FROM intervention_decisions
  UNION ALL SELECT payment_attempt_id, created_at, 'C' FROM intervention_commands
  UNION ALL SELECT payment_attempt_id, created_at, 'O' FROM payment_outcome_observations
  UNION ALL SELECT payment_attempt_id, created_at, 'A' FROM counterfactual_attributions
) refs
GROUP BY payment_attempt_id
"""

# S2: namespace classification. Validates the key format; the provider comes from the trigger event.
CREATE_CLASSIFIED = """
CREATE TEMP TABLE p3_classified ON COMMIT DROP AS
SELECT attempt_key,
       left(attempt_key, 13) = 'paypal-order:' AS paypal_namespace,
       CASE WHEN left(attempt_key, 13) = 'paypal-order:' THEN substr(attempt_key, 14)
            ELSE attempt_key END AS provider_reference
FROM p3_keys
"""

# S3: every candidate trigger event. Non-PayPal candidates include any non-PayPal source, so
# unsupported values are reported (PF-01) rather than skipped or coerced.
CREATE_CANDIDATES = """
CREATE TEMP TABLE p3_candidates ON COMMIT DROP AS
SELECT c.attempt_key, e.event_id, e.source_system, e.payment_id, e.ingested_at
FROM p3_classified c
JOIN payment_events e
  ON e.payment_id = c.provider_reference
 AND (    (c.paypal_namespace AND e.source_system = 'paypal' AND e.event_type = 'order.approved')
      OR (NOT c.paypal_namespace AND e.source_system <> 'paypal' AND e.event_type = 'payment.authorized'))
"""

# Earliest valid trigger per key; a total order (ingested_at is NOT NULL, event_id is the primary key).
CREATE_RESOLVED = """
CREATE TEMP TABLE p3_resolved ON COMMIT DROP AS
SELECT DISTINCT ON (attempt_key)
       attempt_key, source_system, payment_id AS provider_reference, event_id AS trigger_event_id
FROM p3_candidates
ORDER BY attempt_key, ingested_at, event_id
"""

DROP_WORK_TABLES = "DROP TABLE p3_resolved, p3_candidates, p3_classified, p3_keys"

# (code, description, query returning one text column of identifiers)
PREFLIGHT_BEFORE_RESOLUTION = (
    ("PF-01", "trigger events must come from a supported provider (razorpay, synthetic, paypal)", """
        SELECT DISTINCT attempt_key || ' source_system=' || source_system
        FROM p3_candidates
        WHERE source_system NOT IN ('razorpay', 'synthetic', 'paypal')"""),
    ("PF-02", "attempt keys must be well formed", """
        SELECT format('%L', attempt_key)
        FROM p3_classified
        WHERE attempt_key = ''
           OR (paypal_namespace AND provider_reference !~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')"""),
    ("PF-03", "no non-PayPal payment event may use the PayPal attempt namespace", """
        SELECT event_id::text
        FROM payment_events
        WHERE source_system <> 'paypal' AND left(payment_id, 13) = 'paypal-order:'"""),
    ("PF-04", "every attempt key needs a valid trigger event", """
        SELECT c.attempt_key
        FROM p3_classified c
        WHERE NOT EXISTS (SELECT 1 FROM p3_candidates t WHERE t.attempt_key = c.attempt_key)"""),
    ("PF-05", "every attempt key must resolve to exactly one provider", """
        SELECT attempt_key || ' sources=' || string_agg(DISTINCT source_system, ',' ORDER BY source_system)
        FROM p3_candidates
        GROUP BY attempt_key
        HAVING count(DISTINCT source_system) > 1"""),
    ("PF-06", "observation and attribution keys must belong to a command", """
        SELECT attempt_key
        FROM p3_keys
        WHERE (in_observations OR in_attributions) AND NOT in_commands"""),
    ("PF-07", "a decision's stage5_prediction_id must exist", """
        SELECT d.decision_id::text
        FROM intervention_decisions d
        WHERE d.stage5_prediction_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM failure_predictions p WHERE p.prediction_id = d.stage5_prediction_id)"""),
    ("PF-08", "a decision and its prediction must share the attempt key", """
        SELECT d.decision_id::text
        FROM intervention_decisions d
        JOIN failure_predictions p ON p.prediction_id = d.stage5_prediction_id
        WHERE p.payment_attempt_id <> d.payment_attempt_id"""),
    ("PF-09", "a command's decision must exist", """
        SELECT c.command_id::text
        FROM intervention_commands c
        WHERE NOT EXISTS (SELECT 1 FROM intervention_decisions d WHERE d.decision_id = c.decision_id)"""),
    ("PF-10", "a command must match its decision's attempt key and version", """
        SELECT c.command_id::text
        FROM intervention_commands c
        JOIN intervention_decisions d ON d.decision_id = c.decision_id
        WHERE d.payment_attempt_id <> c.payment_attempt_id OR d.decision_version <> c.decision_version"""),
    ("PF-11", "an execution attempt's command must exist", """
        SELECT ea.attempt_id::text
        FROM intervention_execution_attempts ea
        WHERE NOT EXISTS (SELECT 1 FROM intervention_commands c WHERE c.command_id = ea.command_id)"""),
    ("PF-12", "an observation's command must exist and share the attempt key", """
        SELECT o.observation_id::text || CASE WHEN c.command_id IS NULL THEN ' command missing'
                                              ELSE ' key differs from command' END
        FROM payment_outcome_observations o
        LEFT JOIN intervention_commands c ON c.command_id = o.command_id
        WHERE c.command_id IS NULL OR c.payment_attempt_id <> o.payment_attempt_id"""),
    ("PF-13", "an attribution must match its command's decision and attempt key", """
        SELECT a.attribution_id::text || CASE WHEN c.command_id IS NULL THEN ' command missing'
                                              WHEN c.decision_id <> a.decision_id THEN ' decision_id differs from command'
                                              ELSE ' key differs from command' END
        FROM counterfactual_attributions a
        LEFT JOIN intervention_commands c ON c.command_id = a.command_id
        WHERE c.command_id IS NULL OR c.decision_id <> a.decision_id
           OR c.payment_attempt_id <> a.payment_attempt_id"""),
    ("PF-14", "an attribution's decision must exist and share the attempt key", """
        SELECT a.attribution_id::text
        FROM counterfactual_attributions a
        LEFT JOIN intervention_decisions d ON d.decision_id = a.decision_id
        WHERE d.decision_id IS NULL OR d.payment_attempt_id <> a.payment_attempt_id"""),
)

PREFLIGHT_AFTER_RESOLUTION = (
    ("PF-15", "an observed terminal event must be a terminal event of the attempt's own provider and reference", """
        SELECT o.observation_id::text || CASE
                 WHEN e.event_id IS NULL THEN ' terminal event missing'
                 WHEN e.source_system <> r.source_system
                   THEN ' terminal event provider ' || e.source_system || ' differs from ' || r.source_system
                 WHEN e.event_type NOT IN ('payment.captured', 'payment.failed') THEN ' not a terminal event'
                 ELSE ' terminal event belongs to another attempt' END
        FROM payment_outcome_observations o
        JOIN p3_resolved r ON r.attempt_key = o.payment_attempt_id
        LEFT JOIN payment_events e ON e.event_id = o.terminal_event_id
        WHERE o.terminal_event_id IS NOT NULL
          AND (e.event_id IS NULL
               OR e.source_system <> r.source_system
               OR e.event_type NOT IN ('payment.captured', 'payment.failed')
               OR CASE WHEN r.source_system = 'paypal' THEN e.order_id IS DISTINCT FROM r.provider_reference
                       ELSE e.payment_id <> r.provider_reference END)"""),
    ("PF-16", "registry identities (source_system, provider_reference) must be unique", """
        SELECT source_system || ':' || provider_reference
        FROM p3_resolved
        GROUP BY source_system, provider_reference
        HAVING count(*) > 1"""),
    ("PF-17", "resolved identities must satisfy the registry provider and namespace constraints", """
        SELECT r.attempt_key
        FROM p3_resolved r
        JOIN p3_classified c USING (attempt_key)
        WHERE (r.source_system = 'paypal') <> c.paypal_namespace
           OR r.provider_reference <> c.provider_reference
           OR r.source_system NOT IN ('razorpay', 'synthetic', 'paypal')"""),
)

# Warning-only (PT-01): trigger ingested after the attempt's earliest logical timestamp.
PT01_SUSPICIOUS_TRIGGER_TIMING = """
WITH first_logical AS (
  SELECT payment_attempt_id AS attempt_key, min(ts) AS first_logical_at
  FROM (SELECT payment_attempt_id, predicted_at AS ts FROM failure_predictions
        UNION ALL SELECT payment_attempt_id, decided_at FROM intervention_decisions) x
  GROUP BY payment_attempt_id
)
SELECT r.attempt_key, e.ingested_at AS trigger_ingested_at, f.first_logical_at
FROM p3_resolved r
JOIN payment_events e ON e.event_id = r.trigger_event_id
JOIN first_logical f USING (attempt_key)
WHERE e.ingested_at > f.first_logical_at
ORDER BY r.attempt_key
"""
PT01_SAMPLE_SIZE = 20

INSERT_REGISTRY = """
INSERT INTO payment_attempts (payment_attempt_id, source_system, provider_reference, trigger_event_id, registered_at)
SELECT r.attempt_key, r.source_system, r.provider_reference, r.trigger_event_id, k.first_created_at
FROM p3_resolved r
JOIN p3_keys k USING (attempt_key)
"""

PF18_EVERY_KEY_REGISTERED = ("PF-18", "every referenced attempt key must be registered", """
        SELECT k.attempt_key
        FROM p3_keys k
        WHERE NOT EXISTS (SELECT 1 FROM payment_attempts pa WHERE pa.payment_attempt_id = k.attempt_key)""")

BACKFILL_EXECUTION_ATTEMPTS = """
UPDATE intervention_execution_attempts ea
SET source_system = c.source_system
FROM intervention_commands c
WHERE c.command_id = ea.command_id
"""


def _backfill_keyed_table(table: str) -> str:
    return (
        f"UPDATE {table} t SET source_system = pa.source_system "
        "FROM payment_attempts pa WHERE pa.payment_attempt_id = t.payment_attempt_id"
    )


def _require_none(code: str, description: str, query: str) -> str:
    quoted_description = description.replace("'", "''")
    return f"SELECT pg_temp.p3_require_none('{code}', '{quoted_description}', $q${query}$q$)"


def _pf19(table: str, pk: str) -> tuple:
    return (
        "PF-19",
        f"every {table} row must have a source_system",
        f"SELECT {pk}::text FROM {table} WHERE source_system IS NULL",
    )


def _report_suspicious_trigger_timing() -> None:
    if context.is_offline_mode():
        op.get_context().impl.static_output(
            "-- PT-01 (warning only): trigger-timing report skipped in offline mode"
        )
        return
    rows = op.get_bind().execute(sa.text(PT01_SUSPICIOUS_TRIGGER_TIMING)).all()
    if rows:
        sample = "; ".join(
            f"{r.attempt_key} trigger_ingested_at={r.trigger_ingested_at.isoformat()} "
            f"first_logical_at={r.first_logical_at.isoformat()}"
            for r in rows[:PT01_SAMPLE_SIZE]
        )
        log.warning(
            "PT-01 (warning only): %d attempt(s) have a trigger event ingested after their earliest "
            "prediction or decision; first %d: %s",
            len(rows), min(len(rows), PT01_SAMPLE_SIZE), sample,
        )


def upgrade() -> None:
    """Validate existing Stage 5-8 data, then create and backfill the provider registry."""
    op.execute(sa.text(CREATE_REQUIRE_NONE))
    for statement in (CREATE_KEYS, CREATE_CLASSIFIED, CREATE_CANDIDATES):
        op.execute(sa.text(statement))
    for check in PREFLIGHT_BEFORE_RESOLUTION:
        op.execute(sa.text(_require_none(*check)))
    op.execute(sa.text(CREATE_RESOLVED))
    for check in PREFLIGHT_AFTER_RESOLUTION:
        op.execute(sa.text(_require_none(*check)))
    _report_suspicious_trigger_timing()

    op.create_unique_constraint('uq_payment_event_provider', 'payment_events', ['event_id', 'source_system'])
    op.create_index(
        'ix_payment_events_provider_order',
        'payment_events',
        ['source_system', 'order_id', 'ingested_at'],
        unique=False,
    )

    # Both CHECK constraints exist before the first row is inserted, are validated, and are not deferrable.
    op.create_table(
        'payment_attempts',
        sa.Column('payment_attempt_id', sa.String(length=255), nullable=False),
        sa.Column('source_system', sa.String(length=50), nullable=False),
        sa.Column('provider_reference', sa.String(length=255), nullable=False),
        sa.Column('trigger_event_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('registered_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('payment_attempt_id'),
        sa.UniqueConstraint('payment_attempt_id', 'source_system', name='uq_payment_attempt_provider'),
        sa.UniqueConstraint('source_system', 'provider_reference', name='uq_payment_attempt_reference'),
        sa.CheckConstraint(SOURCE_SYSTEM_CHECK, name='ck_payment_attempt_source_system'),
        sa.CheckConstraint(NAMESPACE_CHECK, name='ck_payment_attempt_namespace'),
        sa.ForeignKeyConstraint(
            ['trigger_event_id', 'source_system'],
            ['payment_events.event_id', 'payment_events.source_system'],
            name='fk_payment_attempt_trigger',
        ),
    )

    op.execute(sa.text(INSERT_REGISTRY))
    op.execute(sa.text(_require_none(*PF18_EVERY_KEY_REGISTERED)))

    for table, _ in STAGE5_8_TABLES:
        op.add_column(table, sa.Column('source_system', sa.String(length=50), nullable=True))
    for table, _ in KEYED_TABLES:
        op.execute(sa.text(_backfill_keyed_table(table)))
    op.execute(sa.text(BACKFILL_EXECUTION_ATTEMPTS))
    for table, pk in STAGE5_8_TABLES:
        op.execute(sa.text(_require_none(*_pf19(table, pk))))

    op.execute(sa.text(DROP_WORK_TABLES))
    op.execute(sa.text(DROP_REQUIRE_NONE))


def downgrade() -> None:
    """Restore the 8b9c0d1e2f3a schema exactly. Rows in pre-existing tables are untouched."""
    for table, _ in reversed(STAGE5_8_TABLES):
        op.drop_column(table, 'source_system')
    op.drop_table('payment_attempts')
    op.drop_index('ix_payment_events_provider_order', table_name='payment_events')
    op.drop_constraint('uq_payment_event_provider', 'payment_events', type_='unique')
