"""
Offline tests for the Alembic guard (src/infrastructure/db_safety.py and migrations/env.py).

Pure-function tests pass mapping literals to the guard; nothing here sets an environment variable.
Subprocess tests run the real env.py with the engine factory replaced by one that raises, so a
guard that failed to refuse could never reach a database.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from src.infrastructure.db_safety import (
    ALEMBIC_INTERMEDIATE_REVISIONS,
    UnsafeDatabaseError,
    final_state_requires_assertion,
    validate_alembic_target,
    validate_alembic_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISPOSABLE_URL = "postgresql+asyncpg://postgres:secretpw@localhost:5434/payment_recovery_test"
REVISION_A = "9d2e4f6a8b1c"
OPT_IN_REFUSAL = "to migrate it deliberately"


class TestUrlValidationBothModes:
    """U1-U6: applied identically offline and online; never connects."""

    @pytest.mark.parametrize("offline", [True, False])
    @pytest.mark.parametrize("environ", [{}, {"DATABASE_URL": ""}, {"DATABASE_URL": "   "}])
    def test_u1_requires_database_url(self, environ, offline):
        with pytest.raises(UnsafeDatabaseError, match="no default database"):
            validate_alembic_target(environ, offline=offline)

    @pytest.mark.parametrize("offline", [True, False])
    def test_u2_rejects_unparseable_url(self, offline):
        with pytest.raises(UnsafeDatabaseError, match="could not be parsed"):
            validate_alembic_target({"DATABASE_URL": "not a url"}, offline=offline)

    @pytest.mark.parametrize("offline", [True, False])
    def test_u2_rejects_non_postgresql_driver(self, offline):
        with pytest.raises(UnsafeDatabaseError, match="PostgreSQL"):
            validate_alembic_target({"DATABASE_URL": "sqlite:////tmp/payment_recovery_test"}, offline=offline)

    @pytest.mark.parametrize("offline", [True, False])
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+asyncpg://postgres:pw@localhost/payment_recovery_test",
            "postgresql+asyncpg://postgres:pw@:5434/payment_recovery_test",
            "postgresql+asyncpg://postgres:pw@localhost:5434/",
        ],
        ids=["no-port", "no-host", "no-database"],
    )
    def test_u3_requires_explicit_host_port_and_database(self, url, offline):
        with pytest.raises(UnsafeDatabaseError, match="explicitly"):
            validate_alembic_target({"DATABASE_URL": url}, offline=offline)

    @pytest.mark.parametrize("offline", [True, False])
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+asyncpg://postgres:pw@localhost:5433/payment_recovery_test",
            "postgresql+asyncpg://postgres:pw@db.internal:5433/production",
            "postgresql+asyncpg://postgres:pw@127.0.0.1:5433/payment_recovery_demo",
        ],
    )
    def test_u4_rejects_port_5433_on_any_host(self, url, offline):
        with pytest.raises(UnsafeDatabaseError, match="5433"):
            validate_alembic_target(
                {"DATABASE_URL": url, "ALEMBIC_TARGET_DATABASE": make_url(url).database}, offline=offline
            )

    @pytest.mark.parametrize("offline", [True, False])
    def test_u4_port_5433_is_rejected_even_with_the_opt_in(self, offline):
        environ_before = dict(os.environ)
        with pytest.raises(UnsafeDatabaseError, match="5433"):
            validate_alembic_target(
                {
                    "DATABASE_URL": "postgresql+asyncpg://postgres:pw@localhost:5433/payment_recovery",
                    "ALEMBIC_TARGET_DATABASE": "payment_recovery",
                    "ALEMBIC_ALLOW_NON_TEST_DATABASE": "yes",
                },
                offline=offline,
            )
        assert dict(os.environ) == environ_before

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
    def test_u5_rejects_the_development_identity(self, host):
        with pytest.raises(UnsafeDatabaseError, match="development database"):
            validate_alembic_url(f"postgresql+asyncpg://postgres:pw@{host}:5433/payment_recovery")

    @pytest.mark.parametrize("offline", [True, False])
    def test_u6_errors_never_contain_the_password(self, offline):
        with pytest.raises(UnsafeDatabaseError) as exc_info:
            validate_alembic_target(
                {"DATABASE_URL": "postgresql+asyncpg://postgres:secretpw@localhost:5433/payment_recovery"},
                offline=offline,
            )
        assert "secretpw" not in str(exc_info.value)

    def test_offline_needs_only_a_valid_url(self):
        parsed = validate_alembic_target(
            {"DATABASE_URL": "postgresql+asyncpg://offline:offline@offline.invalid:1/offline_parity_test"},
            offline=True,
        )
        assert (parsed.host, parsed.port) == ("offline.invalid", 1)


class TestTargetVerificationOnline:
    """T1-T2: online only."""

    def test_disposable_test_database_is_accepted(self):
        parsed = validate_alembic_target(
            {"DATABASE_URL": DISPOSABLE_URL, "ALEMBIC_TARGET_DATABASE": "payment_recovery_test"}, offline=False
        )
        assert parsed.database == "payment_recovery_test"

    @pytest.mark.parametrize("target", [None, "", "payment_recovery", "PAYMENT_RECOVERY_TEST"])
    def test_t1_target_database_must_match_exactly(self, target):
        environ = {"DATABASE_URL": DISPOSABLE_URL}
        if target is not None:
            environ["ALEMBIC_TARGET_DATABASE"] = target
        with pytest.raises(UnsafeDatabaseError, match="ALEMBIC_TARGET_DATABASE"):
            validate_alembic_target(environ, offline=False)

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+asyncpg://postgres:pw@db.internal:5432/payment_recovery",
            "postgresql+asyncpg://postgres:pw@localhost:5434/payment_recovery_demo",
            "postgresql+asyncpg://postgres:pw@localhost:5435/payment_recovery_test",
            "postgresql+asyncpg://postgres:pw@db.internal:5434/payment_recovery_test",
        ],
    )
    def test_t2_non_test_target_requires_the_opt_in(self, url):
        with pytest.raises(UnsafeDatabaseError, match=OPT_IN_REFUSAL):
            validate_alembic_target(
                {"DATABASE_URL": url, "ALEMBIC_TARGET_DATABASE": make_url(url).database}, offline=False
            )

    @pytest.mark.parametrize("value", ["", "true", "1", "YES", "yes "])
    def test_t2_opt_in_must_be_exactly_yes(self, value):
        with pytest.raises(UnsafeDatabaseError, match=OPT_IN_REFUSAL):
            validate_alembic_target(
                {
                    "DATABASE_URL": "postgresql+asyncpg://postgres:pw@db.internal:5432/payment_recovery",
                    "ALEMBIC_TARGET_DATABASE": "payment_recovery",
                    "ALEMBIC_ALLOW_NON_TEST_DATABASE": value,
                },
                offline=False,
            )

    def test_t2_opt_in_allows_a_non_test_target_that_is_not_port_5433(self):
        parsed = validate_alembic_target(
            {
                "DATABASE_URL": "postgresql+asyncpg://postgres:pw@db.internal:5432/payment_recovery",
                "ALEMBIC_TARGET_DATABASE": "payment_recovery",
                "ALEMBIC_ALLOW_NON_TEST_DATABASE": "yes",
            },
            offline=False,
        )
        assert parsed.host == "db.internal"


class TestFinalStateDecision:
    """FS-2 decision logic; the env.py wiring is exercised in test_alembic_destination_resolution.py."""

    DISPOSABLE = make_url(DISPOSABLE_URL)
    OTHER = make_url("postgresql+asyncpg://postgres:pw@db.internal:5432/payment_recovery")

    def test_revision_a_is_the_only_intermediate_revision(self):
        assert ALEMBIC_INTERMEDIATE_REVISIONS == frozenset({REVISION_A})

    @pytest.mark.parametrize("offline", [True, False])
    @pytest.mark.parametrize("heads", [frozenset(), frozenset({"8b9c0d1e2f3a"}), frozenset({"bbbbbbbbbbbb"})])
    def test_non_intermediate_end_states_need_nothing(self, heads, offline):
        assert final_state_requires_assertion(heads, self.OTHER, offline=offline) is False

    def test_offline_intermediate_end_state_requires_the_emitted_assertion(self):
        assert final_state_requires_assertion({REVISION_A}, self.OTHER, offline=True) is True

    def test_online_intermediate_end_state_is_allowed_only_on_the_disposable_database(self):
        assert final_state_requires_assertion({REVISION_A}, self.DISPOSABLE, offline=False) is True

    @pytest.mark.parametrize("heads", [{REVISION_A}, {REVISION_A, "bbbbbbbbbbbb"}])
    def test_online_intermediate_end_state_elsewhere_is_refused(self, heads):
        with pytest.raises(UnsafeDatabaseError, match="intermediate revision"):
            final_state_requires_assertion(heads, self.OTHER, offline=False)


# Replaces every engine factory before env.py is imported, then runs the Alembic CLI.
NO_ENGINE_BOOTSTRAP = """
import sys
import sqlalchemy
import sqlalchemy.ext.asyncio as sa_asyncio

def _refuse(*args, **kwargs):
    raise SystemExit("ENGINE FACTORY CALLED")

sa_asyncio.create_async_engine = _refuse
sa_asyncio.async_engine_from_config = _refuse
sqlalchemy.create_engine = _refuse

from alembic.config import main
main(argv=sys.argv[1:], prog="alembic")
"""


def run_alembic_without_engines(*args, environ):
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}
    env.update(environ)
    return subprocess.run(
        [sys.executable, "-c", NO_ENGINE_BOOTSTRAP, *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


class TestEnvPyNeverConnectsWhenRefusingOrOffline:
    def test_offline_render_creates_no_engine(self):
        result = run_alembic_without_engines(
            "upgrade", "8b9c0d1e2f3a:head", "--sql",
            environ={"DATABASE_URL": "postgresql+asyncpg://offline:offline@offline.invalid:1/offline_parity_test"},
        )
        assert result.returncode == 0, result.stderr
        assert "ENGINE FACTORY CALLED" not in result.stderr
        assert "CREATE TABLE payment_attempts" in result.stdout

    @pytest.mark.parametrize(
        "environ, reason",
        [
            ({}, "no default database"),
            ({"DATABASE_URL": "postgresql+asyncpg://postgres:pw@localhost:5433/payment_recovery",
              "ALEMBIC_TARGET_DATABASE": "payment_recovery"}, "development database"),
            ({"DATABASE_URL": "postgresql+asyncpg://postgres:pw@localhost:5433/payment_recovery_test",
              "ALEMBIC_TARGET_DATABASE": "payment_recovery_test"}, "5433"),
            ({"DATABASE_URL": DISPOSABLE_URL}, "ALEMBIC_TARGET_DATABASE"),
            ({"DATABASE_URL": "postgresql+asyncpg://postgres:pw@offline.invalid:5432/payment_recovery",
              "ALEMBIC_TARGET_DATABASE": "payment_recovery"}, OPT_IN_REFUSAL),
        ],
        ids=["no-url", "dev-identity", "port-5433", "no-target", "non-test-without-opt-in"],
    )
    def test_online_refusals_happen_before_any_engine_is_created(self, environ, reason):
        result = run_alembic_without_engines("upgrade", "head", environ=environ)
        assert result.returncode != 0
        assert reason in result.stderr
        assert "ENGINE FACTORY CALLED" not in result.stderr
        assert "secretpw" not in result.stderr

    @pytest.mark.parametrize("offline_args", [("--sql",), ()], ids=["offline", "online"])
    def test_port_5433_is_refused_in_both_modes_by_env_py(self, offline_args):
        result = run_alembic_without_engines(
            "upgrade", "head", *offline_args,
            environ={"DATABASE_URL": "postgresql+asyncpg://postgres:pw@db.internal:5433/anything",
                     "ALEMBIC_TARGET_DATABASE": "anything"},
        )
        assert result.returncode != 0
        assert "5433" in result.stderr
        assert "ENGINE FACTORY CALLED" not in result.stderr
