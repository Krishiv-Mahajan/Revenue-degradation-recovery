# PHASE 1: STAGE 5 SEVERITY CONTRACT FIX REPORT

### A. Exact Defect
The `DeterministicBaselineModel` explicitly handled `"HIGH"`, `"MEDIUM"`, and `"LOW"` string literals for `degradation_severity`, but the canonical vocabulary produced by Stage 3 (`degradation_models.py`) consists of `MODERATE`, `HIGH`, and `CRITICAL`. This caused `CRITICAL` severity incidents to silently fall through the `if` branches in the model, receiving a $+0.0$ probability bonus instead of the intended maximal bonus, mathematically capping the prediction ceiling.

### B. Semantic Rationale for Chosen Severity Contributions
To maintain monotonicity (`CRITICAL >= HIGH >= MODERATE`), reflect the severity of the drop thresholds from Stage 3, and preserve backward compatibility:
- **`CRITICAL` ($+0.45$)**: Triggered by an absolute drop $\ge 0.30$. Assigned the maximal bonus.
- **`HIGH` ($+0.30$)**: Unchanged. Triggered by a drop $\ge 0.20$.
- **`MODERATE` ($+0.15$)**: Replaces `MEDIUM` to match Stage 3's vocabulary ($\ge 0.10$ drop). The `MEDIUM` literal is retained internally to prevent legacy testing regressions.
- **`LOW` ($+0.05$)**: Retained.

### C. Exact Files Changed
- `src/core/ml/model.py` (updated `DeterministicBaselineModel.predict()`)
- `tests/unit/test_deterministic_model.py` (new)

### D. Tests Added/Changed
I created a dedicated, focused test suite in `tests/unit/test_deterministic_model.py` that proves:
- `MODERATE`, `HIGH`, and `CRITICAL` are evaluated explicitly.
- The mapping is monotonic: `CRITICAL >= HIGH >= MODERATE`.
- Out-of-bounds probability is correctly clipped at `1.0`.
- Legacy inputs (`MEDIUM`, `LOW`) are supported safely.
- Probabilities are deterministic and do not leak future state.

### E. Before/After Probability Behavior
- **Before:** A `CRITICAL` severity incident with a `0.50` global failure rate and `MODERATE` RCA evidence yielded a probability of **`~0.375`** (because `CRITICAL` gave $+0.0$).
- **After:** The identical `CRITICAL` incident now correctly receives the maximal bonus, yielding a probability of **`~0.825`**. 

### F. New Theoretical Probability Ceiling/Range
Assuming a base global rate of $0.15$:
Max legitimate input for `global_30m_failure_rate` is $1.0$.
Absolute drop = $1.0 - 0.15 = 0.85$ (CRITICAL).
Because Stage 4 RCA is still untouched (sufferring from denominator dilution), the max evidence strength is still `MODERATE` ($+0.10$).
Maximum probability = $0.025 (\text{base}) + 0.50 (\text{global rate}) + 0.45 (\text{CRITICAL}) + 0.10 (\text{MODERATE}) = 1.075$.
The model actively clips this output, meaning the **new theoretical probability ceiling is bounded to exactly `1.000`**.

### G. Whether Legitimate ACT is now Mathematically Possible
**Yes.** 
Even with Stage 4 RCA handicapped at `MODERATE`, a synthetic incident causing the global failure rate to reach $0.45$ (an absolute drop of $0.30$) produces `CRITICAL` severity.
Probability = $0.025 + (0.5 * 0.45) + 0.45 (\text{CRITICAL}) + 0.10 (\text{MODERATE}) = 0.800$.
Because $0.800 \ge 0.70$, `ACT` is now organically and mathematically achievable using legitimate bounds.

### H. Full Regression Result
```text
======================= 214 passed, 2 warnings in 6.64s ========================
```
- All 210 original tests pass.
- All 4 new Stage 5 invariant tests pass.

### I. Git Diff
```diff
diff --git a/src/core/ml/model.py b/src/core/ml/model.py
--- a/src/core/ml/model.py
+++ b/src/core/ml/model.py
@@ -51,9 +51,11 @@ class DeterministicBaselineModel(FailurePredictionModel):
         # Strongly adjust if in active degradation
         if feature_vector.get("is_in_active_degradation") is True:
             severity = feature_vector.get("degradation_severity")
-            if severity == "HIGH":
-                prob += 0.3
-            elif severity == "MEDIUM":
+            if severity == "CRITICAL":
+                prob += 0.45
+            elif severity == "HIGH":
+                prob += 0.30
+            elif severity in ("MODERATE", "MEDIUM"):
                 prob += 0.15
             elif severity == "LOW":
                 prob += 0.05
```
