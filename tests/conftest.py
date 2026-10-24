"""
Shared database fixtures for integration and API tests.

Safety model:
- The only database these fixtures ever connect to is TEST_DATABASE_URL.
  There is no fallback to DATABASE_URL or any development default.
- When TEST_DATABASE_URL is unset, every database-backed test is skipped and
  offline unit/boundary tests run normally.
- Before any connection the URL must name exactly the disposable test database
  (localhost, 127.0.0.1 or ::1; explicit port 5434; database payment_recovery_test)
  and differ from every development URL; otherwise the session aborts.
- The session aborts at start if ALEMBIC_ALLOW_NON_TEST_DATABASE is set: tests never
  migrate anything but the disposable test database.
- The schema is built once per session with `alembic upgrade head`, matching
  production. Tests never call create_all/drop_all. Leftover rows from an earlier
  session are truncated first, so a migration's preflight never sees stale test data.
- Every truncation runs only after the server confirms, in the same transaction,
  that current_database() is payment_recovery_test.
- The application's default session factory (bound to DATABASE_URL, which falls
  back to the development database) is replaced in every test by a guard that
  raises and fails the test. Tests must use these fixtures, an explicit session,
  or a mock instead.
"""
import asyncio
import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.infrastructure.db_safety import (
    ALEMBIC_ALLOW_NON_TEST_DATABASE_ENV,
    ALEMBIC_TARGET_DATABASE_ENV,
    DATABASE_URL_ENV,
    DISPOSABLE_TEST_DATABASE,
    TEST_DATABASE_URL_ENV,
    UnsafeDatabaseError,
    assert_connected_database,
    assert_disposable_test_target,
    build_truncate_statement,
    verify_connected_disposable,
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


TEST_CLIENT_HOSTS = "localhost,127.0.0.1,testserver,test"


@pytest.fixture(autouse=True)
def allowed_test_client_hosts(monkeypatch):
    """The app rejects Host headers outside ALLOWED_HOSTS; TestClient sends 'testserver' and AsyncClient 'test'."""
    monkeypatch.setenv("ALLOWED_HOSTS", TEST_CLIENT_HOSTS)


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


def pytest_sessionstart(session):
    if ALEMBIC_ALLOW_NON_TEST_DATABASE_ENV in os.environ:
        pytest.exit(
            f"{ALEMBIC_ALLOW_NON_TEST_DATABASE_ENV} is set. Tests never migrate a non-test "
            "database; unset it and re-run.",
            returncode=1,
        )


def pytest_collection_modifyitems(config, items):
    for item in items:
        if DB_FIXTURES.intersection(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integration)


def disposable_test_target(url: str) -> URL:
    """Checks C2-C6 (URL only, before any connection); any mismatch aborts the session."""
    try:
        return assert_disposable_test_target(url, protected_urls=_protected_database_urls())
    except UnsafeDatabaseError as exc:
        pytest.exit(f"Refusing to use TEST_DATABASE_URL: {exc}", returncode=1)


async def verify_and_truncate(conn, expected: URL, *, existing_tables_only: bool = False) -> None:
    """Checks C8 then C9: confirm the connected database in this transaction, then truncate."""
    current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    assert_connected_database(current, expected)
    verify_connected_disposable(current)
    existing = None
    if existing_tables_only:
        existing = set(
            (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                    )
                )
            ).scalars()
        )
    statement = build_truncate_statement(Base.metadata, existing_tables=existing)
    if statement is not None:
        await conn.execute(text(statement))


async def _truncate_leftover_rows(expected: URL) -> None:
    # C7: the engine is built from the validated URL object, never from the environment.
    engine = create_async_engine(expected, echo=False)
    try:
        async with engine.begin() as conn:
            await verify_and_truncate(conn, expected, existing_tables_only=True)
    finally:
        await engine.dispose()


def run_isolated(coroutine) -> None:
    """Run a coroutine on a private event loop, independent of any loop pytest-asyncio is running."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, coroutine).result()


def alembic_subprocess_env(expected: URL) -> dict:
    """Explicit environment for Alembic subprocesses: the validated URL and target, never the opt-in."""
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
        if key in os.environ
    }
    env[DATABASE_URL_ENV] = expected.render_as_string(hide_password=False)
    env[ALEMBIC_TARGET_DATABASE_ENV] = DISPOSABLE_TEST_DATABASE
    return env


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not url:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is not set; skipping database-backed test.")

    expected = disposable_test_target(url)
    run_isolated(_truncate_leftover_rows(expected))

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=alembic_subprocess_env(expected),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed on the test database:\n{result.stderr}")
    return url


@pytest_asyncio.fixture
async def db_engine(test_database_url):
    expected = disposable_test_target(test_database_url)
    engine = create_async_engine(expected, echo=False)
    try:
        async with engine.begin() as conn:
            await verify_and_truncate(conn, expected)
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    Session = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
