"""
Guards that keep destructive database operations away from non-disposable databases.

Two policies are enforced:
- Test fixtures may only truncate a dedicated, disposable test database.
- The demo orchestrator may only reset a database whose name is explicitly allowlisted.

Error messages never include passwords; URLs are rendered with the password hidden.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional, Tuple

from sqlalchemy import MetaData
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"
TEST_DATABASE_NAME_SUFFIX = "_test"

# Host port of the development/demo PostgreSQL container in docker-compose.yml.
DEV_DATABASE_PORT = 5433

# Databases the demo orchestrator is permitted to reset. Exact names only.
DEMO_RESET_ALLOWED_DATABASES = frozenset({"payment_recovery", "payment_recovery_demo"})

_LOCAL_HOST_ALIASES = {"localhost", "127.0.0.1", "::1", ""}


class UnsafeDatabaseError(RuntimeError):
    """Raised when a database URL is not safe for the requested operation."""


def _parse(url: str) -> URL:
    try:
        return make_url(url)
    except ArgumentError as exc:
        raise UnsafeDatabaseError("Database URL could not be parsed.") from exc


def _redacted(parsed: URL) -> str:
    return parsed.render_as_string(hide_password=True)


def _identity(parsed: URL) -> Tuple[str, int, str]:
    host = (parsed.host or "").lower()
    if host in _LOCAL_HOST_ALIASES:
        host = "localhost"
    return host, parsed.port or 5432, parsed.database or ""


def require_env_database_url(env_var: str = "DATABASE_URL") -> str:
    """Return the database URL from the environment, failing clearly if unset."""
    url = os.getenv(env_var)
    if not url:
        raise UnsafeDatabaseError(
            f"{env_var} is not set. Configure it in the environment or .env (see .env.example)."
        )
    return url


def validate_test_database_url(
    url: Optional[str],
    protected_urls: Iterable[Optional[str]] = (),
) -> URL:
    """
    Validate that a URL points at a disposable test database.

    Requirements:
    - PostgreSQL URL that parses cleanly.
    - Database name ends with '_test'.
    - Port is not the development database port (5433).
    - Not the same host/port/database as any protected (development/demo) URL.
    """
    if not url:
        raise UnsafeDatabaseError(f"{TEST_DATABASE_URL_ENV} is not set.")

    parsed = _parse(url)
    if not parsed.drivername.startswith("postgresql"):
        raise UnsafeDatabaseError(
            f"Test database must be PostgreSQL, got driver '{parsed.drivername}'."
        )

    database = parsed.database or ""
    if not database.endswith(TEST_DATABASE_NAME_SUFFIX):
        raise UnsafeDatabaseError(
            f"Refusing to use {_redacted(parsed)}: test database name must end with "
            f"'{TEST_DATABASE_NAME_SUFFIX}'."
        )

    if parsed.port == DEV_DATABASE_PORT:
        raise UnsafeDatabaseError(
            f"Refusing to use {_redacted(parsed)}: port {DEV_DATABASE_PORT} is reserved for "
            "the development/demo database."
        )

    for protected in protected_urls:
        if not protected:
            continue
        try:
            protected_parsed = make_url(protected)
        except ArgumentError:
            continue
        if _identity(protected_parsed) == _identity(parsed):
            raise UnsafeDatabaseError(
                f"Refusing to use {_redacted(parsed)}: it resolves to the same database as "
                "the development/demo configuration."
            )

    return parsed


def validate_demo_reset_database_url(url: str) -> URL:
    """Validate that a URL names a database the demo orchestrator may reset."""
    parsed = _parse(url)
    database = parsed.database or ""
    if database not in DEMO_RESET_ALLOWED_DATABASES:
        allowed = ", ".join(sorted(DEMO_RESET_ALLOWED_DATABASES))
        raise UnsafeDatabaseError(
            f"Refusing to reset {_redacted(parsed)}: database '{database}' is not in the "
            f"demo reset allowlist ({allowed})."
        )
    return parsed


def assert_connected_database(actual_database: str, expected: URL) -> None:
    """Confirm the server-reported current_database() matches the validated URL."""
    if actual_database != expected.database:
        raise UnsafeDatabaseError(
            f"Connected to database '{actual_database}' but expected '{expected.database}'. "
            "Refusing to continue."
        )


def build_truncate_statement(metadata: MetaData) -> str:
    """
    TRUNCATE statement covering every application table in the metadata.
    alembic_version is not part of the metadata and is therefore preserved.
    """
    table_names = ", ".join(f'"{table.name}"' for table in metadata.sorted_tables)
    return f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"
