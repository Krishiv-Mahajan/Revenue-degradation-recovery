"""
Fixtures for migration tests (upgrade/downgrade through real Alembic subprocesses).

These tests downgrade and re-upgrade the disposable test database, so they run in their own
invocation (`RUN_MIGRATION_TESTS=1 pytest tests/migration`, a separate CI step) and are not even
collected otherwise. The only database they can reach is localhost:5434/payment_recovery_test:
the URL is checked before any connection, current_database() is checked before each test and after
every Alembic call, and every truncation is preceded by the same checks as tests/conftest.py.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.infrastructure.db_safety import TEST_DATABASE_URL_ENV, verify_connected_disposable
from tests.conftest import alembic_subprocess_env, disposable_test_target, verify_and_truncate

RUN_MIGRATION_TESTS_ENV = "RUN_MIGRATION_TESTS"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_REVISION = "8b9c0d1e2f3a"
REVISION_A = "9d2e4f6a8b1c"

if os.environ.get(RUN_MIGRATION_TESTS_ENV) != "1":
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture(scope="session")
def migration_target():
    url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not url:
        pytest.exit(f"{RUN_MIGRATION_TESTS_ENV}=1 requires {TEST_DATABASE_URL_ENV}.", returncode=1)
    return disposable_test_target(url)


async def assert_current_database(engine) -> None:
    async with engine.connect() as conn:
        verify_connected_disposable((await conn.execute(text("SELECT current_database()"))).scalar_one())


class Alembic:
    """Runs Alembic against the validated disposable database only, with an explicit environment."""

    def __init__(self, target, engine):
        self.target = target
        self.engine = engine

    async def run(self, *args, bootstrap=None):
        command = [sys.executable, "-c", bootstrap, *args] if bootstrap else [sys.executable, "-m", "alembic", *args]
        result = subprocess.run(
            command, cwd=PROJECT_ROOT, env=alembic_subprocess_env(self.target), capture_output=True, text=True
        )
        await assert_current_database(self.engine)
        return result

    async def ok(self, *args):
        result = await self.run(*args)
        assert result.returncode == 0, result.stderr
        return result


@pytest_asyncio.fixture
async def migration_db(migration_target):
    """
    Starts every test at the pre-Phase-3 revision with empty tables, and always leaves the
    database at head with empty tables.
    """
    engine = create_async_engine(migration_target, echo=False)
    alembic = Alembic(migration_target, engine)
    try:
        await assert_current_database(engine)
        await _truncate(engine, migration_target)
        await alembic.ok("upgrade", "head")
        await alembic.ok("downgrade", BASE_REVISION)
        yield engine, alembic
    finally:
        try:
            await _truncate(engine, migration_target)
            await alembic.ok("upgrade", "head")
            await _truncate(engine, migration_target)
        finally:
            await engine.dispose()


async def _truncate(engine, target) -> None:
    async with engine.begin() as conn:
        await verify_and_truncate(conn, target, existing_tables_only=True)
