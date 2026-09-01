# Stage 4 — Root Cause Analysis Design

> [!IMPORTANT]
> This is a design/review artifact. **Do not begin implementation** until this document is formally approved and authorized by the user.

---

## 1. Stage 3 → Stage 4 Interface

**Inputs from Stage 3:**  
Stage 4 consumes `DegradationEpisode` records produced by Stage 3. Confirmed against the actual `DegradationEpisodeModel` and `DegradationEpisode` dataclass in the repository. For a given logical episode, Stage 4 extracts:

- `episode_id` — UUID5 derived from `segment_dimension:segment_value:started_at_window.isoformat()`
- `segment_dimension` and `segment_value` — the primary degraded segment (e.g., `payment_method:upi`)
- `started_at_window` — UTC datetime of the first BAD signal
- `ended_at_window` — UTC datetime of the last BAD window if `RECOVERED`, else `None` if `ACTIVE`
- `status` — `ACTIVE | RECOVERED | INVALIDATED`

Stage 4 does **NOT** reconstruct episode success rates or second-guess the Stage 3 degradation verdict. It accepts the factual episode definition and investigates its composition.

---

## 2. RCA Responsibilities

Stage 4 answers: **"Why did this payment segment degrade?"**  
It is exclusively responsible for:

- Constructing an exact historical baseline for the episode's analysis window.
- Aggregating `payment_events` across candidate dimensions within the episode window.
- Calculating deterministic, per-segment-value deviations between episode behavior and historical baseline behavior.
- Identifying and ranking evidence-backed Candidate Causes based on excess failure contribution.
- Categorizing Evidence Strength deterministically (`STRONG`, `MODERATE`, `WEAK`).
- Persisting an immutable, versioned RCA evaluation containing a full mathematical audit trail.

---

## 3. RCA Non-Responsibilities

Stage 4 **MUST NOT**:

- Predict future payment failures.
- Calculate failure probabilities.
- Make or recommend intervention decisions.
- Calculate `revenue_at_risk`, `recoverable_gmv`, or `protected_gmv`.
- Implement an LLM as a reasoning engine or source of truth.
- Invent, speculate, or hallucinate root causes without direct factual evidence present in Stage 1 data.

---

## 4. Evidence Sources

Stage 4 relies purely on factual observations from earlier stages:

- **Stage 2 `payment_health_snapshots`:** Used for broad structural dimension baselines where snapshots already exist (dimensions: `GLOBAL`, `currency`, `payment_method`, `bank`, `wallet`, `error_source`). These are the same dimensions snapshotted by Stage 2's `calculator.py`.
- **Stage 1 `payment_events`:** Used for high-cardinality dimensions not covered by Stage 2 snapshots (`error_code`, `error_step`), restricted strictly to the episode analysis windows and historical comparison windows.

No external knowledge bases, inferred fields, or LLM-generated context are used.

---

## 5. Event-Type Semantics

RCA operates over terminal payment lifecycle events, aligning exactly with Stage 2 semantics (confirmed in `calculator.py`):

- **Failure Numerator:** Count of rows where `event_type == 'payment.failed'`
- **Transaction Denominator:** Count of rows where `event_type IN ('payment.captured', 'payment.failed')`
- **Exclusions:** `payment.authorized`, `payment.created`, and all other intermediate events do **not** participate in failure rate calculations.
- Deduplication is handled upstream in Stage 1; RCA queries the deduplicated `payment_events` table.

---

## 6. RCA Analysis Window

The episode analysis window spans exactly:

```
analysis_window_start = episode.started_at_window
analysis_window_end =
    episode.ended_at_window           (if episode.status == RECOVERED)
    OR
    window_start of the most recently evaluated BAD signal + 5 minutes
                                      (if episode.status == ACTIVE)
```

The `+ 5 minutes` accounts for the 5-minute tumbling window convention, ensuring the final BAD window's events are fully included using the half-open `[start, end)` convention from Stage 2.

---

## 7. Historical Comparison Methodology

### 7A. Window Selection

For every 5-minute tumbling window `[t, t+5min)` within the RCA Analysis Window, Stage 4 identifies the matching historical windows:

```
historical_windows = [
    [t - 1 week, t - 1 week + 5min),
    [t - 2 weeks, t - 2 weeks + 5min),
    [t - 3 weeks, t - 3 weeks + 5min),
    [t - 4 weeks, t - 4 weeks + 5min),
]
```

This is the exact same weekday/time-of-day selection used by Stage 2's `get_historical_snapshots` method, confirmed in `analytics_repository.py`.

### 7B. Historical Baseline Aggregation — Volume-Weighted Method

For each candidate dimension/value pair, the historical segment failure rate is computed using **volume-weighted aggregation** across all valid historical windows:

```
historical_segment_failure_rate =
    SUM(historical_segment_failures across all valid historical windows)
    / SUM(historical_segment_transactions across all valid historical windows)
```

**Justification:** Volume-weighting is chosen over the median of per-window rates because:

1. It preserves statistical correctness — a window with 1,000 transactions should influence the baseline more than a window with 10 transactions.
2. It produces the same result regardless of how the windows are segmented, making it fully reproducible.
3. The simple median of per-window rates is sensitive to low-volume outlier windows in ways that can produce misleading baselines.
4. It aligns with how Stage 2's `calculate_baseline` already de-emphasizes low-volume data via the `insufficient_volume` flag.

> [!NOTE]
> Stage 2 uses `statistics.median(rates)` for snapshot-level GLOBAL baselines. Stage 4 intentionally uses volume-weighted aggregation for sub-segment candidates because sub-segment windows will have lower counts, and per-window median of rates would be severely skewed by near-zero volume windows.

### 7C. Historical Window Validity Rules

A historical window is **valid** for inclusion if and only if:

| Condition | Action |
|---|---|
| Snapshot exists in `payment_health_snapshots` (for Stage-2-covered dimensions) | Include if volume meets threshold |
| Snapshot `insufficient_volume == True` | **Exclude** from aggregation |
| Snapshot `transaction_count < MIN_BASELINE_VOLUME` | **Exclude** from aggregation |
| No snapshot exists for a given historical window | **Exclude** that window entirely |
| For error dimensions: query `payment_events` directly; segment transaction count = 0 | **Exclude** that window |

### 7D. Insufficient History Rules

| Valid historical windows found | Action |
|---|---|
| `< 2` valid historical windows | Historical comparison = `INSUFFICIENT` for this dimension/value → candidate **cannot** be evaluated |
| `>= 2` valid historical windows | Proceed with volume-weighted aggregation |

If `SUM(historical_segment_transactions) == 0` (edge case where all historical windows passed validity checks but contributed zero transactions), the baseline is treated as `INSUFFICIENT` to prevent division by zero:

```
if SUM(historical_segment_transactions) == 0:
    historical_segment_failure_rate = INSUFFICIENT
    → candidate cannot be evaluated
```

---

## 8. Candidate Dimension Taxonomy

### 8A. Structural Dimensions (Primary Attribution)

Structural dimensions represent the payment infrastructure path. A structural candidate can independently prove `SEGMENT_SPECIFIC` classification:

| Dimension | Source Field | Notes |
|---|---|---|
| `PAYMENT_METHOD` | `payment_method` | e.g., `'upi'`, `'card'` |
| `BANK` | `bank` | e.g., `'HDFC'` |
| `WALLET` | `wallet` | e.g., `'paytm'` |
| `CURRENCY` | `currency` | e.g., `'INR'` |

Queried via Stage 2 `payment_health_snapshots` where available, with fallback to raw Stage 1 `payment_events`.

### 8B. Supporting Evidence Dimensions (Explanatory Only)

Supporting dimensions explain *how* the degradation manifested but do **not** independently drive `SEGMENT_SPECIFIC` classification:

| Dimension | Source Field | Notes |
|---|---|---|
| `ERROR_CODE` | `error_code` | Structured Razorpay error code |
| `ERROR_SOURCE` | `error_source` | e.g., `'customer'`, `'issuer'`, `'acquirer'` |
| `ERROR_STEP` | `error_step` | e.g., `'payment_authentication'` |

Supporting evidence dimensions are evaluated using error concentration deviation analysis (see §12) rather than excess failure contribution. They are stored in the `evidence_audit_payload` but do **not** create `CandidateCause` rows and do **not** influence `SEGMENT_SPECIFIC` classification.

### 8C. Excluded Fields

`error_description` and `error_reason` are free-form text and are **excluded** as candidate dimensions to prevent cardinality explosion and unstructured noise. They may be included verbatim in the `evidence_audit_payload` for human context.

---

## 9. Evidence Metrics — Per Candidate (Segment-Level Mathematics)

> [!IMPORTANT]
> All expected-failure calculations use the **segment's own transaction count**, not the global episode transaction volume.

For any evaluated structural dimension/value pair (e.g., `PAYMENT_METHOD:upi`):

```
episode_segment_transaction_count  = COUNT(payment.captured + payment.failed)
                                     WHERE dimension_field = value
                                     AND timestamp IN analysis_window

historical_segment_failure_rate    = SUM(hist_segment_failures)
                                     / SUM(hist_segment_transactions)
                                     (computed per §7B, using valid historical windows)

expected_segment_failures          = episode_segment_transaction_count
                                     x historical_segment_failure_rate
                                     (retained as a float; NOT prematurely rounded)

actual_segment_failures            = COUNT(payment.failed)
                                     WHERE dimension_field = value
                                     AND timestamp IN analysis_window

excess_segment_failures            = actual_segment_failures
                                     - expected_segment_failures
                                     (retained as a float; NOT prematurely rounded)
```

Fractional `expected_segment_failures` and `excess_segment_failures` values are **retained as floats** throughout all comparison arithmetic. Rounding (to nearest integer) occurs **only** in the final `evidence_audit_payload` serialization for human readability, and the rounded and unrounded values are both stored.

---

## 10. Traffic vs Failure vs Excess-Failure Semantics

```
traffic_share               = episode_segment_transaction_count
                              / total_episode_transaction_count

failure_share               = actual_segment_failures
                              / total_episode_actual_failures

excess_failure_contribution = excess_segment_failures
                              / episode_total_excess_failures
```

**Crucial Distinction:** A segment with 80% `traffic_share` will naturally have a high `failure_share`. RCA does **not** flag this as a root cause. RCA relies on `excess_failure_contribution` to identify segments whose failure behavior *deviated disproportionately from their own historical baseline*.

---

## 11. Episode-Level Excess Failure Aggregation and Division-by-Zero Guard

### 11A. Definition

```
episode_total_excess_failures =
    SUM(excess_segment_failures)
    for all structural dimension/value pairs evaluated
    WHERE excess_segment_failures > 0
```

Only positive excess failure values are summed. A segment with `excess_segment_failures <= 0` (i.e., performed at or better than its baseline) does not contribute to the episode total.

### 11B. Guard Against Non-Positive Episode Total

Before computing `excess_failure_contribution` for any candidate:

```
IF episode_total_excess_failures <= 0:
    classification = UNKNOWN
    candidate_causes = []
    reason = "No net excess failures detected: observed failures did not exceed
              historical baseline across any structural dimension."
    STOP evaluation, persist UNKNOWN evaluation with empty candidates
```

This prevents:

- Division by zero.
- Negative contribution scores.
- Misleading candidate rankings when the episode is statistically noise or fully explained by reduced traffic.

---

## 12. Error Evidence Analysis (Supporting Dimensions)

Errors are analyzed based on concentration relative to historical behavior, **not raw frequency**.

```
baseline_error_frequency  = COUNT(payment.failed WHERE error_code = X in historical windows)
                            / COUNT(payment.failed in historical windows)

episode_error_frequency   = COUNT(payment.failed WHERE error_code = X in analysis window)
                            / COUNT(payment.failed in analysis window)

error_deviation           = episode_error_frequency - baseline_error_frequency
```

An error code becomes **supporting evidence** (recorded in `evidence_audit_payload`) if:

```
error_deviation >= MIN_ERROR_DEVIATION_THRESHOLD  (e.g., +0.20)
```

Supporting evidence dimensions (`ERROR_CODE`, `ERROR_SOURCE`, `ERROR_STEP`) are stored in the audit payload's `supporting_error_evidence` list but do **not** create `CandidateCause` rows.

---

## 13. Deterministic Candidate-Cause Rules

A structural dimension/value becomes a `CandidateCause` if and only if **all** of the following hold:

1. `actual_segment_failures >= MIN_FAILURES_FOR_RCA_EVALUATION`
2. `excess_segment_failures > 0` (float comparison, before rounding)
3. `excess_failure_contribution >= MIN_CONTRIBUTION_THRESHOLD` (e.g., 0.30)
4. Historical comparison data is **not** `INSUFFICIENT` for this dimension/value
5. `episode_total_excess_failures > 0` (episode-level guard from §11B)

---

## 14. Systemic vs Segment-Specific Classification

| Classification | Condition |
|---|---|
| `SEGMENT_SPECIFIC` | At least one **structural** dimension/value passes all five `CandidateCause` rules (§13). |
| `SYSTEMIC` | `episode_total_excess_failures > 0`, historical baseline is valid, but **no** structural dimension/value passes the `excess_failure_contribution` threshold — i.e., failure degraded broadly and evenly across all banks, payment methods, etc. |
| `UNKNOWN` | Any of: total episode actual failures `< MIN_FAILURES_FOR_RCA_EVALUATION`; historical baseline is `INSUFFICIENT` for the majority of the analysis window; episode status is `INVALIDATED`; `episode_total_excess_failures <= 0`. |

Supporting evidence dimensions (`ERROR_CODE`, `ERROR_SOURCE`, `ERROR_STEP`) **cannot** independently produce `SEGMENT_SPECIFIC`. They may appear as supporting evidence within a `SEGMENT_SPECIFIC` evaluation that already has a structural candidate.

---

## 15. Candidate Overlap and Evidence Independence

Multiple structural candidates can qualify within the same episode (e.g., `PAYMENT_METHOD:UPI` and `BANK:HDFC` both pass thresholds). These are **not** assumed to be mutually exclusive, causally independent, or three separately proven causes.

**Semantics:**

- Each `CandidateCause` is an **independent evidence finding** derived from its own excess failure contribution.
- Overlap is expected and correct — `BANK:HDFC` may explain failures *within* `PAYMENT_METHOD:UPI` traffic. The two candidates are correlated and may represent facets of the same underlying incident.
- Stage 4 **never** asserts absolute or exclusive causality. It produces a ranked list of "leading evidence-backed candidates."
- Consumers (Stage 5 and human reviewers) must treat overlapping candidates as correlated hypotheses, not independent root causes.

**Audit Payload Annotation:**

```json
"overlap_note": "Multiple structural candidates detected. Candidates may be correlated —
                 e.g., BANK:HDFC failures may be a subset of PAYMENT_METHOD:UPI failures.
                 Treat as correlated hypotheses, not independent proven causes."
```

---

## 16. Evidence Strength

Assigned deterministically to each `CandidateCause` based solely on the structural candidate's own metrics:

| Strength | Condition |
|---|---|
| `STRONG` | `excess_failure_contribution >= 0.70` **AND** `actual_segment_failures >= (3 x MIN_FAILURES_FOR_RCA_EVALUATION)` |
| `MODERATE` | `excess_failure_contribution >= 0.40` (and does not meet STRONG) |
| `WEAK` | `excess_failure_contribution >= MIN_CONTRIBUTION_THRESHOLD` (and does not meet MODERATE) |

Note: `INSUFFICIENT` is not an evidence strength on a `CandidateCause` row — a candidate that cannot be evaluated due to insufficient data does not become a `CandidateCause` at all.

---

## 17. Multiple Candidate Causes and Ranking

An evaluation can produce multiple `CandidateCause` records.

- Ranked **descending** by `excess_failure_contribution` (float, before rounding).
- The highest-ranked candidate is the "leading evidence-backed candidate."
- **Tie-breaking** (deterministic, in order):
  1. `actual_segment_failures` descending
  2. `candidate_dimension` ascending alphabetically
  3. `candidate_value` ascending alphabetically
- `rank` is a 1-based integer, persisted on the `CandidateCause` row.

---

## 18. UNKNOWN Semantics

Classification evaluates to `UNKNOWN` when:

- `actual_episode_failures < MIN_FAILURES_FOR_RCA_EVALUATION` — too few failures to analyze.
- Historical comparison data is absent or `INSUFFICIENT` for the analysis window.
- `episode.status == INVALIDATED` — episode was invalidated by Stage 3.
- `episode_total_excess_failures <= 0` — observed failures did not exceed baseline.

When `UNKNOWN`, the evaluation is persisted with `candidate_causes = []` and a `reason` string in the audit payload.

---

## 19. RCA Domain Model

```python
class RootCauseAnalysisEvaluation:
    evaluation_id: UUID           # Primary Key; deterministic UUID5 (see §23)
    episode_id: UUID              # FK to DegradationEpisode.episode_id
    evaluation_version: int       # Monotonically increasing per episode_id
    classification: str           # SYSTEMIC | SEGMENT_SPECIFIC | UNKNOWN
    analysis_window_start: datetime
    analysis_window_end: datetime
    input_fingerprint: str        # SHA-256 of deterministic input state (see §24)
    generated_at: datetime        # Wall-clock UTC insertion time (informational only)
    evidence_audit_payload: dict  # Immutable JSON snapshot (see §20)

class CandidateCause:
    candidate_id: UUID            # Primary Key; deterministic UUID5 (see §23)
    evaluation_id: UUID           # FK to RootCauseAnalysisEvaluation.evaluation_id
                                  # Immutable: version 1 rows are never reused in version 2
    candidate_dimension: str      # Structural dimension only (BANK, PAYMENT_METHOD, etc.)
    candidate_value: str
    evidence_strength: str        # STRONG | MODERATE | WEAK
    excess_failure_contribution: float
    actual_segment_failures: int
    expected_segment_failures: float   # Retained as float (not prematurely rounded)
    excess_segment_failures: float     # Retained as float
    rank: int                     # 1-based, ascending (1 = highest contribution)
```

---

## 20. Evidence Audit Payload (Reproducibility Contract)

The `evidence_audit_payload` is an immutable JSON snapshot stored on each `RootCauseAnalysisEvaluation` row. It must contain **all information required for a reviewer to independently reconstruct and verify why each candidate did or did not qualify**, without reading implementation code.

Required fields in the payload:

```
analysis_window:
  start, end (ISO8601 UTC)

historical_comparison_windows:
  list of { start, end, valid: bool, exclusion_reason? }

episode:
  episode_id, segment_dimension, segment_value, status

episode_totals:
  transaction_count
  actual_failures
  historical_baseline_failure_rate
  expected_failures (float)
  episode_total_excess_failures (float)

thresholds:
  MIN_FAILURES_FOR_RCA_EVALUATION
  MIN_CONTRIBUTION_THRESHOLD
  MIN_ERROR_DEVIATION_THRESHOLD
  MIN_BASELINE_VOLUME
  HISTORICAL_WEEKS_BACK
  MIN_VALID_HISTORICAL_WINDOWS

structural_candidates_evaluated:
  for each dimension/value:
    dimension
    value
    episode_segment_transaction_count
    historical_segment_transaction_count (sum across valid windows)
    historical_segment_failures (sum across valid windows)
    historical_segment_failure_rate (float)
    expected_segment_failures (float, unrounded)
    actual_segment_failures (int)
    excess_segment_failures (float, unrounded)
    traffic_share (float)
    failure_share (float)
    excess_failure_contribution (float)
    evidence_strength (STRONG | MODERATE | WEAK | null)
    qualified_as_candidate (bool)
    disqualification_reason (string | null)

supporting_error_evidence:
  for each qualifying error dimension/value:
    dimension
    value
    episode_error_frequency (float)
    baseline_error_frequency (float)
    error_deviation (float)
    qualifies_as_supporting (bool)

classification
classification_reason
overlap_note (if multiple candidates)
input_fingerprint
```

---

## 21. Physical Immutability Guarantees

All Stage 4 tables (`rca_evaluations`, `rca_candidate_causes`) are **strictly append-only**.

- **NO `UPDATE`** is ever performed on any historical row.
- **NO `DELETE`** is ever performed on any historical row.
- **NO `is_latest` flag** is maintained via mutation — current truth is always derived via `MAX(evaluation_version)`.
- Evaluation versions are **monotonically increasing** per `episode_id`.
- Historical rows remain **byte-for-byte unchanged** during any replay.

**Enforcement layers:**

1. **Repository-layer:** The `RCARepository` exposes only `append_evaluation()` and `append_candidate_causes()` write methods. No `update_evaluation()` or `delete_evaluation()` methods exist.
2. **Integration test enforcement:** Immutability tests (see §31) retrieve previously written rows by primary key *after* a replay and assert byte-for-byte equality against the pre-replay snapshot.
3. **Database-level:** If DB-level role restrictions are practical, a read-only role constraint can prevent `UPDATE`/`DELETE` on these tables. If not implemented at DB level, immutability is enforced exclusively through repository boundaries and integration tests. This limitation is explicitly documented and acknowledged.

---

## 22. Evaluation Versioning

Current truth is derived dynamically:

```sql
SELECT * FROM rca_evaluations
WHERE episode_id = :episode_id
ORDER BY evaluation_version DESC
LIMIT 1
```

When re-evaluation is required:

```
new_evaluation_version = MAX(evaluation_version) + 1  WHERE episode_id = :episode_id
```

This is an atomic read-then-append operation executed within a database transaction. A unique constraint on `(episode_id, evaluation_version)` prevents duplicate version insertion under concurrent conditions.

---

## 23. Logical Identity

```python
NAMESPACE_RCA = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")  # fixed constant

evaluation_id = uuid5(NAMESPACE_RCA, f"{episode_id}:{evaluation_version}")

candidate_id  = uuid5(NAMESPACE_RCA,
                      f"{evaluation_id}:{candidate_dimension}:{candidate_value}")
```

This guarantees determinism and divorces identity from wall-clock insertion times.

---

## 24. Deterministic Re-Evaluation Detection (Fingerprinting)

The ambiguous phrase "shifted significantly" is replaced with an exact deterministic mechanism.

### 24A. Input Fingerprint Definition

Stage 4 computes a **deterministic SHA-256 fingerprint** of the RCA input state before each evaluation. The fingerprint includes exactly the following inputs:

```python
fingerprint_inputs = {
    "episode_id":                str(episode.episode_id),
    "episode_status":            episode.status.value,
    "analysis_window_start":     analysis_window_start.isoformat(),
    "analysis_window_end":       analysis_window_end.isoformat(),
    "episode_actual_failures":   int(episode_actual_failures),
    "episode_transaction_count": int(episode_transaction_count),
    "historical_windows": sorted([
        (
            hist_window_start.isoformat(),
            int(hist_segment_failures),
            int(hist_segment_transactions),
            dimension,
            value
        )
        for each historical window, dimension, value
    ])
}

input_fingerprint = sha256(
    json.dumps(fingerprint_inputs, sort_keys=True, separators=(',', ':'))
).hexdigest()
```

Serialization is canonical: `sort_keys=True`, `separators=(',', ':')` (no whitespace).

### 24B. Re-Evaluation Decision Rule

```
IF input_fingerprint == last_evaluation.input_fingerprint:
    No re-evaluation needed. Do nothing.

IF input_fingerprint != last_evaluation.input_fingerprint:
    Append new evaluation version.
```

Wall-clock timestamps (`generated_at`, `ingested_at`) are **never** used to determine whether evidence changed. Only the fingerprint determines re-evaluation necessity.

### 24C. Replay Scenarios

| Scenario | Fingerprint Change? | Action |
|---|---|---|
| **A — Unchanged:** Episode boundaries and event counts identical since last evaluation | No | Do nothing |
| **B — Boundaries Extended:** Episode `ended_at_window` changed, or new BAD window detected | Yes | Append new evaluation version |
| **C — Episode Invalidated:** Stage 3 sets `status = INVALIDATED` | Yes | Append new evaluation version with `classification = UNKNOWN`, empty candidates, `reason = "Episode invalidated by Stage 3"` |
| **D — Late Events Arrive:** Same boundaries, but `episode_transaction_count` or `episode_actual_failures` changed due to late-arriving events | Yes | Append new evaluation version |

---

## 25. CandidateCause Version Ownership

Each `CandidateCause` row is permanently and exclusively owned by one `RootCauseAnalysisEvaluation` via a non-nullable foreign key:

```
CandidateCause.evaluation_id → RootCauseAnalysisEvaluation.evaluation_id
```

**Lifecycle rule:**

- When evaluation version `N` is generated, **new** `CandidateCause` rows are created with `evaluation_id = evaluation_id_for_version_N`.
- `CandidateCause` rows from version `N-1` are **never updated or reused** in version `N`.
- To retrieve current candidates: join `rca_candidate_causes` on the `evaluation_id` of `MAX(evaluation_version)` for the episode.

---

## 26. Database / Query Architecture

- Stage 4 queries Stage 2 `payment_health_snapshots` for structural dimension baselines (dimensions: `payment_method`, `bank`, `wallet`, `currency`, `error_source`).
- Stage 4 queries Stage 1 `payment_events` directly for supporting evidence dimensions (`error_code`, `error_step`), restricted by `timestamp >= analysis_window_start AND timestamp < analysis_window_end`.
- Historical baseline queries for Stage 1 use: `timestamp >= hist_start AND timestamp < hist_end`.
- Grouping and aggregation (counts by dimension) are executed in PostgreSQL using `GROUP BY`.

---

## 27. Performance / Indexing

The existing unique constraint in Stage 1 (`uq_payment_event_source_identity`) does not optimize time-range queries.

**Required index (to be created in Stage 4 migration):**

```sql
CREATE INDEX idx_payment_events_timestamp_event_type
ON payment_events (timestamp, event_type);
```

**Required constraint for Stage 4 tables:**

```sql
CREATE UNIQUE INDEX uq_rca_evaluation_version
ON rca_evaluations (episode_id, evaluation_version);
```

---

## 28. Configuration

All thresholds are explicit named constants with no magic numbers:

```python
HISTORICAL_WEEKS_BACK              = 4
MIN_VALID_HISTORICAL_WINDOWS       = 2
MIN_BASELINE_VOLUME                = 50
MIN_FAILURES_FOR_RCA_EVALUATION    = 10
MIN_CONTRIBUTION_THRESHOLD         = 0.30
MIN_ERROR_DEVIATION_THRESHOLD      = 0.20

STRONG_CONTRIBUTION_THRESHOLD      = 0.70
STRONG_FAILURE_COUNT_MULTIPLIER    = 3     # actual_failures >= 3 x MIN_FAILURES
MODERATE_CONTRIBUTION_THRESHOLD    = 0.40

NAMESPACE_RCA = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
```

---

## 29. Financial Semantics

RCA is descriptive, not predictive.

- **Allowed:** Recording `failed_gmv_minor_units` for a segment in the `evidence_audit_payload` as a descriptive human context field. GMV is sourced directly from `payment_events.amount_minor_units` in integer minor units.
- **Prohibited:** Generating `revenue_at_risk`, `recoverable_gmv`, or `protected_gmv`. Observed GMV is not Revenue at Risk.

---

## 30. Stage 4 → Stage 5 Interface

Stage 5 (Failure Prediction) will eventually consume:

- `episode_id`
- `classification`
- List of `CandidateCause` records: `(candidate_dimension, candidate_value, evidence_strength, excess_failure_contribution, rank)`
- `analysis_window_start` / `analysis_window_end`

Stage 4 exposes these via a repository read method returning the latest-version evaluation for a given `episode_id`. Stage 4 provides factual evidence; Stage 5 is responsible for translating that into predictive probabilities.

---

## 31. Test Strategy

Comprehensive pytest suite required before implementation is declared complete:

- **Baseline Correctness:**
  - Asserts exact weekday/time-of-day window matching (same as Stage 2 `get_historical_snapshots`).
  - Asserts volume-weighted aggregation produces correct results.
  - Asserts `< 2 valid historical windows` → `INSUFFICIENT`.
  - Asserts `SUM(hist_transactions) == 0` → `INSUFFICIENT`.
  - Asserts low-volume windows (below `MIN_BASELINE_VOLUME`) are excluded.

- **Expected Failure Math:**
  - Asserts `expected_segment_failures = episode_segment_transaction_count x historical_segment_failure_rate` (not global volume).
  - Asserts float values are retained pre-rounding.

- **Episode-Level Guard:**
  - Asserts `episode_total_excess_failures <= 0` produces `UNKNOWN` with empty candidates.

- **Concentration Correctness:**
  - Asserts a dominant traffic segment with historically normal failure rate is **not** flagged.
  - Asserts a minority segment with elevated failure rate **is** flagged via `excess_failure_contribution`.

- **Candidate Causes:**
  - Single clear structural candidate.
  - Multiple qualifying structural candidates — both are persisted.
  - Tied candidates broken deterministically by `actual_failures` then dimension/value alphabetically.
  - Zero qualifying candidates → `SYSTEMIC` (if baseline valid and excess > 0) or `UNKNOWN`.

- **Dimension Taxonomy:**
  - Asserts `ERROR_CODE` does **not** create a `CandidateCause` row.
  - Asserts `ERROR_CODE` with high deviation is recorded in `supporting_error_evidence`.
  - Asserts `SEGMENT_SPECIFIC` is not set based solely on error dimension evidence.

- **Candidate Overlap:**
  - Asserts episode with both `PAYMENT_METHOD:UPI` and `BANK:HDFC` qualifying produces two `CandidateCause` rows with correct individual contribution scores.
  - Asserts audit payload includes `overlap_note`.

- **Systemic vs Segment-Specific:**
  - Asserts broad uniform degradation (no structural dimension exceeds threshold) → `SYSTEMIC`.

- **UNKNOWN:**
  - Asserts `actual_failures < MIN_FAILURES_FOR_RCA_EVALUATION` → `UNKNOWN`.
  - Asserts `INVALIDATED` episode → `UNKNOWN`.
  - Asserts missing historical baseline → `UNKNOWN`.

- **Event Semantics:**
  - Asserts `payment.authorized` and `payment.created` are excluded from all counts.

- **Immutability & Replay:**
  - Asserts replay appends new `evaluation_version` rows.
  - Asserts old rows remain byte-for-byte unchanged after replay (read by primary key before and after replay; fields compared field-by-field).
  - Tests: unchanged fingerprint → no new version; changed boundary → new version; invalidated episode → `UNKNOWN` version.

- **Fingerprint Determinism:**
  - Asserts identical inputs produce identical fingerprint strings.
  - Asserts a single changed event count produces a different fingerprint.
  - Asserts identical inputs produce identical `evaluation_id` UUIDs and `candidate_id` UUIDs.

- **Audit Payload Determinism:**
  - Asserts identical inputs produce byte-for-byte identical `evidence_audit_payload` JSON.

- **Boundary Audit:**
  - Static assertion (grep or AST check) that Stage 4 source contains no LLM API calls.
  - Static assertion that Stage 4 source contains no `revenue_at_risk`, `recoverable_gmv`, or `protected_gmv` identifiers.

---

## 32. Risks and Remaining Ambiguities

### Performance
- Querying 4 weeks of raw `payment_events` for each episode evaluation carries significant DB load risk for error dimension analysis.
- **Mitigation:** Stage 4 prefers Stage 2 `payment_health_snapshots` for structural dimensions. Only `error_code` and `error_step` require direct `payment_events` queries. The required composite index on `(timestamp, event_type)` (§27) must be created in the migration.

### Confirmed Design Decision
- RCA evaluates **all** `ACTIVE` episodes regardless of severity. Evidence strength will naturally reflect the magnitude of the degradation.

### Remaining Open Questions (Implementation-Phase Risks)

- **Stage 3 Episode Mutability:** `DegradationEpisode` rows are mutable in Stage 3 (confirmed: `upsert_episode` uses `on_conflict_do_update`). Stage 4 captures a point-in-time snapshot of episode state in the fingerprint (via `episode_status`, `analysis_window_start`, `analysis_window_end`), which correctly detects changes.
- **Concurrent Replay:** If two Stage 4 evaluations for the same episode are triggered concurrently, the unique constraint `(episode_id, evaluation_version)` must prevent duplicate version insertion. This requires a per-episode serialized transaction or advisory lock in the repository. This is a known implementation risk to be resolved during implementation.

---

## 33. Final Boundary Audit

This design rigorously confines Stage 4 to evidence-backed, deterministic, append-only Root Cause Analysis. Confirmed:

- **No prediction** — no failure probability, no recovery probability.
- **No intervention** — no routing change, no alert escalation decision.
- **No revenue forecasting** — `revenue_at_risk`, `recoverable_gmv`, `protected_gmv` are explicitly prohibited.
- **No LLM** — all candidate selection is deterministic rule-based arithmetic.
- **No Stage 1–3 mutation** — Stage 4 reads Stage 1 and Stage 2 data; it does not modify them.
- **No Stage 5 implementation** — Stage 4 exposes a read interface; Stage 5 is not implemented here.
