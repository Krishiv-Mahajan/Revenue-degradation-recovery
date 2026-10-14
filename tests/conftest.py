"""
Shared database fixtures for integration and API tests.

Safety model:
- The only database these fixtures ever connect to is TEST_DATABASE_URL.
  There is no fallback to DATABASE_URL or any development default.
- When TEST_DATABASE_URL is unset, every database-backed test is skipped and
  offline unit/boundary tests run normally.
- The URL is validated (name must end in '_test', port must not be 5433, and it
  must differ from the development DATABASE_URL) before any connection is made.
- The schema is built once per session with `alembic upgrade head`, matching
  production. Tests never call create_all/drop_all.
- Before each test the application tables are truncated, but only after the
  server confirms current_database() matches the validated test database.
- The application's default session factory (bound to DATABASE_URL, which falls
  back to the development database) is replaced in every test by a guard that
  raises and fails the test. Tests must use these fixtures, an explicit session,
  or a mock instead.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.infrastructure.db_safety import (
    TEST_DATABASE_URL_ENV,
    UnsafeDatabaseError,
    assert_connected_database,
    build_truncate_statement,
    validate_test_database_url,
)
from src.infrastructure.models import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_FIXTURES = {"test_database_url", "db_engine", "db_session"}

# Every module-level reference to the application's default session factory.
# live_pipeline imports the name directly, so it must be patched separately.
APP_DEFAULT_SESSION_FACTORIES = (
    "src.infrastructure.database.AsyncSessionLocal",
    "src.core.services.live_pipeline.AsyncSessionLocal",
)


class DefaultSessionGuard:
    """Stands in for AsyncSessionLocal; records and rejects every use."""

    def __init__(self):
        self.attempts = 0

    def __call__(self, *args, **kwargs):
        self.attempts += 1
        raise UnsafeDatabaseError(
            "Tests must not use the application's default AsyncSessionLocal "
            "(bound to DATABASE_URL). Use the db_session/db_engine fixtures, pass an "
            "explicit session, or mock the caller."
        )


@pytest.fixture(autouse=True)
def default_session_guard(monkeypatch):
    guard = DefaultSessionGuard()
    for target in APP_DEFAULT_SESSION_FACTORIES:
        monkeypatch.setattr(target, guard)
    yield guard
    # Callers such as the live pipeline may swallow the exception, so fail here too.
    assert guard.attempts == 0, (
        f"Test used the application's default AsyncSessionLocal {guard.attempts} time(s)."
    )


def _protected_database_urls():
    """Every development/demo URL the application could resolve to."""
    from src.infrastructure.database import DATABASE_URL as app_database_url

    env_file = PROJECT_ROOT / ".env"
    dotenv_url = dotenv_values(env_file).get("DATABASE_URL") if env_file.exists() else None
    return [os.environ.get("DATABASE_URL"), dotenv_url, app_database_url]


def pytest_collection_modifyitems(config, items):
    for item in items:
        if DB_FIXTURES.intersection(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not url:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is not set; skipping database-backed test.")

    validate_test_database_url(url, protected_urls=_protected_database_urls())

    env = dict(os.environ)
    env["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed on the test database:\n{result.stderr}")
    return url


@pytest_asyncio.fixture
async def db_engine(test_database_url):
    expected = validate_test_database_url(
        test_database_url, protected_urls=_protected_database_urls()
    )
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as conn:
            current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert_connected_database(current, expected)
            await conn.execute(text(build_truncate_statement(Base.metadata)))
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    Session = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
