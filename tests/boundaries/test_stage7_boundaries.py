"""
Stage 7 boundary assertions.

Ensures strict stage isolation:
- No LLM imports or calls
- No Stage 8 protected GMV, recovered GMV, or counterfactual attribution
- No provider API SDK imports in core execution domain
- No mutation of upstream records
"""
import os

_STAGE7_FILES = [
    "src/core/domain/execution_models.py",
    "src/core/domain/execution_exceptions.py",
    "src/core/execution/state_machine.py",
    "src/core/execution/executor.py",
    "src/core/execution/simulator.py",
    "src/core/execution/execution_service.py",
    "src/core/execution/observation_service.py",
    "src/infrastructure/execution_repository.py",
]


def _read_src(filename: str) -> str:
    project_root = os.path.join(os.path.dirname(__file__), "../..")
    path = os.path.join(project_root, filename)
    with open(path, "r") as f:
        return f.read()


def test_stage7_no_llm_imports():
    """Static assertion: Stage 7 source contains no LLM imports or references."""
    forbidden = [
        "openai",
        "langchain",
        "anthropic",
        "llama",
        "transformers",
        "chatgpt",
        "gpt-4",
        "claude",
        "mistral",
    ]
    for fname in _STAGE7_FILES:
        content = _read_src(fname).lower()
        for word in forbidden:
            assert word not in content, f"Forbidden LLM usage '{word}' found in {fname}"


def test_stage7_no_stage8_financial_attribution_leakage():
    """Static assertion: Stage 7 contains no protected_gmv or counterfactual attribution logic."""
    forbidden_stage8 = [
        "protected_gmv",
        "recovered_gmv",
        "counterfactual",
        "uplift_estimation",
        "propensity_score",
    ]
    for fname in _STAGE7_FILES:
        content = _read_src(fname).lower()
        for word in forbidden_stage8:
            assert word not in content, f"Stage 8 leakage '{word}' found in {fname}"


def test_stage7_no_provider_sdk_in_core_execution():
    """Static assertion: Core execution files do not import external provider SDKs or HTTP clients."""
    core_files = [
        "src/core/domain/execution_models.py",
        "src/core/domain/execution_exceptions.py",
        "src/core/execution/state_machine.py",
        "src/core/execution/executor.py",
        "src/core/execution/execution_service.py",
        "src/core/execution/observation_service.py",
    ]
    forbidden_clients = [
        "razorpay.client",
        "requests.post",
        "requests.get",
        "httpx.post",
        "httpx.get",
        "aiohttp",
    ]
    for fname in core_files:
        content = _read_src(fname).lower()
        for word in forbidden_clients:
            assert word not in content, f"Direct provider SDK/HTTP leakage '{word}' found in {fname}"


def test_stage7_no_upstream_mutation():
    """Static assertion: Stage 7 repository does not update or delete upstream tables."""
    content = _read_src("src/infrastructure/execution_repository.py").lower()
    upstream_tables = [
        "payment_events",
        "payment_health_snapshots",
        "degradation_signals",
        "degradation_episodes",
        "rca_evaluations",
        "failure_predictions",
        "intervention_decisions",
    ]
    for table in upstream_tables:
        assert f"update {table}" not in content, f"Forbidden UPDATE on upstream table {table}"
        assert f"delete from {table}" not in content, f"Forbidden DELETE on upstream table {table}"
