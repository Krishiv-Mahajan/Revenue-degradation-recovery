"""
Stage 8 boundary assertions.

Ensures strict stage isolation:
- No LLM imports or references
- No mutation of upstream records
- Numeric typing rules: integer minor-unit money and Decimal fixed-point confidence/probabilities
- Terminal stage (no Stage 9+ leakage)
"""
import os

_STAGE8_FILES = [
    "src/core/domain/attribution_models.py",
    "src/core/domain/attribution_exceptions.py",
    "src/core/attribution/calculator.py",
    "src/core/attribution/attribution_service.py",
    "src/infrastructure/attribution_repository.py",
]


def _read_src(filename: str) -> str:
    project_root = os.path.join(os.path.dirname(__file__), "../..")
    path = os.path.join(project_root, filename)
    with open(path, "r") as f:
        return f.read()


def test_stage8_no_llm_imports():
    """Static assertion: Stage 8 source contains no LLM imports or references."""
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
    for fname in _STAGE8_FILES:
        content = _read_src(fname).lower()
        for word in forbidden:
            assert word not in content, f"Forbidden LLM usage '{word}' found in {fname}"


def test_stage8_no_upstream_mutation():
    """Static assertion: Stage 8 repository does not update or delete upstream tables."""
    content = _read_src("src/infrastructure/attribution_repository.py").lower()
    upstream_tables = [
        "payment_events",
        "payment_health_snapshots",
        "degradation_signals",
        "degradation_episodes",
        "rca_evaluations",
        "failure_predictions",
        "intervention_decisions",
        "intervention_commands",
        "intervention_execution_attempts",
        "payment_outcome_observations",
    ]
    for table in upstream_tables:
        assert f"update {table}" not in content, f"Forbidden UPDATE on upstream table {table}"
        assert f"delete from {table}" not in content, f"Forbidden DELETE on upstream table {table}"


def test_stage8_numeric_typing_rules():
    """Static assertion: Money is integer minor units, probabilities/confidence are Decimal."""
    calc_content = _read_src("src/core/attribution/calculator.py")
    assert "Decimal" in calc_content
    assert "ROUND_HALF_EVEN" in calc_content

    models_content = _read_src("src/infrastructure/models.py")
    assert "counterfactual_failure_probability = Column(Numeric(6, 4)" in models_content
    assert "attribution_confidence = Column(Numeric(6, 4)" in models_content
    assert "payment_amount_minor_units = Column(Integer" in models_content
    assert "attributed_protected_gmv_minor_units = Column(Integer" in models_content


def test_stage8_terminal_boundary_no_stage9():
    """Static assertion: Stage 8 is the terminal stage of the system."""
    for fname in _STAGE8_FILES:
        content = _read_src(fname).lower()
        assert "stage9" not in content
        assert "stage_9" not in content
