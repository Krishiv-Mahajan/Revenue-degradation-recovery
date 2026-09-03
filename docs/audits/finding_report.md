# EXPERIMENT/AUDIT FINDINGS: Stage 6 ACT Feasibility

After a mathematical and empirical audit of the frozen Stage 1–8 engine (`224dc4a`), I have determined that reaching the Stage 6 `ACT` threshold is mathematically impossible under legitimate synthetic conditions.

### Final Determination: **B (Not Achievable)**

An honest, legitimate synthetic scenario cannot naturally trigger an `ACT` intervention decision without monkey-patching or violating the current implementation.

### Mathematical Proof & Rationale

The failure to reach the `act_risk_threshold = 0.70` stems from three interacting logical boundaries within the frozen system:

#### 1. The `CRITICAL` Severity Blindspot in the ML Model
The `DeterministicBaselineModel` expects severity values of `"HIGH"`, `"MEDIUM"`, or `"LOW"`. However, Stage 3 generates domain severity values of `MODERATE`, `HIGH`, or `CRITICAL`.
- If an incident causes an absolute drop $\ge 0.30$, Stage 3 correctly assigns `CRITICAL` severity.
- Because `model.py` lacks a branch for `"CRITICAL"`, it falls through and adds **$+0.0$** to the failure probability (whereas `"HIGH"` adds $+0.30$).
- **Consequence:** To maximize the model's probability score, the incident's failure rate must be artificially constrained so the global absolute drop remains strictly $< 0.30$, forcing a `HIGH` severity. This caps the `global_30m_failure_rate` contribution.

#### 2. The Probability Ceiling
With the severity constrained to `HIGH` (drop $< 0.30$), assuming a historical baseline failure rate of $0.15$, the maximum allowable global failure rate is roughly $\sim 0.44$.
The model computes:
`prob = 0.5 * 0.05 + 0.5 * global_30m_failure_rate + severity_bonus + rca_bonus`
`prob = 0.025 + (0.5 * 0.44) + 0.30 + rca_bonus = 0.545 + rca_bonus`

To cross the `0.70` threshold, the model **must** receive `STRONG` RCA evidence (which grants a $+0.20$ bonus). `MODERATE` RCA is not evaluated by the model.

#### 3. The RCA Contribution Denominator Flaw
To achieve `STRONG` evidence, a candidate must have an `excess_failure_contribution >= 0.70`.
However, `rca/calculator.py` computes the episode's total excess failures by summing the positive excess values across **all evaluated structural dimensions**:
```python
def compute_episode_total_excess_failures(excess_values: List[float]) -> float:
    return sum(v for v in excess_values if v > 0)
```
Because a single payment has multiple structural dimensions (e.g., `BANK=HDFC`, `PAYMENT_METHOD=UPI`, `CURRENCY=INR`), a massive failure in `HDFC` will naturally cause excess failures in the overlapping `UPI` and `INR` segments.
If all 3 dimensions have historical baselines, the calculator sums the excess failures across all three, effectively triple-counting the incident's impact. The `HDFC` candidate's contribution is then calculated as:
`HDFC_excess / (HDFC_excess + UPI_excess + INR_excess) ≈ 33%`
- **Consequence:** In a production environment with populated baselines, overlapping dimensions mathematically dilute any single candidate's contribution to $1/N$. It is inherently impossible for a single dimension to reach the $0.70$ (`STRONG`) threshold.
- The evidence degrades to `MODERATE` or `WEAK`, granting a $+0.0$ bonus in the ML model.

### Conclusion
Because RCA evidence cannot reach `STRONG` (due to denominator dilution), and the severity cannot reach `CRITICAL` without penalizing the score to $+0.0$, the mathematical ceiling of the `DeterministicBaselineModel` is permanently capped at roughly **`0.545`**, strictly below the `0.70` `ACT` threshold.

Therefore, an end-to-end demonstration of Stage 6 `ACT` $\rightarrow$ Stage 7 Execution $\rightarrow$ Stage 8 Protected GMV cannot be achieved under the frozen codebase using legitimate inputs alone.
