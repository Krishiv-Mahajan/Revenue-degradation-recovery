"""
Offline model/migration parity.

The Alembic chain is rendered in offline mode (`alembic upgrade head --sql`, no database
connection) and compared with the DDL compiled from the SQLAlchemy models using the same
PostgreSQL dialect. Tables, columns (type and nullability), named constraints (primary keys,
unique constraints, foreign keys) and indexes must match exactly.

The live counterpart (tests/integration/test_schema_parity_live.py) runs Alembic's
autogenerate comparison against the migrated test database.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from src.infrastructure.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Offline mode never connects; this URL only selects the PostgreSQL dialect.
OFFLINE_DATABASE_URL = "postgresql+asyncpg://offline:offline@offline.invalid:1/offline_parity_test"

CREATE_TABLE_RE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\s*\)", re.DOTALL)
ADD_COLUMN_RE = re.compile(r"ALTER TABLE (\w+) ADD COLUMN (\w+) ([^;]+);")
# Constraint and nullability changes, applied in the order the migrations emit them.
ALTER_TABLE_RE = re.compile(
    r"ALTER TABLE (\w+) (?:"
    r"ADD (?P<add>CONSTRAINT \w+ [^;]+)"
    r"|DROP CONSTRAINT (?P<drop>\w+)"
    r"|ALTER COLUMN (?P<column>\w+) (?P<nullability>SET|DROP) NOT NULL"
    r");"
)
CREATE_INDEX_RE = re.compile(r"CREATE (UNIQUE )?INDEX (\w+) ON (\w+) \(([^)]*)\)")
# Transaction-scoped work objects of the Phase 3 provider-identity migrations are not schema.
UNSUPPORTED_DDL_RE = re.compile(
    r"^\s*("
    r"ALTER TABLE \w+ (?!ADD COLUMN |ADD CONSTRAINT |DROP CONSTRAINT |ALTER COLUMN \w+ (?:SET|DROP) NOT NULL;)"
    r"|DROP (?!TABLE p3_\w+(?:, p3_\w+)*;|FUNCTION pg_temp\.)"
    r"|CREATE (?!TABLE |INDEX |UNIQUE INDEX |TEMP TABLE p3_\w+ ON COMMIT DROP AS|FUNCTION pg_temp\.)"
    r")",
    re.MULTILINE,
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(",").strip()


def _parse_schema(ddl: str):
    tables = {}
    for name, body in CREATE_TABLE_RE.findall(ddl):
        if name == "alembic_version":
            continue
        columns, constraints = {}, set()
        for raw_line in body.splitlines():
            line = _normalize(raw_line)
            if not line:
                continue
            if line.startswith(("PRIMARY KEY", "CONSTRAINT", "UNIQUE", "FOREIGN KEY")):
                constraints.add(line)
            else:
                column, definition = line.split(" ", 1)
                columns[column] = definition
        tables[name] = {"columns": columns, "constraints": constraints}

    for table, column, definition in ADD_COLUMN_RE.findall(ddl):
        tables[table]["columns"][column] = _normalize(definition)

    for match in ALTER_TABLE_RE.finditer(ddl):
        table = tables[match.group(1)]
        if match.group("add"):
            table["constraints"].add(_normalize(match.group("add")))
        elif match.group("drop"):
            name = match.group("drop")
            dropped = {c for c in table["constraints"] if c.startswith(f"CONSTRAINT {name} ")}
            assert len(dropped) == 1, f"cannot drop unknown constraint {name}"
            table["constraints"] -= dropped
        else:
            column, definition = match.group("column"), table["columns"][match.group("column")]
            definition = definition.replace(" NOT NULL", "")
            if match.group("nullability") == "SET":
                definition += " NOT NULL"
            table["columns"][column] = definition

    indexes = {
        name: (table, _normalize(cols), bool(unique))
        for unique, name, table, cols in CREATE_INDEX_RE.findall(ddl)
    }
    return tables, indexes


@pytest.fixture(scope="module")
def migration_ddl() -> str:
    env = {k: v for k, v in os.environ.items() if k != "TEST_DATABASE_URL"}
    env["DATABASE_URL"] = OFFLINE_DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def model_ddl() -> str:
    dialect = postgresql.dialect()
    statements = []
    for table in Base.metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in sorted(table.indexes, key=lambda i: i.name):
            statements.append(str(CreateIndex(index).compile(dialect=dialect)))
    return ";\n".join(statements) + ";"


def test_migrations_use_only_parsed_ddl(migration_ddl):
    """Guards the parser: a new kind of DDL in migrations must be modelled here first."""
    assert UNSUPPORTED_DDL_RE.findall(migration_ddl) == []


def test_tables_match(migration_ddl, model_ddl):
    migration_tables, _ = _parse_schema(migration_ddl)
    model_tables, _ = _parse_schema(model_ddl)
    assert sorted(migration_tables) == sorted(model_tables)


def test_columns_match(migration_ddl, model_ddl):
    migration_tables, _ = _parse_schema(migration_ddl)
    model_tables, _ = _parse_schema(model_ddl)
    for table in model_tables:
        assert model_tables[table]["columns"] == migration_tables[table]["columns"], table


def test_constraints_match(migration_ddl, model_ddl):
    migration_tables, _ = _parse_schema(migration_ddl)
    model_tables, _ = _parse_schema(model_ddl)
    for table in model_tables:
        assert model_tables[table]["constraints"] == migration_tables[table]["constraints"], table


def test_indexes_match(migration_ddl, model_ddl):
    _, migration_indexes = _parse_schema(migration_ddl)
    _, model_indexes = _parse_schema(model_ddl)
    assert model_indexes == migration_indexes


def test_foreign_keys_declared_on_models(model_ddl):
    model_tables, _ = _parse_schema(model_ddl)
    foreign_keys = {
        constraint.split(" ")[1]
        for table in model_tables.values()
        for constraint in table["constraints"]
        if "FOREIGN KEY" in constraint
    }
    assert foreign_keys == {
        "fk_candidate_cause_evaluation",
        "fk_attribution_command",
        "fk_attribution_decision",
        "fk_payment_attempt_trigger",
        "fk_failure_predictions_payment_attempt",
        "fk_intervention_decisions_payment_attempt",
        "fk_intervention_commands_payment_attempt",
        "fk_payment_outcome_observations_payment_attempt",
        "fk_counterfactual_attributions_payment_attempt",
        "fk_command_decision",
        "fk_exec_attempt_command",
        "fk_observation_command",
    }
