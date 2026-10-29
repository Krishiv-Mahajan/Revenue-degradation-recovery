"""
Database-free evidence for the final-state guard in migrations/env.py.

DF-1 pins why the requested destination argument is not used for enforcement: Alembic returns
partial and relative targets unresolved. DF-2 shows that the guard instead acts on the state a run
actually ends at, for upgrades and downgrades spelled as full, partial, relative or symbolic
revisions. Every run here is offline (--sql) against an unresolvable host; nothing connects.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from src.infrastructure.db_safety import INTERMEDIATE_REVISION_ASSERTION_SQL

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_URL = "postgresql+asyncpg://offline:offline@offline.invalid:1/offline_parity_test"
BASE_REVISION = "8b9c0d1e2f3a"
REVISION_A = "9d2e4f6a8b1c"
REVISION_B = "e2d3c4b5a6f7"


def render(*args, config_path=PROJECT_ROOT / "alembic.ini"):
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}
    env["DATABASE_URL"] = OFFLINE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(config_path), *args, "--sql"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def ends_with_assertion(sql: str) -> bool:
    body = sql.rstrip()
    assert body.endswith("COMMIT;"), "offline render must end with COMMIT"
    return body[: -len("COMMIT;")].rstrip().endswith(INTERMEDIATE_REVISION_ASSERTION_SQL + ";")


class TestDestinationArgumentIsNotReliable:
    """DF-1: Alembic 1.19.1 resolves only symbolic and full ids in get_revision_argument()."""

    @pytest.fixture(scope="class")
    def script(self):
        return ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))

    def test_symbolic_and_full_ids_resolve(self, script):
        assert script.as_revision_number("head") == REVISION_B
        assert script.as_revision_number("heads") == (REVISION_B,)
        assert script.as_revision_number(REVISION_A) == REVISION_A
        assert script.as_revision_number(REVISION_B) == REVISION_B
        assert script.as_revision_number("base") is None

    @pytest.mark.parametrize("target", ["9d2e", "+1", "-1", "head-1", f"{BASE_REVISION}+1"])
    def test_partial_and_relative_targets_come_back_unresolved(self, script, target):
        assert script.as_revision_number(target) == target


class TestFinalStateGuardOnRealMigrations:
    """DF-2 on the real chain, where Revision B is currently the head and Revision A is intermediate."""

    @pytest.mark.parametrize(
        "args",
        [
            ("upgrade", REVISION_A),
            ("upgrade", "9d2e"),
            ("upgrade", f"{BASE_REVISION}:{REVISION_A}"),
            ("upgrade", f"{BASE_REVISION}:+1"),
            ("upgrade", "8b9c:9d2e"),
            ("upgrade", f"{BASE_REVISION}:head-1"),
        ],
        ids=["full", "partial", "range", "relative", "partial-range", "relative-head"],
    )
    def test_upgrades_ending_at_revision_a_emit_the_assertion(self, args):
        assert ends_with_assertion(render(*args))

    @pytest.mark.parametrize(
        "args",
        [
            ("upgrade", "head"),
            ("upgrade", "heads"),
            ("upgrade", REVISION_B),
            ("upgrade", "e2d3"),
            ("upgrade", f"{BASE_REVISION}:{REVISION_B}"),
            ("upgrade", f"{BASE_REVISION}:+2"),
            ("upgrade", f"{REVISION_A}:{REVISION_B}"),
        ],
        ids=["head", "heads", "full", "partial", "full-range", "relative-2", "a-to-b"],
    )
    def test_upgrades_ending_at_revision_b_emit_no_assertion(self, args):
        assert not ends_with_assertion(render(*args))

    @pytest.mark.parametrize(
        "args",
        [
            ("downgrade", f"{REVISION_B}:{REVISION_A}"),
            ("downgrade", f"{REVISION_B}:-1"),
            ("downgrade", "e2d3:9d2e"),
            ("downgrade", f"head:{REVISION_A}"),
        ],
        ids=["full", "relative", "partial", "symbolic"],
    )
    def test_downgrades_back_to_revision_a_emit_the_assertion(self, args):
        assert ends_with_assertion(render(*args))

    @pytest.mark.parametrize(
        "args",
        [
            ("downgrade", f"{REVISION_A}:{BASE_REVISION}"),
            ("downgrade", f"{REVISION_A}:-1"),
            ("downgrade", "9d2e:8b9c"),
            ("downgrade", f"{REVISION_B}:{BASE_REVISION}"),
            ("downgrade", f"head:{BASE_REVISION}"),
        ],
        ids=["full-a", "relative-a", "partial-a", "b-to-base", "head-to-base"],
    )
    def test_downgrades_back_to_the_prior_revision_emit_no_assertion(self, args):
        sql = render(*args)
        assert f"version_num='{BASE_REVISION}'" in sql
        assert not ends_with_assertion(sql)

    def test_upgrades_ending_before_revision_a_emit_no_assertion(self):
        assert not ends_with_assertion(render("upgrade", f"base:{BASE_REVISION}"))


SYNTHETIC_ROOT = "f0f0f0f0f0f0"
SYNTHETIC_B = "fb0b0b0b0b0b"
SYNTHETIC_REVISION = '''"""synthetic test revision"""
from alembic import op
import sqlalchemy as sa

revision = "{revision}"
down_revision = {down_revision}
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("SELECT 'upgrade {revision}'"))


def downgrade():
    op.execute(sa.text("SELECT 'downgrade {revision}'"))
'''


@pytest.fixture(scope="module")
def synthetic_config(tmp_path_factory):
    """
    The real env.py with a synthetic chain root -> Revision A's id -> a later revision, so paths
    that pass through or end at the intermediate revision can be rendered before Revision B exists.
    """
    root = tmp_path_factory.mktemp("synthetic_alembic")
    versions = root / "versions"
    versions.mkdir()
    chain = [(SYNTHETIC_ROOT, None), (REVISION_A, SYNTHETIC_ROOT), (SYNTHETIC_B, REVISION_A)]
    for revision, down_revision in chain:
        (versions / f"{revision}_synthetic.py").write_text(
            SYNTHETIC_REVISION.format(
                revision=revision, down_revision=repr(down_revision) if down_revision else "None"
            )
        )
    config_path = root / "alembic.ini"
    real_ini = (PROJECT_ROOT / "alembic.ini").read_text()
    config_path.write_text(
        real_ini.replace(
            "script_location = %(here)s/migrations",
            f"script_location = {PROJECT_ROOT / 'migrations'}\nversion_locations = {versions}",
        ).replace("prepend_sys_path = .", f"prepend_sys_path = {PROJECT_ROOT}")
    )
    return config_path


class TestFinalStateGuardOnSyntheticChain:
    """DF-2 for paths that continue past, or come back down to, the intermediate revision."""

    @pytest.mark.parametrize(
        "args, expected",
        [
            (("upgrade", f"{SYNTHETIC_ROOT}:{REVISION_A}"), True),
            (("upgrade", f"{SYNTHETIC_ROOT}:9d2e"), True),
            (("upgrade", f"{SYNTHETIC_ROOT}:+1"), True),
            (("upgrade", f"{SYNTHETIC_ROOT}:{SYNTHETIC_B}"), False),
            (("upgrade", f"{SYNTHETIC_ROOT}:+2"), False),
            (("upgrade", f"{SYNTHETIC_ROOT}:head"), False),
            (("upgrade", f"{SYNTHETIC_ROOT}:fb0b"), False),
        ],
        ids=["full-a", "partial-a", "relative-a", "full-b", "relative-b", "head-b", "partial-b"],
    )
    def test_upgrade_paths(self, synthetic_config, args, expected):
        assert ends_with_assertion(render(*args, config_path=synthetic_config)) is expected

    @pytest.mark.parametrize(
        "args, expected",
        [
            (("downgrade", f"{SYNTHETIC_B}:{REVISION_A}"), True),
            (("downgrade", f"{SYNTHETIC_B}:9d2e"), True),
            (("downgrade", f"{SYNTHETIC_B}:-1"), True),
            (("downgrade", f"head:{REVISION_A}"), True),
            (("downgrade", f"{SYNTHETIC_B}:{SYNTHETIC_ROOT}"), False),
            (("downgrade", f"{SYNTHETIC_B}:-2"), False),
            (("downgrade", f"{SYNTHETIC_B}:f0f0"), False),
        ],
        ids=["full-a", "partial-a", "relative-a", "head-to-a", "full-root", "relative-root", "partial-root"],
    )
    def test_downgrade_paths(self, synthetic_config, args, expected):
        assert ends_with_assertion(render(*args, config_path=synthetic_config)) is expected

    def test_a_run_that_passes_through_the_intermediate_revision_checks_only_the_end_state(self, synthetic_config):
        sql = render("upgrade", f"{SYNTHETIC_ROOT}:{SYNTHETIC_B}", config_path=synthetic_config)
        assert "'upgrade 9d2e4f6a8b1c'" in sql and "'upgrade fb0b0b0b0b0b'" in sql
        assert INTERMEDIATE_REVISION_ASSERTION_SQL not in sql
