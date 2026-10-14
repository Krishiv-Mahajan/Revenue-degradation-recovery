"""
Static guards that keep tests and scripts from touching the development/demo database.

- Database-backed tests must use the shared fixtures in tests/conftest.py, which only
  connect to a validated TEST_DATABASE_URL. They must not build their own engines or
  embed development URLs.
- No test or script may call metadata.create_all/drop_all; the schema is owned by Alembic.
- Scripts read DATABASE_URL from the environment and carry no hardcoded fallback.
"""
import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = PROJECT_ROOT / "tests"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SHARED_FIXTURES = TESTS_DIR / "conftest.py"

SCHEMA_CALLS = {"create_all", "drop_all"}


def _python_files(root: Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _called_names(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                yield func.attr
            elif isinstance(func, ast.Name):
                yield func.id


def _string_constants(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _contains_dev_url(value: str) -> bool:
    if not value.startswith("postgresql"):
        return False
    return ":5433/" in value or value.endswith("/payment_recovery")


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


DB_TEST_FILES = _python_files(TESTS_DIR / "integration") + _python_files(TESTS_DIR / "api")
ALL_TEST_FILES = [p for p in _python_files(TESTS_DIR) if p != SHARED_FIXTURES]
SCRIPT_FILES = _python_files(SCRIPTS_DIR)


def _rel(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))


@pytest.mark.parametrize("path", ALL_TEST_FILES, ids=_rel)
def test_tests_do_not_call_create_all_or_drop_all(path):
    assert SCHEMA_CALLS.isdisjoint(_called_names(_parse(path)))


@pytest.mark.parametrize("path", DB_TEST_FILES, ids=_rel)
def test_db_tests_use_shared_engine_fixture(path):
    assert "create_async_engine" not in set(_called_names(_parse(path)))


@pytest.mark.parametrize("path", DB_TEST_FILES, ids=_rel)
def test_db_tests_have_no_hardcoded_database_urls(path):
    urls = [v for v in _string_constants(_parse(path)) if v.startswith("postgresql")]
    assert urls == []


@pytest.mark.parametrize("path", SCRIPT_FILES, ids=_rel)
def test_scripts_do_not_call_create_all_or_drop_all(path):
    assert SCHEMA_CALLS.isdisjoint(_called_names(_parse(path)))


@pytest.mark.parametrize("path", SCRIPT_FILES, ids=_rel)
def test_scripts_have_no_hardcoded_dev_database_url(path):
    assert [v for v in _string_constants(_parse(path)) if _contains_dev_url(v)] == []


def test_contains_dev_url_detects_known_forms():
    assert _contains_dev_url("postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery")
    assert _contains_dev_url("postgresql://u:p@db:5432/payment_recovery")
    assert not _contains_dev_url("postgresql+asyncpg://u:p@localhost:5434/payment_recovery_test")


def test_truncate_sql_removed():
    assert not (SCRIPTS_DIR / "truncate.sql").exists()
