from typing import Optional
from src.core.domain.degradation_models import (
    DegradationSignal,
    Severity,
    SEVERITY_HIGH_ABSOLUTE_DROP,
    SEVERITY_HIGH_RELATIVE_DROP,
    SEVERITY_CRITICAL_ABSOLUTE_DROP,
    SEVERITY_CRITICAL_RELATIVE_DROP,
    MIN_BASELINE_RATE_FOR_RELATIVE_TEST
)

def evaluate_severity(signal: DegradationSignal, baseline_success_rate: float) -> Severity:
    if signal.absolute_drop is None or signal.relative_drop is None:
        return Severity.MODERATE

    abs_drop = signal.absolute_drop
    rel_drop = signal.relative_drop

    is_critical = abs_drop >= SEVERITY_CRITICAL_ABSOLUTE_DROP or (
        baseline_success_rate >= MIN_BASELINE_RATE_FOR_RELATIVE_TEST and rel_drop >= SEVERITY_CRITICAL_RELATIVE_DROP
    )
    
    if is_critical:
        return Severity.CRITICAL
        
    is_high = abs_drop >= SEVERITY_HIGH_ABSOLUTE_DROP or (
        baseline_success_rate >= MIN_BASELINE_RATE_FOR_RELATIVE_TEST and rel_drop >= SEVERITY_HIGH_RELATIVE_DROP
    )
    
    if is_high:
        return Severity.HIGH
        
    return Severity.MODERATE

def escalate_severity(current: Severity, proposed: Severity) -> Severity:
    order = {Severity.MODERATE: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}
    if order[proposed] > order[current]:
        return proposed
    return current
