import pytest
import os
import glob

def test_no_llm_imports():
    """Ensure no LLM libraries are used anywhere in the source."""
    forbidden = ["openai", "langchain", "anthropic", "llama", "transformers"]
    src_dir = os.path.join(os.path.dirname(__file__), "../../src")

    for root, _, files in os.walk(src_dir):
        for file in files:
            if file.endswith(".py"):
                with open(os.path.join(root, file), 'r') as f:
                    content = f.read().lower()
                    for word in forbidden:
                        assert word not in content, (
                            f"Found forbidden LLM import/usage '{word}' in {file}"
                        )

def test_no_stage2_plus_fields():
    """Ensure no speculative fields for Stage 2+ exist in domain models."""
    forbidden_fields = [
        "health_score",
        "baseline",
        "degradation",
        "anomaly",
        "root_cause",
        "failure_probability",
        "revenue_at_risk",
        "expected_saved_gmv",
        "intervention",
        "protected_gmv",
        "counterfactual"
    ]

    models_file = os.path.join(os.path.dirname(__file__), "../../src/core/domain/models.py")
    with open(models_file, 'r') as f:
        content = f.read().lower()
        for field in forbidden_fields:
            assert field not in content, (
                f"Found forbidden Stage 2+ field '{field}' in models.py"
            )


# ---------------------------------------------------------------------------
# Stage 4 boundary assertions (§31 of design)
# ---------------------------------------------------------------------------

# Stage 4 source files to audit
_STAGE4_FILES = [
    "src/core/domain/rca_models.py",
    "src/core/rca/calculator.py",
    "src/core/rca/service.py",
    "src/infrastructure/rca_repository.py",
]


def _read_stage4_src(filename: str) -> str:
    project_root = os.path.join(os.path.dirname(__file__), "../..")
    path = os.path.join(project_root, filename)
    with open(path, "r") as f:
        return f.read()


def test_stage4_no_llm_api_calls():
    """Static assertion: Stage 4 source contains no LLM API calls."""
    forbidden = ["openai", "langchain", "anthropic", "llama", "transformers",
                 "chatgpt", "gpt-4", "claude", "mistral"]
    for fname in _STAGE4_FILES:
        content = _read_stage4_src(fname).lower()
        for word in forbidden:
            assert word not in content, (
                f"Found forbidden LLM call '{word}' in Stage 4 file: {fname}"
            )


def test_stage4_no_revenue_at_risk():
    """Static assertion: Stage 4 source contains no revenue_at_risk identifier."""
    for fname in _STAGE4_FILES:
        content = _read_stage4_src(fname)
        assert "revenue_at_risk" not in content, (
            f"Forbidden identifier 'revenue_at_risk' found in Stage 4 file: {fname}"
        )


def test_stage4_no_recoverable_gmv():
    """Static assertion: Stage 4 source contains no recoverable_gmv identifier."""
    for fname in _STAGE4_FILES:
        content = _read_stage4_src(fname)
        assert "recoverable_gmv" not in content, (
            f"Forbidden identifier 'recoverable_gmv' found in Stage 4 file: {fname}"
        )


def test_stage4_no_protected_gmv():
    """Static assertion: Stage 4 source contains no protected_gmv identifier."""
    for fname in _STAGE4_FILES:
        content = _read_stage4_src(fname)
        assert "protected_gmv" not in content, (
            f"Forbidden identifier 'protected_gmv' found in Stage 4 file: {fname}"
        )


def test_stage4_no_failure_probability():
    """Static assertion: Stage 4 source contains no failure_probability identifier."""
    for fname in _STAGE4_FILES:
        content = _read_stage4_src(fname)
        assert "failure_probability" not in content, (
            f"Forbidden identifier 'failure_probability' found in Stage 4 file: {fname}"
        )


def test_stage4_no_stage5_implementation():
    """Stage 4 source must not implement Stage 5 prediction or intervention logic."""
    forbidden_stage5 = ["predict_", "intervention", "counterfactual", "recommended_action"]
    for fname in _STAGE4_FILES:
        content = _read_stage4_src(fname).lower()
        for word in forbidden_stage5:
            assert word not in content, (
                f"Found Stage 5 leakage '{word}' in Stage 4 file: {fname}"
            )

# ---------------------------------------------------------------------------
# Stage 5 boundary assertions
# ---------------------------------------------------------------------------

_STAGE5_FILES = [
    "src/core/domain/failure_prediction_models.py",
    "src/infrastructure/failure_prediction_repository.py",
    "src/core/services/failure_prediction_service.py",
]

def test_stage5_no_llm_api_calls():
    """Static assertion: Stage 5 source contains no LLM API calls."""
    forbidden = ["openai", "langchain", "anthropic", "llama", "transformers",
                 "chatgpt", "gpt-4", "claude", "mistral"]
    for fname in _STAGE5_FILES:
        content = _read_stage4_src(fname).lower()
        for word in forbidden:
            assert word not in content, (
                f"Found forbidden LLM call '{word}' in Stage 5 file: {fname}"
            )

def test_stage5_no_stage6_implementation():
    """Stage 5 source must not implement Stage 6 intervention logic."""
    forbidden_stage6 = [
        "revenue_at_risk",
        "expected_saved_gmv",
        "intervention",
        "protected_gmv",
        "counterfactual",
        "recommended_action",
    ]
    for fname in _STAGE5_FILES:
        content = _read_stage4_src(fname).lower()
        for word in forbidden_stage6:
            assert word not in content, (
                f"Found Stage 6 leakage '{word}' in Stage 5 file: {fname}"
            )
