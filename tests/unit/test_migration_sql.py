"""
Database-free checks on the Phase 3 Revision A migration SQL.

V1: every statement compiles through sqlalchemy.text() with no bind parameters and unchanged text,
    so colons ('paypal-order:', ::text), percent signs and dollar quotes reach PostgreSQL intact.
V2: the offline render (--sql) of upgrade and downgrade contains them unchanged.
The PayPal identifier grammar in the registry CHECK must equal the PayPal parser's grammar, and the
opt-in for non-test Alembic targets must never be set by any test.
Whether PostgreSQL executes these statements as intended is verified only by the database-backed
tests in tests/migration, which require separate approval.
"""
import ast
import importlib.util
import os
import re
import subprocess
import sys
import typing
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.dialects.postgresql import asyncpg as pg_asyncpg

from src.core.parsers.paypal import Identifier
from src.infrastructure import models
from src.infrastructure.db_safety import INTERMEDIATE_REVISION_ASSERTION_SQL

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = PROJECT_ROOT / "tests"
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "versions" / "9d2e4f6a8b1c_stage5_8_provider_identity_expand.py"
OFFLINE_URL = "postgresql+asyncpg://offline:offline@offline.invalid:1/offline_parity_test"
BIND_PLACEHOLDER_RE = re.compile(r"(?<![:\w]):[A-Za-z_]\w*")

# Shared with the database-backed grammar test: (identifier, accepted by the PayPal parser).
PAYPAL_ID_CORPUS = (
    ("A", True),
    ("0", True),
    ("8MC585209K746392H", True),
    ("AB-CD_EF", True),
    ("A" * 128, True),
    ("", False),
    ("-ABC", False),
    ("_ABC", False),
    ("A" * 129, False),
    ("AB:CD", False),
    ("AB CD", False),
    ("AB.CD", False),
    ("ABC\n", False),
    ("\u00c9ABC", False),
    ("paypal-order:ABC", False),
)


def _load_revision_a():
    spec = importlib.util.spec_from_file_location("revision_9d2e4f6a8b1c", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REVISION_A = _load_revision_a()


def _revision_a_statements():
    m = REVISION_A
    statements = [m.CREATE_REQUIRE_NONE, m.CREATE_KEYS, m.CREATE_CLASSIFIED, m.CREATE_CANDIDATES]
    statements += [m._require_none(*check) for check in m.PREFLIGHT_BEFORE_RESOLUTION]
    statements += [m.CREATE_RESOLVED]
    statements += [m._require_none(*check) for check in m.PREFLIGHT_AFTER_RESOLUTION]
    statements += [m.PT01_SUSPICIOUS_TRIGGER_TIMING, m.INSERT_REGISTRY, m._require_none(*m.PF18_EVERY_KEY_REGISTERED)]
    statements += [m._backfill_keyed_table(table) for table, _ in m.KEYED_TABLES]
    statements += [m.BACKFILL_EXECUTION_ATTEMPTS]
    statements += [m._require_none(*m._pf19(table, pk)) for table, pk in m.STAGE5_8_TABLES]
    statements += [m.DROP_WORK_TABLES, m.DROP_REQUIRE_NONE, m.SOURCE_SYSTEM_CHECK, m.NAMESPACE_CHECK]
    return statements


ALL_STATEMENTS = _revision_a_statements() + [
    models.PAYMENT_ATTEMPT_SOURCE_SYSTEM_CHECK,
    models.PAYMENT_ATTEMPT_NAMESPACE_CHECK,
    INTERMEDIATE_REVISION_ASSERTION_SQL,
]


class TestV1BindParameterSafety:
    @pytest.mark.parametrize("statement", ALL_STATEMENTS, ids=lambda s: s.strip().split("\n")[0][:60])
    def test_statement_compiles_without_binds_and_unchanged(self, statement):
        compiled = sa.text(statement).compile(dialect=pg_asyncpg.dialect())
        assert dict(compiled.binds) == {}
        assert str(compiled) == statement

    def test_every_op_execute_call_wraps_sqlalchemy_text(self):
        tree = ast.parse(MIGRATION_PATH.read_text())
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute" and getattr(node.func.value, "id", None) == "op"
        ]
        assert calls
        for call in calls:
            argument = call.args[0]
            assert isinstance(argument, ast.Call) and getattr(argument.func, "attr", None) == "text", ast.unparse(call)

    def test_check_descriptions_are_quoted_safely(self):
        rendered = REVISION_A._require_none("PF-XX", "an attempt's key", "SELECT 'x'")
        assert "'an attempt''s key'" in rendered

    def test_fragments_that_must_survive(self):
        joined = "\n".join(ALL_STATEMENTS)
        for fragment in ("'paypal-order:'", "::text", "$fn$", "$q$", "format('%L'", "(%s)", "RAISE EXCEPTION '"):
            assert fragment in joined


def _render(*args):
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}
    env["DATABASE_URL"] = OFFLINE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args, "--sql"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestV2OfflineRender:
    @pytest.fixture(scope="class")
    def upgrade_sql(self):
        return _render("upgrade", "8b9c0d1e2f3a:9d2e4f6a8b1c")

    @pytest.fixture(scope="class")
    def downgrade_sql(self):
        return _render("downgrade", "9d2e4f6a8b1c:8b9c0d1e2f3a")

    def test_upgrade_contains_every_statement_verbatim(self, upgrade_sql):
        for statement in _revision_a_statements()[:-2]:
            if statement is REVISION_A.PT01_SUSPICIOUS_TRIGGER_TIMING:
                continue
            assert statement.strip() in upgrade_sql

    def test_upgrade_renders_no_bind_placeholders(self, upgrade_sql):
        assert "%(" not in upgrade_sql
        assert BIND_PLACEHOLDER_RE.findall(upgrade_sql) == []

    def test_upgrade_reports_pt01_as_skipped_offline(self, upgrade_sql):
        assert "-- PT-01 (warning only): trigger-timing report skipped in offline mode" in upgrade_sql

    def test_downgrade_restores_the_prior_schema(self, downgrade_sql):
        for table, _ in REVISION_A.STAGE5_8_TABLES:
            assert f"ALTER TABLE {table} DROP COLUMN source_system;" in downgrade_sql
        assert "DROP TABLE payment_attempts;" in downgrade_sql
        assert "DROP INDEX ix_payment_events_provider_order;" in downgrade_sql
        assert "ALTER TABLE payment_events DROP CONSTRAINT uq_payment_event_provider;" in downgrade_sql
        assert "version_num='8b9c0d1e2f3a'" in downgrade_sql
        assert "DELETE" not in downgrade_sql and "TRUNCATE" not in downgrade_sql


def _parser_pattern() -> str:
    field_info = typing.get_args(Identifier)[2]
    [pattern] = [m.pattern for m in field_info.metadata if getattr(m, "pattern", None)]
    return pattern


class TestPayPalGrammarAgreement:
    def test_registry_check_uses_the_parser_grammar(self):
        assert _parser_pattern() == models.PAYPAL_IDENTIFIER_PATTERN == REVISION_A.PAYPAL_IDENTIFIER_PATTERN
        assert f"provider_reference ~ '{_parser_pattern()}'" in models.PAYMENT_ATTEMPT_NAMESPACE_CHECK

    def test_models_and_migration_constraints_are_identical(self):
        assert models.PAYMENT_ATTEMPT_NAMESPACE_CHECK == REVISION_A.NAMESPACE_CHECK
        assert models.PAYMENT_ATTEMPT_SOURCE_SYSTEM_CHECK == REVISION_A.SOURCE_SYSTEM_CHECK

    def test_malformed_key_preflight_uses_the_parser_grammar(self):
        [pf02] = [c for c in REVISION_A.PREFLIGHT_BEFORE_RESOLUTION if c[0] == "PF-02"]
        assert f"!~ '{_parser_pattern()}'" in pf02[2]

    @pytest.mark.parametrize("identifier, accepted", PAYPAL_ID_CORPUS)
    def test_corpus_matches_the_parser(self, identifier, accepted):
        adapter = TypeAdapter(Identifier)
        try:
            adapter.validate_python(identifier)
            parsed = True
        except ValidationError:
            parsed = False
        assert parsed is accepted


OPT_IN = "ALEMBIC_ALLOW_NON_TEST" + "_DATABASE"
OPT_IN_CONSTANT = OPT_IN + "_ENV"
PURE_GUARD_TEST = TESTS_DIR / "unit" / "test_alembic_guard.py"


def _names_opt_in(node) -> bool:
    return (
        (isinstance(node, ast.Constant) and node.value == OPT_IN)
        or (isinstance(node, ast.Name) and node.id == OPT_IN_CONSTANT)
        or (isinstance(node, ast.Attribute) and node.attr == OPT_IN_CONSTANT)
    )


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


class TestOptInIsNeverSetByTests:
    @pytest.mark.parametrize("path", sorted(TESTS_DIR.rglob("*.py")), ids=lambda p: str(p.relative_to(TESTS_DIR)))
    def test_no_test_sets_the_opt_in(self, path):
        tree = ast.parse(path.read_text())
        parents = _parents(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                assert not (name in {"setenv", "putenv", "setdefault"} and node.args and _names_opt_in(node.args[0])), (
                    f"{path}:{node.lineno} sets the opt-in"
                )
                assert not any(kw.arg == OPT_IN for kw in node.keywords), f"{path}:{node.lineno} passes the opt-in"
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    assert not (isinstance(target, ast.Subscript) and _names_opt_in(target.slice)), (
                        f"{path}:{node.lineno} assigns the opt-in"
                    )
            if isinstance(node, ast.Constant) and node.value == OPT_IN:
                container = parents.get(node)
                call = parents.get(container)
                assert path == PURE_GUARD_TEST, f"{path}:{node.lineno} mentions the opt-in"
                assert isinstance(container, ast.Dict) and node in container.keys, (
                    f"{path}:{node.lineno} must use the opt-in only as a mapping key"
                )
                assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == "validate_alembic_target", (
                    f"{path}:{node.lineno} must pass the opt-in only directly to validate_alembic_target"
                )

    def test_ci_never_sets_the_opt_in(self):
        assert OPT_IN not in (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text()
