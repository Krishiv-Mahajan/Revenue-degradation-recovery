"""
Guards that keep destructive database operations away from non-disposable databases.

Policies enforced:
- Test fixtures may only truncate the disposable test database localhost:5434/payment_recovery_test.
- The demo orchestrator may only reset a database whose name is explicitly allowlisted.
- Alembic requires an explicit, valid DATABASE_URL, never accepts port 5433, and may only leave a
  database at an intermediate revision on the disposable test database.

Error messages never include passwords; URLs are rendered with the password hidden.
"""
from __future__ import annotations

import os
from typing import AbstractSet, Iterable, Mapping, Optional, Tuple

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

DATABASE_URL_ENV = "DATABASE_URL"
DEV_DATABASE_NAME = "payment_recovery"

# The only database that tests may connect to, truncate, or migrate.
DISPOSABLE_TEST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DISPOSABLE_TEST_PORT = 5434
DISPOSABLE_TEST_DATABASE = "payment_recovery_test"

ALEMBIC_TARGET_DATABASE_ENV = "ALEMBIC_TARGET_DATABASE"
ALEMBIC_ALLOW_NON_TEST_DATABASE_ENV = "ALEMBIC_ALLOW_NON_TEST_DATABASE"
ALEMBIC_ALLOW_NON_TEST_DATABASE_VALUE = "yes"

# Revisions a non-disposable database must never be left at: Phase 3 Revision A (expand) is only
# safe together with Revision B (contract), applied in the same `alembic upgrade head` transaction.
ALEMBIC_INTERMEDIATE_REVISIONS = frozenset({"9d2e4f6a8b1c"})

# Emitted (offline) or executed (online) before COMMIT whenever a run ends at an intermediate
# revision, so even a rendered SQL script cannot leave another database there.
INTERMEDIATE_REVISION_ASSERTION_SQL = (
    "DO $$ BEGIN "
    f"IF current_database() <> '{DISPOSABLE_TEST_DATABASE}' THEN "
    "RAISE EXCEPTION 'Phase 3 Revision A may only remain applied on "
    f"{DISPOSABLE_TEST_DATABASE}; apply Revisions A and B together with alembic upgrade head'; "
    "END IF; END $$"
)


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


def is_disposable_test_identity(parsed: URL) -> bool:
    """True only for localhost:5434/payment_recovery_test with an explicit port."""
    return (
        (parsed.host or "").lower() in DISPOSABLE_TEST_HOSTS
        and parsed.port == DISPOSABLE_TEST_PORT
        and parsed.database == DISPOSABLE_TEST_DATABASE
    )


def assert_disposable_test_target(
    url: Optional[str],
    protected_urls: Iterable[Optional[str]] = (),
) -> URL:
    """
    Validate, before any connection, that a URL names the disposable test database exactly:
    PostgreSQL, a local host, explicit port 5434, database payment_recovery_test, and not any
    protected (development/demo) URL.
    """
    parsed = validate_test_database_url(url, protected_urls=protected_urls)
    host = (parsed.host or "").lower()
    if host not in DISPOSABLE_TEST_HOSTS:
        raise UnsafeDatabaseError(
            f"Refusing to use {_redacted(parsed)}: the test database host must be one of "
            f"{', '.join(sorted(DISPOSABLE_TEST_HOSTS))}."
        )
    if parsed.port != DISPOSABLE_TEST_PORT:
        raise UnsafeDatabaseError(
            f"Refusing to use {_redacted(parsed)}: the test database port must be explicitly "
            f"{DISPOSABLE_TEST_PORT}."
        )
    if parsed.database != DISPOSABLE_TEST_DATABASE:
        raise UnsafeDatabaseError(
            f"Refusing to use {_redacted(parsed)}: the test database must be "
            f"'{DISPOSABLE_TEST_DATABASE}'."
        )
    return parsed


def verify_connected_disposable(actual_database: str) -> None:
    """Confirm the server-reported current_database() is the disposable test database."""
    if actual_database != DISPOSABLE_TEST_DATABASE:
        raise UnsafeDatabaseError(
            f"Connected to database '{actual_database}' but expected "
            f"'{DISPOSABLE_TEST_DATABASE}'. Refusing to continue."
        )


def validate_alembic_url(raw_url: Optional[str]) -> URL:
    """
    URL validation for every Alembic run, offline or online. Never connects.
    Requires an explicit PostgreSQL URL with host, port and database, and rejects port 5433.
    """
    if not raw_url or not raw_url.strip():
        raise UnsafeDatabaseError(
            f"{DATABASE_URL_ENV} is not set. Alembic has no default database and never falls "
            f"back to port {DEV_DATABASE_PORT}."
        )
    parsed = _parse(raw_url)
    if not parsed.drivername.startswith("postgresql"):
        raise UnsafeDatabaseError(
            f"Alembic requires a PostgreSQL URL, got driver '{parsed.drivername}'."
        )
    if not parsed.host or parsed.port is None or not parsed.database:
        raise UnsafeDatabaseError(
            f"Refusing {_redacted(parsed)}: the URL must name the host, port and database "
            "explicitly."
        )
    if _identity(parsed) == ("localhost", DEV_DATABASE_PORT, DEV_DATABASE_NAME):
        raise UnsafeDatabaseError(
            f"Refusing {_redacted(parsed)}: it is the development database."
        )
    if parsed.port == DEV_DATABASE_PORT:
        raise UnsafeDatabaseError(
            f"Refusing {_redacted(parsed)}: port {DEV_DATABASE_PORT} is reserved for the "
            "development/demo database and is blocked for Alembic."
        )
    return parsed


def validate_alembic_target(environ: Mapping[str, str], *, offline: bool) -> URL:
    """
    Guard for migrations/env.py.

    URL validation (both modes): see validate_alembic_url.
    Target verification (online only): ALEMBIC_TARGET_DATABASE must equal the URL's database, and
    any target other than the disposable test database additionally requires
    ALEMBIC_ALLOW_NON_TEST_DATABASE=yes. The connected current_database() is verified separately,
    inside the migration transaction.
    """
    parsed = validate_alembic_url(environ.get(DATABASE_URL_ENV))
    if offline:
        return parsed

    target = environ.get(ALEMBIC_TARGET_DATABASE_ENV)
    if target != parsed.database:
        raise UnsafeDatabaseError(
            f"Refusing {_redacted(parsed)}: {ALEMBIC_TARGET_DATABASE_ENV} must be set to the "
            "URL's database name exactly."
        )

    disposable = is_disposable_test_identity(parsed)
    if disposable:
        validate_test_database_url(environ.get(DATABASE_URL_ENV))
    elif environ.get(ALEMBIC_ALLOW_NON_TEST_DATABASE_ENV) != ALEMBIC_ALLOW_NON_TEST_DATABASE_VALUE:
        raise UnsafeDatabaseError(
            f"Refusing {_redacted(parsed)}: it is not the disposable test database "
            f"(localhost:{DISPOSABLE_TEST_PORT}/{DISPOSABLE_TEST_DATABASE}); set "
            f"{ALEMBIC_ALLOW_NON_TEST_DATABASE_ENV}={ALEMBIC_ALLOW_NON_TEST_DATABASE_VALUE} "
            "to migrate it deliberately."
        )
    return parsed


def final_state_requires_assertion(
    final_heads: AbstractSet[str],
    target: URL,
    *,
    offline: bool,
    intermediate_revisions: AbstractSet[str] = ALEMBIC_INTERMEDIATE_REVISIONS,
) -> bool:
    """
    Decide what a migration run that ends at `final_heads` must do before COMMIT.

    Returns True when the run ends at an intermediate revision: the caller must then execute
    (online) or emit (offline) INTERMEDIATE_REVISION_ASSERTION_SQL. Online, a run ending there on
    anything but the disposable test database is refused outright.
    """
    if not set(final_heads) & set(intermediate_revisions):
        return False
    if not offline and not is_disposable_test_identity(target):
        raise UnsafeDatabaseError(
            f"Refusing to leave {_redacted(target)} at intermediate revision(s) "
            f"{', '.join(sorted(set(final_heads) & set(intermediate_revisions)))}; apply "
            "Revisions A and B together with alembic upgrade head."
        )
    return True


def build_truncate_statement(
    metadata: MetaData, existing_tables: Optional[AbstractSet[str]] = None
) -> Optional[str]:
    """
    TRUNCATE statement covering every application table in the metadata, or only those of them in
    `existing_tables` (for a schema at an older revision). None when there is nothing to truncate.
    alembic_version is not part of the metadata and is therefore preserved.
    """
    tables = [
        table.name
        for table in metadata.sorted_tables
        if existing_tables is None or table.name in existing_tables
    ]
    if not tables:
        return None
    table_names = ", ".join(f'"{name}"' for name in tables)
    return f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"
