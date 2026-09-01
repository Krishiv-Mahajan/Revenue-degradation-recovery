import pytest
import os
import glob

def test_no_llm_imports():
    """Ensure no LLM libraries are used."""
    forbidden = ["openai", "langchain", "anthropic", "llama", "transformers"]
    src_dir = os.path.join(os.path.dirname(__file__), "../../src")
    
    for root, _, files in os.walk(src_dir):
        for file in files:
            if file.endswith(".py"):
                with open(os.path.join(root, file), 'r') as f:
                    content = f.read().lower()
                    for word in forbidden:
                        assert word not in content, f"Found forbidden LLM import/usage '{word}' in {file}"

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
            assert field not in content, f"Found forbidden Stage 2+ field '{field}' in models.py"
