"""
Stage 6 boundary assertions.

Ensures strict stage isolation:
- No LLM imports or calls
- No Stage 7 execution or provider dispatch
- No Stage 8 protected GMV, recovered GMV, or counterfactual attribution
- No mutation of upstream records
"""
import os

_STAGE6_FILES = [
    "src/core/domain/intervention_models.py",
    "src/core/intervention/policy.py",
    "src/core/intervention/gates.py",
    "src/core/intervention/calculator.py",
    "src/core/intervention/service.py",
    "src/infrastructure/intervention_repository.py",
]


def _read_src(filename: str) -> str:
    project_root = os.path.join(os.path.dirname(__file__), "../..")
    path = os.path.join(project_root, filename)
    with open(path, "r") as f:
        return f.read()


def test_stage6_no_llm_imports():
    """Static assertion: Stage 6 source contains no LLM imports or references."""
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
    for fname in _STAGE6_FILES:
        content = _read_src(fname).lower()
        for word in forbidden:
            assert word not in content, f"Forbidden LLM usage '{word}' found in {fname}"


def test_stage6_no_stage7_execution_leakage():
    """Static assertion: Stage 6 does not implement intervention execution or provider API calls."""
    forbidden_stage7 = [
        "razorpay.client",
        "dispatch_intervention",
        "execute_intervention",
        "requests.post",
        "requests.get",
        "httpx.post",
        "httpx.get",
        "aiohttp",
        "refund_payment",
        "capture_payment",
        "payment_capture",
    ]
    for fname in _STAGE6_FILES:
        content = _read_src(fname).lower()
        for word in forbidden_stage7:
            assert word not in content, f"Stage 7 execution leakage '{word}' found in {fname}"


def test_stage6_no_stage8_financial_attribution_leakage():
    """Static assertion: Stage 6 contains no protected_gmv or counterfactual attribution logic."""
    forbidden_stage8 = [
        "protected_gmv",
        "recovered_gmv",
        "counterfactual",
        "uplift_estimation",
    ]
    for fname in _STAGE6_FILES:
        content = _read_src(fname).lower()
        for word in forbidden_stage8:
            assert word not in content, f"Stage 8 leakage '{word}' found in {fname}"
