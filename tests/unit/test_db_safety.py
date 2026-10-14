"""
Offline tests for the database safety guards in src/infrastructure/db_safety.py.
No database connection is made.
"""
import pytest

from src.infrastructure.db_safety import (
    DEMO_RESET_ALLOWED_DATABASES,
    UnsafeDatabaseError,
    assert_connected_database,
    build_truncate_statement,
    require_env_database_url,
    validate_demo_reset_database_url,
    validate_test_database_url,
)
from src.infrastructure.models import Base

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
