"""
Offline tests for the database safety guards in src/infrastructure/db_safety.py.
No database connection is made.
"""
import pytest

from src.infrastructure.db_safety import (
    DEMO_RESET_ALLOWED_DATABASES,
    UnsafeDatabaseError,
    assert_connected_database,
    assert_disposable_test_target,
    build_truncate_statement,
    require_env_database_url,
    validate_demo_reset_database_url,
    validate_test_database_url,
    verify_connected_disposable,
)
from src.infrastructure.models import Base
from tests.conftest import verify_and_truncate

DEV_URL = "postgresql+asyncpg://postgres:secretpw@localhost:5433/payment_recovery"
TEST_URL = "postgresql+asyncpg://postgres:secretpw@localhost:5434/payment_recovery_test"


class TestValidateTestDatabaseUrl:
    def test_accepts_dedicated_test_database(self):
        parsed = validate_test_database_url(TEST_URL, protected_urls=[DEV_URL])
        assert parsed.database == "payment_recovery_test"

    @pytest.mark.parametrize("url", [None, ""])
    def test_rejects_missing_url(self, url):
        with pytest.raises(UnsafeDatabaseError):
            validate_test_database_url(url)

    def test_rejects_dev_database(self):
        with pytest.raises(UnsafeDatabaseError, match="_test"):
            validate_test_database_url(DEV_URL)

    def test_rejects_name_without_test_suffix(self):
        with pytest.raises(UnsafeDatabaseError, match="_test"):
            validate_test_database_url(
                "postgresql+asyncpg://postgres:pw@localhost:5434/payment_recovery_demo"
            )

    def test_rejects_test_suffix_on_dev_port(self):
        with pytest.raises(UnsafeDatabaseError, match="5433"):
            validate_test_database_url(
                "postgresql+asyncpg://postgres:pw@localhost:5433/payment_recovery_test"
            )

    def test_rejects_non_postgres_driver(self):
        with pytest.raises(UnsafeDatabaseError, match="PostgreSQL"):
            validate_test_database_url("sqlite+aiosqlite:///payment_recovery_test")

    def test_rejects_unparseable_url(self):
        with pytest.raises(UnsafeDatabaseError):
            validate_test_database_url("not a url")

    def test_rejects_url_matching_protected_database(self):
        with pytest.raises(UnsafeDatabaseError, match="same database"):
            validate_test_database_url(TEST_URL, protected_urls=[TEST_URL])

    def test_protected_match_normalizes_local_host_aliases(self):
        protected = "postgresql://other:pw@127.0.0.1:5434/payment_recovery_test"
        with pytest.raises(UnsafeDatabaseError, match="same database"):
            validate_test_database_url(TEST_URL, protected_urls=[protected])

    def test_protected_match_uses_default_port(self):
        url = "postgresql+asyncpg://postgres:pw@db/payment_recovery_test"
        protected = "postgresql://postgres:pw@db:5432/payment_recovery_test"
        with pytest.raises(UnsafeDatabaseError, match="same database"):
            validate_test_database_url(url, protected_urls=[protected])

    def test_ignores_empty_and_unparseable_protected_urls(self):
        validate_test_database_url(TEST_URL, protected_urls=[None, "", "garbage"])

    def test_error_message_hides_password(self):
        with pytest.raises(UnsafeDatabaseError) as exc_info:
            validate_test_database_url(DEV_URL)
        assert "secretpw" not in str(exc_info.value)


class TestValidateDemoResetDatabaseUrl:
    @pytest.mark.parametrize("database", sorted(DEMO_RESET_ALLOWED_DATABASES))
    def test_accepts_allowlisted_databases(self, database):
        url = f"postgresql+asyncpg://postgres:pw@localhost:5433/{database}"
        assert validate_demo_reset_database_url(url).database == database

    @pytest.mark.parametrize(
        "database", ["postgres", "payment_recovery_prod", "payment_recovery_test", "Payment_Recovery"]
    )
    def test_rejects_non_allowlisted_databases(self, database):
        url = f"postgresql+asyncpg://postgres:pw@localhost:5433/{database}"
        with pytest.raises(UnsafeDatabaseError, match="allowlist"):
            validate_demo_reset_database_url(url)

    def test_error_message_hides_password(self):
        with pytest.raises(UnsafeDatabaseError) as exc_info:
            validate_demo_reset_database_url("postgresql://u:secretpw@host/prod")
        assert "secretpw" not in str(exc_info.value)


class TestAssertConnectedDatabase:
    def test_matching_database_passes(self):
        assert_connected_database("payment_recovery_test", validate_test_database_url(TEST_URL))

    def test_mismatched_database_raises(self):
        with pytest.raises(UnsafeDatabaseError):
            assert_connected_database("payment_recovery", validate_test_database_url(TEST_URL))


class TestRequireEnvDatabaseUrl:
    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", TEST_URL)
        assert require_env_database_url() == TEST_URL

    def test_raises_when_unset(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(UnsafeDatabaseError, match="DATABASE_URL"):
            require_env_database_url()


class TestBuildTruncateStatement:
    def test_covers_every_model_table_and_preserves_alembic_version(self):
        statement = build_truncate_statement(Base.metadata)
        assert statement.startswith("TRUNCATE TABLE ")
        assert statement.endswith(" RESTART IDENTITY CASCADE")
        for table_name in Base.metadata.tables:
            assert f'"{table_name}"' in statement
        assert "alembic_version" not in statement

    def test_existing_tables_limits_the_statement_to_tables_present(self):
        statement = build_truncate_statement(
            Base.metadata, existing_tables={"payment_events", "alembic_version", "unrelated"}
        )
        assert statement == 'TRUNCATE TABLE "payment_events" RESTART IDENTITY CASCADE'

    def test_no_existing_application_tables_means_nothing_to_truncate(self):
        assert build_truncate_statement(Base.metadata, existing_tables={"alembic_version"}) is None


class TestAssertDisposableTestTarget:
    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "LOCALHOST"])
    def test_accepts_only_the_disposable_identity(self, host):
        url = f"postgresql+asyncpg://postgres:pw@{host}:5434/payment_recovery_test"
        parsed = assert_disposable_test_target(url, protected_urls=[DEV_URL])
        assert (parsed.port, parsed.database) == (5434, "payment_recovery_test")

    @pytest.mark.parametrize(
        "url, reason",
        [
            ("postgresql+asyncpg://postgres:pw@db.internal:5434/payment_recovery_test", "host"),
            ("postgresql+asyncpg://postgres:pw@10.0.0.5:5434/payment_recovery_test", "host"),
            ("postgresql+asyncpg://postgres:pw@localhost/payment_recovery_test", "port"),
            ("postgresql+asyncpg://postgres:pw@localhost:5432/payment_recovery_test", "port"),
            ("postgresql+asyncpg://postgres:pw@localhost:5435/payment_recovery_test", "port"),
            ("postgresql+asyncpg://postgres:pw@localhost:5434/payment_recovery2_test", "payment_recovery_test"),
            ("postgresql+asyncpg://postgres:pw@localhost:5434/other_test", "payment_recovery_test"),
            ("postgresql+asyncpg://postgres:pw@localhost:5433/payment_recovery_test", "5433"),
            ("postgresql+asyncpg://postgres:pw@localhost:5434/PAYMENT_RECOVERY_TEST", "_test"),
            ("postgresql+asyncpg://postgres:pw@localhost:5434/payment_recovery", "_test"),
            ("sqlite+aiosqlite:///payment_recovery_test", "PostgreSQL"),
        ],
    )
    def test_rejects_any_other_target(self, url, reason):
        with pytest.raises(UnsafeDatabaseError, match=reason):
            assert_disposable_test_target(url, protected_urls=[DEV_URL])

    @pytest.mark.parametrize("url", [None, ""])
    def test_rejects_missing_url(self, url):
        with pytest.raises(UnsafeDatabaseError):
            assert_disposable_test_target(url)

    def test_rejects_a_protected_url(self):
        with pytest.raises(UnsafeDatabaseError, match="same database"):
            assert_disposable_test_target(TEST_URL, protected_urls=[TEST_URL])

    def test_error_message_hides_password(self):
        with pytest.raises(UnsafeDatabaseError) as exc_info:
            assert_disposable_test_target("postgresql://u:secretpw@db.internal:5434/payment_recovery_test")
        assert "secretpw" not in str(exc_info.value)


class TestVerifyConnectedDisposable:
    def test_accepts_the_disposable_database(self):
        verify_connected_disposable("payment_recovery_test")

    @pytest.mark.parametrize("database", ["payment_recovery", "payment_recovery_demo", "other_test", ""])
    def test_rejects_any_other_connected_database(self, database):
        with pytest.raises(UnsafeDatabaseError):
            verify_connected_disposable(database)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value

    def scalars(self):
        return list(self._value)


class _FakeConnection:
    """Records statements; answers current_database() and the information_schema table list."""

    def __init__(self, current_database, tables=()):
        self.current_database = current_database
        self.tables = tables
        self.statements = []

    async def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if "current_database()" in sql:
            return _FakeResult(self.current_database)
        if "information_schema.tables" in sql:
            return _FakeResult(self.tables)
        return _FakeResult(None)


class TestVerifyAndTruncate:
    async def test_mismatched_connected_database_aborts_before_any_truncate(self):
        conn = _FakeConnection("payment_recovery")
        with pytest.raises(UnsafeDatabaseError):
            await verify_and_truncate(conn, validate_test_database_url(TEST_URL))
        assert not any(s.startswith("TRUNCATE") for s in conn.statements)

    async def test_verified_database_is_truncated_after_the_check(self):
        conn = _FakeConnection("payment_recovery_test")
        await verify_and_truncate(conn, validate_test_database_url(TEST_URL))
        assert "current_database()" in conn.statements[0]
        assert conn.statements[-1] == build_truncate_statement(Base.metadata)

    async def test_backstop_truncates_only_existing_application_tables(self):
        conn = _FakeConnection("payment_recovery_test", tables=["payment_events", "alembic_version"])
        await verify_and_truncate(conn, validate_test_database_url(TEST_URL), existing_tables_only=True)
        assert conn.statements[-1] == 'TRUNCATE TABLE "payment_events" RESTART IDENTITY CASCADE'

    async def test_backstop_on_an_empty_schema_truncates_nothing(self):
        conn = _FakeConnection("payment_recovery_test", tables=["alembic_version"])
        await verify_and_truncate(conn, validate_test_database_url(TEST_URL), existing_tables_only=True)
        assert not any(s.startswith("TRUNCATE") for s in conn.statements)
