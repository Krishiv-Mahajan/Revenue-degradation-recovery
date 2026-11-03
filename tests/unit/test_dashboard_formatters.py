"""
Runs the dashboard formatter tests (tests/frontend/*.test.mjs) with Node's built-in test runner.
No JavaScript dependencies are installed; Node is preinstalled on the CI runners.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND_TESTS = sorted((Path(__file__).resolve().parents[1] / "frontend").glob("*.test.mjs"))


def test_dashboard_formatters():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed; the dashboard formatter tests need it.")
    assert len(FRONTEND_TESTS) >= 2

    # A non-UTC zone proves timestamps labelled UTC really are UTC.
    env = {**os.environ, "TZ": "Asia/Kolkata"}
    result = subprocess.run(
        [node, "--test", *map(str, FRONTEND_TESTS)], env=env, capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stdout + result.stderr
