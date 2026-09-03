import pytest
from src.core.ml.model import DeterministicBaselineModel

def test_severity_levels():
    model = DeterministicBaselineModel()
    
    # Base probability is 0.05
    base_prob = model.predict({})
    assert base_prob == 0.05
    
    # MODERATE severity (+0.05)
    prob_mod = model.predict({"is_in_active_degradation": True, "degradation_severity": "MODERATE"})
    assert prob_mod == pytest.approx(0.10)
    
    # HIGH severity (+0.15)
    prob_high = model.predict({"is_in_active_degradation": True, "degradation_severity": "HIGH"})
    assert prob_high == pytest.approx(0.20)
    
    # CRITICAL severity (+0.30)
    prob_crit = model.predict({"is_in_active_degradation": True, "degradation_severity": "CRITICAL"})
    assert prob_crit == pytest.approx(0.35)

def test_monotonic_severity():
    model = DeterministicBaselineModel()
    
    prob_mod = model.predict({"is_in_active_degradation": True, "degradation_severity": "MODERATE"})
    prob_high = model.predict({"is_in_active_degradation": True, "degradation_severity": "HIGH"})
    prob_crit = model.predict({"is_in_active_degradation": True, "degradation_severity": "CRITICAL"})
    
    # CRITICAL > HIGH > MODERATE
    assert prob_crit > prob_high
    assert prob_high > prob_mod

def test_probability_bounds():
    model = DeterministicBaselineModel()
    
    # Max out all bonuses
    prob_max = model.predict({
        "global_30m_failure_rate": 1.0, 
        "is_in_active_degradation": True, 
        "degradation_severity": "CRITICAL",
        "rca_candidate_matches_payment_segment": True,
        "rca_evidence_strength": "STRONG"
    })
    
    # Expected: 
    # prob = 0.05
    # global shift: 0.5 * 0.05 + 0.5 * 1.0 = 0.525
    # CRITICAL: +0.30 = 0.825
    # STRONG: +0.2 = 1.025
    # Bounded to 1.0
    assert prob_max == 1.0
    
    # Negative inputs should bound to 0.0
    prob_min = model.predict({
        "global_30m_failure_rate": -1.0
    })
    assert prob_min == 0.0

def test_no_future_data_leakage_and_deterministic():
    model = DeterministicBaselineModel()
    
    # Verify we only use explicitly passed features in the feature_vector 
    # without relying on side-effects or external state
    features = {
        "is_in_active_degradation": True, 
        "degradation_severity": "CRITICAL"
    }
    
    prob_1 = model.predict(features)
    prob_2 = model.predict(features)
    
    # Must be deterministic and identical
    assert prob_1 == prob_2

def test_identical_feature_vectors_with_different_severities():
    model = DeterministicBaselineModel()
    
    base_features = {
        "global_30m_failure_rate": 0.50,
        "is_in_active_degradation": True,
        "rca_candidate_matches_payment_segment": True,
        "rca_evidence_strength": "MODERATE"
    }
    
    features_mod = {**base_features, "degradation_severity": "MODERATE"}
    features_high = {**base_features, "degradation_severity": "HIGH"}
    features_crit = {**base_features, "degradation_severity": "CRITICAL"}
    
    prob_mod = model.predict(features_mod)
    prob_high = model.predict(features_high)
    prob_crit = model.predict(features_crit)
    
    # Baseline for these features:
    # 0.5 * 0.05 + 0.5 * 0.50 = 0.275
    # RCA MODERATE = +0.10
    # Base = 0.375
    # MODERATE (+0.05) -> 0.425
    # HIGH (+0.15) -> 0.525
    # CRITICAL (+0.30) -> 0.675
    
    assert prob_mod == pytest.approx(0.425)
    assert prob_high == pytest.approx(0.525)
    assert prob_crit == pytest.approx(0.675)
    
    assert round(prob_crit - prob_high, 5) == 0.15
    assert round(prob_high - prob_mod, 5) == 0.10
