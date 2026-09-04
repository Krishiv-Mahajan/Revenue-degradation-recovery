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


# ---------------------------------------------------------------------------
# Tests for untrained-heuristic-v2: 24h fallback (Policy B)
# ---------------------------------------------------------------------------

def test_24h_fallback_when_30m_null():
    """
    When global_30m_failure_rate is None and global_24h_failure_rate is available,
    the 24h rate participates in the formula with the same 0.5 coefficient.

    Expected:
      prob = 0.5 * 0.05 + 0.5 * 0.8 = 0.025 + 0.4 = 0.425
    """
    model = DeterministicBaselineModel()
    prob = model.predict({
        "global_30m_failure_rate": None,
        "global_24h_failure_rate": 0.8,
    })
    assert prob == pytest.approx(0.425)


def test_30m_preferred_over_24h():
    """
    When global_30m_failure_rate is available (non-null), the 24h value must have
    absolutely no effect on the output.

    With 30m=0.2, 24h=0.9:
      Expected: 0.5 * 0.05 + 0.5 * 0.2 = 0.025 + 0.10 = 0.125

    The 0.9 value must not be used.
    """
    model = DeterministicBaselineModel()
    prob = model.predict({
        "global_30m_failure_rate": 0.2,
        "global_24h_failure_rate": 0.9,
    })
    assert prob == pytest.approx(0.125)

    # Cross-check: same 30m without 24h key must give identical result
    prob_no_24h = model.predict({"global_30m_failure_rate": 0.2})
    assert prob == pytest.approx(prob_no_24h)


def test_both_rates_null():
    """
    When both global_30m_failure_rate and global_24h_failure_rate are None,
    no behavioral adjustment is applied — base is 0.05, unchanged from v1 behavior.
    """
    model = DeterministicBaselineModel()
    prob = model.predict({
        "global_30m_failure_rate": None,
        "global_24h_failure_rate": None,
    })
    assert prob == pytest.approx(0.05)

    # Same result when neither key is present
    prob_absent = model.predict({})
    assert prob == pytest.approx(prob_absent)


def test_24h_fallback_with_critical_strong_rca():
    """
    Full live-dataset scenario (pay_TXuodB2vKy4bhW, 2026-09-04):
      global_30m_failure_rate = None  (6 events < 50 threshold)
      global_24h_failure_rate = 0.7969
      severity               = CRITICAL  (+0.30)
      rca_match              = True, STRONG  (+0.20)

    Expected:
      base  = 0.5 * 0.05 + 0.5 * 0.7969 = 0.025 + 0.39845 = 0.42345
      +CRIT = 0.42345 + 0.30              = 0.72345
      +RCA  = 0.72345 + 0.20              = 0.92345
    """
    model = DeterministicBaselineModel()
    prob = model.predict({
        "global_30m_failure_rate": None,
        "global_24h_failure_rate": 0.7969,
        "is_in_active_degradation": True,
        "degradation_severity": "CRITICAL",
        "rca_candidate_matches_payment_segment": True,
        "rca_evidence_strength": "STRONG",
    })
    assert prob == pytest.approx(0.92345, abs=1e-5)


def test_probability_bounds_with_24h_fallback():
    """
    When global_30m_failure_rate is None and global_24h_failure_rate=1.0,
    with all bonuses applied, the result must be clamped to exactly 1.0.

    Unclamped: 0.5*0.05 + 0.5*1.0 + 0.30 + 0.20 = 0.025 + 0.5 + 0.5 = 1.025
    Clamped: 1.0
    """
    model = DeterministicBaselineModel()
    prob = model.predict({
        "global_30m_failure_rate": None,
        "global_24h_failure_rate": 1.0,
        "is_in_active_degradation": True,
        "degradation_severity": "CRITICAL",
        "rca_candidate_matches_payment_segment": True,
        "rca_evidence_strength": "STRONG",
    })
    assert prob == 1.0
