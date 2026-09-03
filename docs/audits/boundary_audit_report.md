# Stage 4 → Stage 5 Boundary Audit

## 1. Boundary Data Flow
- **Stage 4 Output**: `RCAEvaluation` containing a `classification` (e.g., `SEGMENT_SPECIFIC`), and related `CandidateCause` rows indicating the structural dimension, value, and `evidence_strength` (`STRONG`/`MODERATE`/`WEAK`).
- **Stage 5 Input**: `FeatureReconstructionService.reconstruct_features()` reads the Active Episode context. If an active episode exists, it retrieves the highest-ranked RCA candidate cause via `FeatureReconstructionRepository.get_rca_context()`.
- **RCA Matching**: The service evaluates whether the prediction target (`PaymentEvent`) belongs to the RCA candidate's segment (e.g. `BANK=HDFC`).
- **Model Consumption**: The `DeterministicBaselineModel` consumes `rca_candidate_matches_payment_segment` and `rca_evidence_strength` to deterministically boost failure probability.

## 2. Temporal Information-Barrier Proof
In `FeatureReconstructionRepository.get_rca_context(episode_id, t)`, the SQL explicitly enforces the temporal boundary using the prediction timestamp `t`:
```python
        eval_stmt = (
            select(RCAEvaluationModel)
            .where(
                and_(
                    RCAEvaluationModel.episode_id == episode_id,
                    RCAEvaluationModel.generated_at < t  # STRICT TEMPORAL BOUNDARY
                )
            )
            # ...
        )
```
**Proof**: The query mathematically prevents any RCA data generated at or after `T` from leaking into the feature vector. The information barrier is intact.

## 3. RCA Version-Selection Proof
Multiple evaluations can exist for the same episode. `FeatureReconstructionRepository.get_rca_context()` handles this:
```python
            .order_by(RCAEvaluationModel.evaluation_version.desc())
            .limit(1)
```
**Proof**: Since it sorts by `evaluation_version` descending and takes the top 1 strictly prior to `T`, it deterministically resolves to the most recent version available *at that exact moment in time*. 

## 4. Severity/RCA Semantic Mapping
Stage 5's `DeterministicBaselineModel` parses RCA semantics accurately:
- **No RCA (or systemic)**: `rca_candidate_matches_payment_segment = False` → +0.0 probability.
- **WEAK RCA**: Modifies nothing explicitly, yielding +0.0, correctly preventing mild correlative hints from distorting predictions.
- **MODERATE RCA**: Boosts probability by +0.10.
- **STRONG RCA**: Boosts probability by +0.20.

## 5. Provenance/Status Handling
Stage 5 performs stateless, point-in-time point-lookups. It does not overwrite Stage 4 output. Model predictions are logged to `prediction_events` with their own independent `schema_version`. RCA tracking remains read-only to Stage 5.

## 6. Concrete Counterexamples
There are two critical semantic defects where Stage 4 correctly produces RCA, but Stage 5 fails to consume it.

**Counterexample 1: Missing Episode Dimensions**
- Stage 3 detects a `CRITICAL` drop on `BANK=HDFC`.
- Stage 4 RCA confirms `BANK=HDFC` as `STRONG` root cause.
- A new payment arrives at time `T` with `bank="HDFC"`.
- `FeatureReconstructionService._get_most_severe_active_episode` checks `GLOBAL`, `CURRENCY`, and `PAYMENT_METHOD` but **completely omits checking `BANK` or `WALLET`**.
- Result: The event is treated as `is_in_active_degradation=False`. The Stage 4 RCA is never queried. The model produces a baseline 5% probability instead of a highly elevated risk.

**Counterexample 2: Missing Dimension Matching**
- Stage 3 detects a `GLOBAL` episode.
- Stage 4 RCA identifies `CURRENCY=INR` as the `STRONG` root cause.
- Payment arrives with `currency="INR"`.
- The episode is correctly retrieved (as `GLOBAL` is checked).
- `FeatureReconstructionService._matches_segment` checks `BANK`, `WALLET`, and `PAYMENT_METHOD`, but **completely omits checking `CURRENCY`**.
- Result: `rca_candidate_matches_payment_segment` evaluates to `False`. The RCA probability boost is dropped.

## 7. Does a real defect exist?
**Yes.** The mapping layer in `FeatureReconstructionService` is structurally incomplete. It silently ignores `BANK` and `WALLET` when reconstructing degradation context, and ignores `CURRENCY` when matching RCA candidates to the event.

## 8. Smallest Correction
1. In `_get_most_severe_active_episode`: Add checks for `BANK` and `WALLET`. To truly respect "most severe", it should ideally evaluate all applicable dimensions (`GLOBAL`, `CURRENCY`, `PAYMENT_METHOD`, `BANK`, `WALLET`) and return the one with the highest severity (CRITICAL > HIGH > MODERATE), or at least definitively check all dimensions in a fallback chain.
2. In `_matches_segment`: Add `if dimension == 'CURRENCY' and event.currency == value: return True`.

## 9. Exact Files Requiring Modification
- `src/core/services/feature_reconstruction_service.py`

## 10. Required Tests
Integration tests verifying Stage 5 boundary alignment:
- A test where `BANK` is the active episode dimension, proving `is_in_active_degradation` goes to `True` and the model gets the severity boost.
- A test where a `GLOBAL` episode points to `CURRENCY` as the RCA candidate, proving `rca_candidate_matches_payment_segment` evaluates to `True`.
