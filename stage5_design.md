# Stage 5 — Failure Prediction

**Status:** FROZEN — Ready for final review / implementation after approval

## Stage 5 Freeze Status

- The design is ready for final review but implementation is not yet authorized by this document update.
- Implementation has NOT started.
- Only the architectural rules are frozen.
- Numerical defaults are initial/configurable and require empirical validation.
- Data-dependent decisions remain open until representative lifecycle data exists.
- This document update does not authorize implementation yet.
**Depends on:** Stage 1 (payment events), Stage 3 (degradation signals), Stage 4 (RCA evaluations)  
**Does not depend on:** Stage 2 `payment_health_snapshots` (mutable — not safe for historical training)  
**Does not depend on:** Stage 3 `degradation_episodes` (mutable — not safe for historical training)

---

## Architectural principle (§0)

> **Training must reconstruct what the system knew at historical prediction time, not what the database happens to say today about that historical period.**

Every design decision in this document is derived from or consistent with that principle.

---

## §1 — Prediction unit

The prediction unit is an **individual eligible payment attempt**, identified by `payment_id`.

Aggregate signals (segment, bank, payment method, wallet, currency, degradation, RCA) may contribute predictive features, but the prediction itself belongs to the individual payment attempt. The output is:

```
failure_probability ∈ [0.0, 1.0]
```

Stage 5 predicts risk only. It does **not** decide or execute interventions. It does **not** calculate revenue at risk, recoverable GMV, or expected saved GMV.

---

## §2 — Prediction timestamp and trigger event

### §2A — Repository inspection findings

The Razorpay normalizer (`src/core/normalizers/razorpay.py`) sets `event_type = event.event` directly from the Razorpay webhook's `event` field. The parser (`src/core/parsers/razorpay.py`) models the `event` field as a plain `str` with no explicit enumeration.

The Razorpay webhook documentation defines the following lifecycle events for a payment:

| Event type | Terminal? | Can carry pre-failure info? |
|---|---|---|
| `payment.authorized` | No | Yes — payment authorized, outcome not yet known |
| `payment.captured` | Yes | No — success |
| `payment.failed` | Yes | No — failure already occurred |

The `payment_events` table (`src/infrastructure/models.py`) stores `event_type` as `String(100)`, with no database-level constraint on values. The model's `PaymentEvent` domain class (`src/core/domain/models.py`) has `event_type: str` with no enum constraint.

The `PaymentHealthAnalyticsService` (`src/core/analytics/service.py`) counts terminal events explicitly:

```python
if e["event_type"] in ("payment.captured", "payment.failed")
```

The RCA service (`src/core/rca/service.py`) repeats the same pattern. This confirms the repository's canonical understanding of terminal event types.

### §2B — Prediction trigger definition

**Prediction trigger:** `payment.authorized`

**Rationale:**
- `payment.authorized` is the only currently supported pre-terminal lifecycle event that arrives before the final payment outcome is known.
- `payment.captured` and `payment.failed` are terminal events. Using them as prediction triggers would mean the outcome is already determined, making prediction pointless.
- No other pre-terminal event types (e.g., `payment.created`) are currently modeled in the parsers or observed in the existing analytics/RCA event-counting logic.

### §2C — Prediction timestamp T

```
T = ingested_at  (from PaymentEvent, set by the normalizer at UTC wall-clock insertion time)
```

This is the timestamp set by `normalize_razorpay_event` (`src/core/normalizers/razorpay.py`):

```python
ingested_at=datetime.now(timezone.utc)
```

`T` is the moment Stage 5 first has reliable access to this `payment.authorized` event.

### §2D — Eligibility rule

A payment attempt is **eligible** for Stage 5 prediction if and only if:
1. Its ingested `event_type == "payment.authorized"`.
2. No prior terminal event (`payment.captured` or `payment.failed`) for the same `payment_id` has been ingested before `T`.

**Implementation Note:** The current `payment_events` schema has no unique constraint on `payment_id` alone (only on `(source_system, source_event_id)`). A Stage 5 eligibility check therefore requires a lookup query: `SELECT EXISTS WHERE payment_id = ? AND event_type IN ('payment.captured', 'payment.failed') AND ingested_at < T`. This query is not currently indexed; see §19 (performance).

---

## §3 — Strict information boundary

For prediction timestamp `T`, **only information ingested or generated strictly before `T`** may be used. Training and inference must obey the same rule.

### §3A — Timestamp taxonomy

This taxonomy must be explicitly understood and enforced:

| Timestamp name | Field | Meaning | Mutable? |
|---|---|---|---|
| **Event time** | `payment_events.timestamp` | When the payment event occurred (Razorpay epoch, normalized to UTC) | No — immutable |
| **Ingestion time** | `payment_events.ingested_at` | When Stage 1 persisted the event | No — immutable |
| **Window effective time** | `degradation_signals.window_start` | Start of the 5-min tumbling window the signal describes | No — immutable |
| **Signal evaluation time** | `degradation_signals.evaluation_timestamp` | When Stage 3 evaluated the signal | No — immutable |
| **Episode start window** | `degradation_episodes.started_at_window` | First BAD window of the episode | **Mutable** (episode can be upserted) |
| **Episode ended window** | `degradation_episodes.ended_at_window` | Last window of recovery | **Mutable** |
| **Episode status** | `degradation_episodes.status` | ACTIVE / RECOVERED / INVALIDATED | **Mutable** |
| **RCA analysis window** | `rca_evaluations.analysis_window_start/end` | The event-window period the RCA analyzed | No — immutable once inserted |
| **RCA generated time** | `rca_evaluations.generated_at` | Wall-clock UTC when the RCA row was inserted | No — immutable once inserted |
| **Snapshot calculated time** | `payment_health_snapshots.calculated_at` | When Stage 2 calculated this snapshot | **Mutable** (upserted on late events) |

### §3B — Information boundary constraints

| Feature source | Boundary constraint | Reason |
|---|---|---|
| Stage 1 payment events | `ingested_at < T` | `ingested_at` is the availability timestamp; event-time alone is insufficient because late-arriving events have event-time before T but are not available at T |
| Stage 3 degradation signals | `evaluation_timestamp < T` | Signals are append-only; `evaluation_timestamp` is the availability timestamp |
| Stage 4 RCA evaluations | `generated_at < T` | RCA rows are append-only; `generated_at` is the insertion timestamp |
| Stage 2 snapshots | **Do NOT use** | Mutable via `upsert_snapshots`; snapshot state at historical T cannot be recovered |
| Stage 3 episodes | **Do NOT use** | Mutable via `upsert_episode`; episode status/ended_at at historical T cannot be recovered |

### §3C — Why `analysis_window_end` must NOT substitute for `generated_at`

`rca_evaluations.analysis_window_end` is the end of the event window that was *analyzed* by the RCA. It is determined by the episode state at analysis time.

`rca_evaluations.generated_at` is when the RCA row was *inserted into the database* — i.e., when it became observable by downstream systems.

**Critical difference:** An RCA evaluation for an analysis window ending at `W` may be generated hours or days after `W` if:
1. The episode was still ACTIVE at `W` and continued evolving.
2. A replay/recalculation was triggered by late-arriving events.
3. The RCA service was delayed or backlogged.

Using `analysis_window_end < T` to gate RCA eligibility would allow Stage 5 to use an RCA evaluation that was *not yet generated* at prediction time `T`, creating temporal leakage.

**Correct constraint:** `generated_at < T`

For a historical prediction at time `T`, select the RCA evaluation with:
```sql
WHERE episode_id = ? AND generated_at < T
ORDER BY evaluation_version DESC
LIMIT 1
```

---

## §4 — Historical training reconstruction

### §4A — Architectural principle (repeated for emphasis)

> **Training must reconstruct what the system knew at historical prediction time, not what the database happens to say today about that historical period.**

### §4B — Stage 1 (payment events) — immutable, safe

Payment events are immutable. Once inserted, they are never updated or deleted (`src/infrastructure/repository.py` — insert-only with `DuplicateEventConflictError` on conflict).

Historical behavioral features derived from payment events are constructed from:

```sql
SELECT ... FROM payment_events
WHERE ingested_at < T
  AND timestamp >= <window_start>
  AND timestamp < <window_end>
```

Using `ingested_at < T` ensures that late-arriving events (events with event-time in the past but ingested after `T`) are excluded from the historical feature set at time `T`.

### §4C — Stage 2 (health snapshots) — PROHIBITED for historical training

The `payment_health_snapshots` table is **not safe for historical training**. The repository (`src/infrastructure/analytics_repository.py`, `upsert_snapshots`) uses `on_conflict_do_update`, meaning:

- When `recalculate_late_events` is called (`src/core/analytics/service.py`), the snapshot for a historical window is silently overwritten.
- The `calculated_at` field is updated, but the previous value is lost.
- There is no version history for snapshots.

Stage 5 must compute all health/behavioral features from immutable Stage 1 `payment_events` as-of `T`. The Stage 2 snapshot table is a read-optimization for real-time Stage 3 evaluation, not a historical archive.

### §4D — Stage 3 (degradation signals) — safe; episodes PROHIBITED

**Signals:** `degradation_signals` is append-only (`DegradationRepository.append_signal` — insert only). Each re-evaluation appends a new row with an incremented `evaluation_version`. Historical signals are recoverable via:

```sql
SELECT * FROM degradation_signals
WHERE segment_dimension = ?
  AND segment_value = ?
  AND evaluation_timestamp < T
ORDER BY window_start, evaluation_version DESC
```

This query gives the latest signal evaluation known for each window, as of `T`.

**Episodes:** `degradation_episodes` is **mutable** via `upsert_episode`. The `status`, `ended_at_window`, `peak_absolute_drop`, `affected_window_count`, and `severity` fields can be overwritten at any time. Stage 5 must **not** read `degradation_episodes` for historical training.

**Reconstruction rule:** To determine the degradation state known at time `T` for a segment:
1. Query `degradation_signals` with `evaluation_timestamp < T`.
2. For each `window_start`, select the signal with the highest `evaluation_version`.
3. Derive historical degradation evidence from persisted Stage 3 append-only signals while respecting the historical `evaluation_timestamp < T` boundary and remaining semantically consistent with Stage 3. Stage 5 must **not independently recreate or second-guess the Stage 3 state machine**.
4. Extract signal attributes (absolute_drop, relative_drop, signal_type) directly from the signal rows.

Stage 5 must not reinterpret or recompute Stage 3's degradation verdict beyond extracting the persisted signal fields.

### §4E — Stage 4 (RCA) — safe

`rca_evaluations` and `rca_candidate_causes` are append-only. No UPDATE or DELETE operations exist in `RCARepository`. Historical evaluations are recoverable via `generated_at < T`. See §3C for the `analysis_window_end` vs `generated_at` distinction.

---

## §5 — Training label

### §5A — Label definition

| Subsequent terminal event | Label value |
|---|---|
| `payment.failed` | `1` (failure) |
| `payment.captured` | `0` (success) |

The label is derived from the *first* terminal event for `payment_id` ingested after the prediction trigger (`payment.authorized`).

### §5B — Unresolved / no-terminal-outcome observations

An `authorized` payment attempt for which no terminal event has been ingested within the observation window is **unresolved**. Unresolved observations must not silently become successes or failures.

**Handling rule:** Unresolved observations are excluded from training. Including them as either class would corrupt the label distribution.

**Label observation window:** The initial configurable default is **30 minutes**. The terminal outcome is determined from the terminal event `ingested_at`. This is NOT empirically validated and must remain configurable. Real lifecycle data will determine whether 30m is optimal.

---

## §6 — Feature contract

### §6A — Payment-attempt features (from Stage 1, at prediction time T)

These are fields available in the triggering `payment.authorized` event before its terminal outcome is known. All fields come from `PaymentEvent` (`src/core/domain/models.py`):

| Feature name | Source field | Type | Notes |
|---|---|---|---|
| `payment_method` | `payment_method` | Categorical (nullable) | Razorpay method: card, upi, netbanking, wallet, etc. |
| `bank` | `bank` | Categorical (nullable) | Present for netbanking; null for other methods |
| `wallet` | `wallet` | Categorical (nullable) | Present for wallet payments only |
| `currency` | `currency` | Categorical | ISO 4217, always uppercase (normalized by Stage 1) |
| `amount_minor_units` | `amount_minor_units` | Integer | Raw amount; may be log-transformed as a feature |
| `hour_of_day` | `timestamp` | Derived integer [0,23] | Temporal feature derived from payment event time |
| `day_of_week` | `timestamp` | Derived integer [0,6] | Temporal feature |

**Excluded fields:**  
- `error_code`, `error_description`, `error_source`, `error_step`, `error_reason` — these fields are populated only after failure and **must not** be used in Stage 5 prediction. They are available on `payment.failed` events, not on `payment.authorized` events. Using them would constitute terminal-outcome leakage.

**Amount representation:** `log(amount_minor_units)` is the initial configurable representation. It is not claimed to be empirically optimal and can be revised after real-data validation. Other feature engineering choices (e.g., day-of-week encoding) also require validation against training data.

### §6B — Historical behavioral features (from Stage 1, as-of T)

Derived from immutable `payment_events` where `ingested_at < T`. These are aggregate statistics computed directly from event data. Stage 2 snapshots are **not used**.

**Design intent:** Provide the model with a view of how payments for the same segment have been behaving historically.

| Feature family | Description |
|---|---|
| Recent segment failure rate | For same `payment_method`: proportion of failures in events before T |
| Recent bank failure rate | For same `bank` value: failure rate |
| Recent wallet failure rate | For same `wallet` value: failure rate |
| Recent currency failure rate | For same `currency` value: failure rate |
| Segment transaction volume | Count of recent transactions for same segment — indicates volume reliability |
| Overall recent failure rate | System-wide failure rate over recent window — baseline context |

**Behavioral feature windows:** The initial configurable defaults are a **30-minute window** and a **24-hour window**. These are not empirically validated and remain subject to real-data validation.

**Minimum volume thresholds:** The initial configurable defaults for minimum historical transactions required before a behavioral feature is considered reliable are:
- payment_method: 20 / 200
- bank: 10 / 100
- wallet: 10 / 100
- currency: 20 / 200
- global: 50 / 500
These are initial defaults and remain subject to empirical validation.

---

## §7 — Stage 3 feature contract

Stage 5 reads degradation state from the append-only `degradation_signals` table with `evaluation_timestamp < T`. It does **not** modify or re-run Stage 3 logic.

The following Stage 3 fields, when a valid signal exists for the payment's segment at time T, are eligible as features:

| Feature name | Source field | Type | Notes |
|---|---|---|---|
| `degradation_signal_type` | `signal_type` | Categorical (NORMAL/BAD/LOW_VOLUME/NO_BASELINE) | Latest signal type for relevant segment |
| `degradation_absolute_drop` | `absolute_drop` | Float (nullable) | Null when signal is LOW_VOLUME or NO_BASELINE |
| `degradation_relative_drop` | `relative_drop` | Float (nullable) | Null when signal is LOW_VOLUME or NO_BASELINE |
| `degradation_baseline_success_rate` | `baseline_success_rate` | Float (nullable) | Historical baseline at time of evaluation |
| `is_in_active_degradation` | Derived | Boolean | True if persisted Stage 3 degradation evidence indicates an active degradation condition as-of T |

**Derivation rule for `is_in_active_degradation`:** `is_in_active_degradation` represents whether persisted Stage 3 degradation evidence indicates an active degradation condition as-of T.
- Stage 5 derives this only from persisted Stage 3 append-only signals and their historical evaluation information.
- Stage 5 does NOT implement an independent degradation state machine.
- Stage 5 does NOT recreate, modify, validate, or second-guess Stage 3 episodes.
- Historical eligibility still requires `evaluation_timestamp < T`.
This preserves semantic consistency with Stage 3.

**Critical constraint:** Stage 5 must not reinterpret Stage 3's verdict. It observes the signal state only. It does not compute new episodes or validate existing ones.

**Stage 3 feature selection:** Match the payment against the relevant structural dimensions: `GLOBAL/ALL`, `currency`, `payment_method`, `bank`, and `wallet` when non-null. Do NOT use `error_source` as a pre-terminal feature. Preserve separate dimension-specific features. Stage 5 consumes persisted Stage 3 signals and does NOT independently recreate or second-guess the Stage 3 state machine. Historical eligibility requires `evaluation_timestamp < T`.

---

## §8 — Stage 4 RCA feature contract

Stage 5 reads RCA evaluations from the append-only `rca_evaluations` and `rca_candidate_causes` tables. Eligibility constraint: `generated_at < T`.

RCA information is **predictive evidence, not causal truth**. A candidate with `BANK = HDFC` and `STRONG` evidence means the RCA observed that HDFC had elevated excess failures during a degradation episode. It does not mean HDFC *caused* the failures, and it must never be represented that way in feature engineering or model documentation.

### §8A — Eligible RCA evaluation selection

For a given `payment.authorized` event at time `T`:
1. Identify whether persisted Stage 3 degradation signals provide eligible degradation evidence for the payment's relevant structural dimensions as-of `T`.
2. If relevant eligible RCA exists for that degradation context, Stage 5 selects the most recent RCA evaluation satisfying:
   ```sql
   SELECT * FROM rca_evaluations
   WHERE episode_id = ?
     AND generated_at < T
   ORDER BY evaluation_version DESC
   LIMIT 1
   ```
3. If no evaluation exists, all RCA features are null.

### §8B — RCA feature representation

| Feature name | Source field | Type | Notes |
|---|---|---|---|
| `rca_classification` | `classification` | Categorical (SYSTEMIC/SEGMENT_SPECIFIC/UNKNOWN/NULL) | NULL when no eligible RCA |
| `rca_candidate_dimension` | `candidate_dimension` (rank-1) | Categorical (BANK/CURRENCY/PAYMENT_METHOD/WALLET/NULL) | Rank-1 candidate only for initial model |
| `rca_candidate_value` | `candidate_value` (rank-1) | Categorical (nullable) | Value associated with rank-1 candidate |
| `rca_evidence_strength` | `evidence_strength` (rank-1) | Categorical (STRONG/MODERATE/WEAK/NULL) | |
| `rca_excess_failure_contribution` | `excess_failure_contribution` (rank-1) | Float (nullable) | Proportion of excess failures from this candidate |
| `rca_candidate_rank` | `rank` (rank-1) | Integer (nullable, always 1 for initial model) | Reserved for future multi-candidate features |

**Initial model restriction:** Only the rank-1 candidate cause is included in the initial Stage 5 feature set. Including multiple candidates requires a richer representation strategy (e.g., multi-hot encoding, separate feature per dimension) that is reserved for future model iterations.

### §8C — Categorical encoding

All categorical RCA fields must use deterministic, version-controlled encoding:
- `rca_classification`: `SYSTEMIC=0, SEGMENT_SPECIFIC=1, UNKNOWN=2, absent=3`
- `rca_evidence_strength`: `WEAK=0, MODERATE=1, STRONG=2, absent=3`
- Dimension and value encoding: determined per training data vocabulary; vocabulary must be serialized as part of `feature_schema_version` (see §15).

Encoding dictionaries are part of the feature schema and must be versioned.

**RCA feature representation:** Use the rank-1 RCA candidate for the initial model. Candidate match is a derived boolean feature `rca_candidate_matches_payment_segment`:
- `SEGMENT_SPECIFIC` + rank-1 candidate matching the payment's structural dimension/value => `True`
- rank-1 mismatch => `False`
- `SYSTEMIC` => `False`
- `UNKNOWN` or no eligible RCA => `NULL`
RCA remains predictive evidence, not causal truth. RCA eligibility requires `generated_at < T`.

---

## §9 — Model architecture

### §9A — Interface contract

Stage 5 defines a versioned model interface. The interface must be replaceable without changing the prediction service contract:

```python
class FailurePredictionModel:
    model_name: str
    model_version: str

    def predict(self, feature_vector: dict) -> float:
        """Returns failure_probability in [0.0, 1.0]."""
        ...

    def get_feature_schema_version(self) -> str:
        ...
```

### §9B — Initial baseline

**Calibrated logistic regression**

Rationale:
- Interpretable: coefficients are auditable.
- Deterministic inference: same inputs produce the same output.
- Probability output: native sigmoid output, calibrated with Platt scaling or isotonic regression.
- Fast: low latency for real-time inference.
- Easy to retrain and version.
- Auditable: feature weights can be inspected.

### §9C — Prohibited model types

**No LLM may be used as the prediction engine.** This is enforced by existing boundary tests (`tests/boundaries/test_boundaries.py`). Stage 5 boundary tests will extend this constraint to Stage 5 source files.

---

## §10 — Probability output

The authoritative prediction output is:

```
failure_probability in [0.0, 1.0]
```

A **risk band** derived from probability thresholds may be persisted alongside the probability for presentation purposes, but the probability is the canonical value.

Risk band definition (thresholds are implementation-blocking; see §24):

| Risk band | Description |
|---|---|
| `LOW` | Low failure risk |
| `ELEVATED` | Elevated failure risk |
| `HIGH` | High failure risk |

**Risk-band thresholds:** Numerical thresholds remain data-dependent and are NOT frozen. They depend on the calibrated probability distribution observed during model evaluation on test data and will be informed by Stage 6 intervention economics.

Stage 5 does not select or recommend interventions. Stage 6 operates on the `risk_band` and `failure_probability` only.

---

## §11 — Training methodology

### §11A — Chronological split

Training uses chronological splits only. Random mixing of time periods is prohibited.

Proposed split structure:
```
[Training set] ──────────── [Validation set] ── [Test set]
    older                                          newer
```

Training set and validation/test sets must not overlap in time. The test period must be strictly later than the training period.

**Date boundaries:** The exact date boundaries for train/validation/test splits depend on the volume and distribution of historical `payment.authorized` events and their labels. These cannot be honestly frozen until representative historical lifecycle data exists.

### §11B — Class imbalance

Payment failure rates are expected to be significantly below 50%, creating class imbalance. Stage 5 must handle this without corrupting the probability output for production inference.

Permitted strategies:
- **Class-weight adjustment** in logistic regression training (does not affect inference).
- **Oversampling (SMOTE or similar)** applied to training data only — never to validation or test data, and never to production inference inputs.
- **Threshold-free evaluation** (PR-AUC, ROC-AUC, Brier score) as primary metrics during model selection.

Prohibited strategies:
- Any sampling or reweighting applied at inference time.
- Modifying the calibrated probability output to compensate for imbalance.

### §11C — Calibration

After training, the model's probability outputs must be calibrated (Platt scaling or isotonic regression) against the validation set to ensure `failure_probability` is a true probability estimate.

Calibration must be evaluated on the test set using Brier score and a calibration curve.

---

## §12 — Model evaluation metrics

These metrics evaluate Stage 5 prediction quality only. Downstream economic value (recovered GMV, intervention lift) is evaluated in later stages and must not be computed here.

### §12A — Discrimination

| Metric | Why |
|---|---|
| PR-AUC | Primary metric for imbalanced classification; measures precision-recall tradeoff across all thresholds |
| ROC-AUC | Secondary; measures rank-ordering ability |

### §12B — Calibration

| Metric | Why |
|---|---|
| Brier score | Scalar measure of probability calibration quality |
| Calibration curve | Visual assessment of predicted vs actual failure rate by probability bin |

### §12C — Threshold diagnostics (informational only)

At selected threshold(s):
- Precision, Recall, F1
- False positive rate, False negative rate

These are informational for development and testing. Stage 5 does not select a production operating threshold; that is a Stage 6 responsibility.

---

## §13 — Insufficient data / cold start

Stage 5 must never fabricate a probability when required evidence is insufficient.

### §13A — Prediction status enum

| Status | Meaning |
|---|---|
| `PREDICTED` | Feature reconstruction succeeded; model inference ran; probability is valid |
| `INSUFFICIENT_DATA` | Required historical features could not be reliably computed |
| `NOT_ELIGIBLE` | Payment attempt does not qualify for Stage 5 prediction (see §2D) |

When `prediction_status != PREDICTED`, `failure_probability` is persisted as `NULL`.

### §13B — Insufficient data conditions

A prediction is classified `INSUFFICIENT_DATA` when any of the following apply:
- If both global 30m and global 24h failure-rate features are unavailable because of insufficient volume, the prediction may be `INSUFFICIENT_DATA`. (Segment-specific feature insufficiency may result in null feature values and does not automatically make the entire prediction insufficient).
- The feature vector cannot be constructed due to missing required fields.

**Volume insufficiency rule:** The global thresholds are initial configurable defaults: 50 / 500. These thresholds require empirical validation.

---

## §14 — Prediction persistence

Predictions are append-only. No UPDATE or DELETE is permitted on Stage 5 prediction rows. This mirrors the immutability design of Stage 4.

### §14A — Prediction record schema

| Field | Type | Description |
|---|---|---|
| `prediction_id` | UUID | Deterministic UUID5 (see §16) |
| `payment_attempt_id` | String(255) | `payment_id` from the triggering event |
| `prediction_version` | Integer | Monotonically increasing per `payment_attempt_id` |
| `predicted_at` | DateTime(tz=True) | Prediction timestamp T (`ingested_at` of trigger event) |
| `prediction_horizon` | String | Human-readable horizon label (e.g. "30m") |
| `failure_probability` | Float (nullable) | Calibrated probability in [0,1]; NULL when status != PREDICTED |
| `risk_band` | String(20) (nullable) | LOW / ELEVATED / HIGH; NULL when status != PREDICTED |
| `prediction_status` | String(20) | PREDICTED / INSUFFICIENT_DATA / NOT_ELIGIBLE |
| `model_name` | String(100) | Canonical model identifier |
| `model_version` | String(50) | Semantic version of the trained model artifact |
| `feature_schema_version` | String(50) | Version of the feature engineering contract |
| `feature_snapshot` | JSON | Serialized feature vector used for this prediction |
| `input_fingerprint` | String(71) | SHA-256 fingerprint (see §16) |
| `created_at` | DateTime(tz=True) | Wall-clock UTC row insertion time (informational only) |

**Prediction horizon:** `30m` is the initial configurable default. It is not empirically validated.

### §14B — Versioning semantics

When Stage 5 receives a new prediction request for a `payment_attempt_id` where a previous prediction exists with an identical `input_fingerprint`, the prediction is idempotent — no new row is created (see §18).

If the `input_fingerprint` differs (e.g., more historical data is available, model version changed), a new row is appended with `prediction_version = max_version + 1`.

### §14C — Table constraints

```sql
UNIQUE (payment_attempt_id, prediction_version)
```

A version advisory lock (mirroring Stage 4's `pg_advisory_xact_lock` pattern in `src/core/rca/service.py`) must be used during version allocation to prevent concurrent duplicate versions.

---

## §15 — Model and feature versioning

Every prediction record must identify:

| Identity | Field | Semantics |
|---|---|---|
| Model | `model_name` + `model_version` | Identifies the trained model artifact. Semantic version (e.g., `logistic_regression_v1.0.0`). NOT a timestamp. |
| Feature schema | `feature_schema_version` | Identifies the feature engineering contract (column names, encoding dictionaries, window sizes). Semantic version (e.g., `v1.0.0`). NOT a timestamp. |

A `model_version` change must be triggered by:
- Changes to model hyperparameters or architecture.
- Model retraining on new/updated training data.
- Changes to calibration parameters.

A `feature_schema_version` change must be triggered by:
- Addition or removal of features.
- Changes to encoding dictionaries.
- Changes to historical window sizes or minimum data requirements.

A `model_version` change does not automatically require a `feature_schema_version` change, and vice versa.

---

## §16 — Fingerprinting

Stage 5 uses deterministic SHA-256 fingerprinting over canonical JSON, mirroring the Stage 4 pattern (`src/core/domain/rca_models.py`, `compute_input_fingerprint`).

### §16A — Canonical inputs

The fingerprint is computed over a dict containing at minimum:

```python
fingerprint_inputs = {
    "payment_attempt_id": str,            # payment_id from trigger event
    "predicted_at": str,                  # T as ISO 8601 UTC
    "prediction_horizon": str,            # horizon label
    "feature_snapshot": dict,             # complete feature vector (sorted)
    "model_name": str,
    "model_version": str,
    "feature_schema_version": str,
}
```

### §16B — Canonicalization

```python
raw = json.dumps(fingerprint_inputs, sort_keys=True, separators=(",", ":"))
digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
fingerprint = f"sha256:{digest}"
```

Same inputs + same model/schema => same fingerprint. This is the idempotency key.

---

## §17 — Auditability

Every persisted `failure_prediction` record must be self-contained for audit purposes, without relying on mutable current database state.

The `feature_snapshot` JSON column must preserve:

| Audit element | Required |
|---|---|
| Prediction timestamp T | Yes |
| `payment_attempt_id` | Yes |
| Full feature vector (all input values before encoding) | Yes |
| `feature_schema_version` (encoding dictionary reference) | Yes |
| `model_name` + `model_version` | Yes |
| `failure_probability` | Yes |
| `prediction_status` | Yes |
| `risk_band` | Yes |
| `input_fingerprint` | Yes |
| Historical Stage 3 signals used (signal_ids or window/eval_version pairs) | Yes |
| Historical Stage 4 evaluation used (evaluation_id, generated_at) | Yes |
| Data sufficiency state for each feature family | Yes |
| Model training metadata reference (external artifact ID, not embedded) | Yes |

---

## §18 — Idempotency

When Stage 5 receives a prediction request for a `payment_attempt_id` that already has a persisted prediction:

1. Compute the `input_fingerprint` for the current context.
2. Retrieve the latest prediction for this `payment_attempt_id`.
3. If fingerprints match => return the existing prediction without inserting a new row.
4. If fingerprints differ (context changed) => append a new prediction version.

This mirrors the Stage 4 idempotency pattern (`src/core/rca/service.py`: fingerprint check before inserting new evaluation).

A repeated request with identical context must produce zero additional database writes.

---

## §19 — Performance

### §19A — Query bounds

All historical feature queries must be bounded. Stage 5 must not load the full `payment_events` table into application memory. Database-side aggregation (`COUNT`, `SUM`, SQL `WHERE` with proper bounds) is required.

### §19B — Required indexes (to be added in Stage 5 migration — do NOT add now)

| Table | Index | Required for |
|---|---|---|
| `payment_events` | `(payment_id, ingested_at)` | Eligibility check; label lookup |
| `payment_events` | `(payment_method, ingested_at, event_type)` | Historical payment_method failure rate |
| `payment_events` | `(bank, ingested_at, event_type)` | Historical bank failure rate |
| `payment_events` | `(wallet, ingested_at, event_type)` | Historical wallet failure rate |
| `payment_events` | `(currency, ingested_at, event_type)` | Historical currency failure rate |
| `degradation_signals` | `(segment_dimension, segment_value, evaluation_timestamp)` | Stage 3 as-of query |
| `rca_evaluations` | `(episode_id, generated_at, evaluation_version)` | RCA as-of query |
| `failure_predictions` (new table) | `(payment_attempt_id, prediction_version)` | Idempotency and versioning |

**No indexes are added in this design document.** Indexes are added in the Stage 5 migration, implemented in a future phase.

### §19C — Existing indexes

No explicit index creation is present in the existing migrations beyond primary keys and unique constraints. The `payment_events` table currently has:
- PK on `event_id`
- UNIQUE on `(source_system, source_event_id)`

No composite index on `payment_id`, `ingested_at`, or `event_type` currently exists.

---

## §20 — Stage 5 strict non-responsibilities

Stage 5 **must NOT**:

- Recommend interventions
- Select interventions
- Execute interventions
- Calculate revenue at risk
- Calculate recoverable GMV
- Calculate expected saved GMV
- Claim causal root causes
- Perform counterfactual attribution
- Modify Stage 2, Stage 3, or Stage 4 records
- Use `payment_health_snapshots` as historical training truth (mutable)
- Use `degradation_episodes` as historical training truth (mutable)
- Use future information (any event or evaluation with availability timestamp >= T)
- Use `analysis_window_end` as a substitute for `generated_at` when gating RCA eligibility
- Use an LLM as the prediction engine

---

## §21 — Stage 5 -> Stage 6 contract

Stage 6 receives the following from Stage 5. Stage 6 is responsible for all intervention, financial, and counterfactual logic.

| Field | Type | Description |
|---|---|---|
| `prediction_id` | UUID | Immutable reference to this specific prediction |
| `payment_attempt_id` | String | The `payment_id` being predicted on |
| `predicted_at` | DateTime | Prediction timestamp T |
| `prediction_horizon` | String | Time window the prediction covers |
| `failure_probability` | Float (nullable) | Calibrated probability in [0,1]; NULL if not PREDICTED |
| `risk_band` | String (nullable) | LOW / ELEVATED / HIGH; NULL if not PREDICTED |
| `prediction_status` | String | PREDICTED / INSUFFICIENT_DATA / NOT_ELIGIBLE |
| `model_version` | String | Model version that produced this prediction |

Stage 6 must not query `failure_predictions` for historical training features. If Stage 6 requires historical prediction data, it reads from the append-only `failure_predictions` table using `predicted_at` or `created_at` as its temporal boundary.

---

## §22 — Test strategy

### §22A — Temporal leakage tests

| Test | Assertion |
|---|---|
| Future payment events excluded | Events with `ingested_at >= T` must never appear in the feature vector for prediction at `T` |
| Future Stage 3 signals excluded | Signals with `evaluation_timestamp >= T` must never appear |
| Future RCA evaluations excluded | Evaluations with `generated_at >= T` must never appear |
| `generated_at` vs `analysis_window_end` | An RCA with `analysis_window_end < T` but `generated_at >= T` must be excluded |
| Late-ingested events | A payment event with `timestamp` in the past but `ingested_at >= T` must be excluded |
| Historical feature reconstruction | Given a fixed set of events, feature reconstruction at T1 and T2 > T1 must differ if new events were ingested between T1 and T2 |

### §22B — Mutable-state protection tests

| Test | Assertion |
|---|---|
| Stage 2 snapshot mutation | After a Stage 2 upsert changes a historical snapshot, Stage 5 historical features must not change (because Stage 5 does not read Stage 2) |
| Stage 3 episode mutation | After a Stage 3 episode status changes from ACTIVE to RECOVERED, historical Stage 5 features reconstructed as-of an earlier T must not change |

### §22C — Prediction correctness tests

| Test | Assertion |
|---|---|
| Probability range | `failure_probability` in [0.0, 1.0] always when status is PREDICTED |
| Deterministic inference | Identical feature vector => identical `failure_probability` |
| Model/version metadata | Every prediction row has non-null `model_name`, `model_version`, `feature_schema_version` |
| INSUFFICIENT_DATA behavior | When volume is below threshold, `failure_probability = NULL`, `prediction_status = INSUFFICIENT_DATA` |
| NOT_ELIGIBLE behavior | When event is `payment.failed` or `payment.captured`, prediction is NOT_ELIGIBLE |

### §22D — Model tests

| Test | Assertion |
|---|---|
| Chronological split | No test-set observation has a training equivalent from a later time |
| Class imbalance handling | Class weight adjustment applied only to training; not to inference |
| Calibration | Brier score and calibration curve evaluated on held-out test set |
| Evaluation metrics | PR-AUC and ROC-AUC computed and compared against a defined minimum acceptance threshold |

### §22E — Persistence tests

| Test | Assertion |
|---|---|
| Append-only | No UPDATE or DELETE exists in Stage 5 prediction repository |
| Versioning | Second prediction with different context creates version 2, not overwrite |
| Fingerprint determinism | Same inputs, same model/schema => same fingerprint |
| Idempotency | Identical inputs => no new database row; existing row returned |
| Replay behavior | Replay of same prediction context => idempotent (no new version) |

### §22F — Boundary tests

Stage 5 boundary tests must assert, via static source analysis (mirroring `tests/boundaries/test_boundaries.py`):

- No LLM imports in Stage 5 source files (`openai`, `langchain`, `anthropic`, `llama`, `transformers`, `chatgpt`, `gpt-4`, `claude`, `mistral`)
- No intervention logic (`intervention`, `recommended_action`, `execute_`, `select_action`)
- No revenue/financial logic (`revenue_at_risk`, `recoverable_gmv`, `protected_gmv`, `counterfactual`, `expected_saved_gmv`)
- No Stage 2 snapshot reads in historical training path (`payment_health_snapshots`, `upsert_snapshots`)
- No Stage 3 episode reads in historical training path (`degradation_episodes`, `upsert_episode`)
- No `analysis_window_end` used as RCA eligibility boundary

---

## §22.5 — Payment Lifecycle Coverage Requirement

Stage 5 implementation must establish tests/fixtures covering the payment lifecycle, without changing Stages 1–4. 
`payment.authorized` is valid in the canonical event model, but it is currently not consumed by existing production source code, and there is currently no same-payment authorized→terminal lifecycle coverage.

Stage 5 must add tests/fixtures covering:
1. Same payment: `payment.authorized → payment.failed`
2. Same payment: `payment.authorized → payment.captured`
3. Prediction eligibility before terminal outcome
4. Correct terminal label lookup after prediction
5. Verification that future terminal information cannot leak into the prediction

*Important: The existing repository contains a deliberately reversed `payment.failed → payment.captured` sequence used for analytical/window testing. **Do not treat that as real lifecycle evidence.***

---

## §23 — Historical training vs live inference vs outcome

### §23A — Historical training

```
For historical prediction time T:
  +--> Stage 1: payment_events WHERE ingested_at < T
  |     -> immutable behavioral features
  +--> Stage 3: degradation_signals WHERE evaluation_timestamp < T
  |     -> as-of degradation state (latest version per window)
  +--> Stage 4: rca_evaluations WHERE generated_at < T
  |     -> as-of RCA state (latest version per episode)
  +-> feature_vector_at_T -> model -> failure_probability_at_T
```

Stage 2 `payment_health_snapshots` and Stage 3 `degradation_episodes` are excluded from this path because they are mutable and cannot represent past state.

### §23B — Live inference

```
For current eligible payment attempt (payment.authorized) at T = now:
  +--> Stage 1: payment_events WHERE ingested_at < T (most recent available data)
  +--> Stage 3: degradation_signals WHERE evaluation_timestamp < T
  +--> Stage 4: rca_evaluations WHERE generated_at < T
  +-> feature_vector -> calibrated model -> failure_probability -> persist prediction
```

### §23C — Outcome (label assignment for training)

```
After prediction at T:
  +-> Observe first terminal event for payment_id:
        payment.captured -> label = 0 (success)
        payment.failed   -> label = 1 (failure)
        (no terminal event within observation window -> unresolved -> excluded from training)
```

---

## §24 — Final Design Decisions & Classifications

The design decisions for Stage 5 are explicitly distinguished into three categories:

### 1. FROZEN ARCHITECTURAL RULES
Explicitly marked as **FROZEN ARCHITECTURAL RULES**:
- Prediction unit = individual payment attempt.
- Prediction trigger = `payment.authorized`.
- Prediction timestamp `T` = `ingested_at` of the authorization event.
- Strict as-of boundary: information with `ingested_at >= T` cannot influence the prediction.
- Historical training reconstructs features as-of historical prediction time `T`.
- Stage 1 immutable payment events are the primary historical source.
- Stage 2 mutable snapshots must NOT be used as historical training truth.
- Stage 3 mutable `degradation_episodes` must NOT be used as historical training truth.
- Stage 3 append-only `degradation_signals` may be used only when `evaluation_timestamp < T`.
- Stage 4 append-only RCA may be used only when `generated_at < T`.
- Stage 3 features preserve separate structural dimensions.
- Stage 4 uses only the rank-1 RCA candidate for the initial payment-segment match feature.
- RCA is predictive evidence, not causal proof.
- Initial model = calibrated logistic regression behind a replaceable model interface.
- `failure_probability ∈ [0,1]` is authoritative.
- Risk bands are derived from probability and are secondary.
- Numerical risk thresholds are deferred until calibration/operating-point analysis.
- Train/validation/test separation is chronological; no random temporal mixing.
- Prediction records are append-only and versioned.
- Prediction records contain feature snapshot, model version, feature schema version, and input fingerprint.
- Input fingerprint is computed deterministically from canonical prediction inputs/features and relevant versions.
- Stage 5 does not perform interventions, recovery decisions, revenue-at-risk calculations, recovered-GMV calculations, or counterfactual attribution.
- No LLM is required for Stage 5.

### 2. INITIAL CONFIGURABLE / EMPIRICAL DEFAULTS
**INITIAL CONFIGURABLE DEFAULTS — NOT EMPIRICALLY VALIDATED**

They must be configurable and validated when representative lifecycle data becomes available. Do NOT claim these numbers are statistically proven, empirically validated, or optimal.
- Label observation window = 30 minutes.
- Terminal label determined using terminal event `ingested_at`.
- Prediction horizon = 30 minutes.
- Behavioral feature windows = 30 minutes and 24 hours.
- Initial volume thresholds:
  - payment method: 20 / 200
  - bank: 10 / 100
  - wallet: 10 / 100
  - currency: 20 / 200
  - global: 50 / 500
- Initial global insufficiency rule: if both global 30m and 24h failure-rate features are unavailable because of insufficient volume, prediction may be `INSUFFICIENT_DATA`.
- Initial amount representation = `log(amount_minor_units)`.
- Initial risk bands = LOW / ELEVATED / HIGH.

### 3. DATA-DEPENDENT DECISIONS
**OPEN / DATA-DEPENDENT**

These cannot honestly be frozen because the current repository has no representative production/staging lifecycle dataset:
- Exact train/validation/test date boundaries.
- Final class distribution.
- Final calibration characteristics.
- Final numerical risk-band thresholds.
- Final volume sufficiency thresholds if real data indicates different values.
- Whether the 30-minute horizon is optimal.
- Whether the proposed feature windows are optimal.
- Any feature decisions that require validation against representative historical lifecycle data.

---

## §25 — Domain object definitions (for implementation reference)

### §25A — `FailurePrediction` domain object

```python
@dataclass(frozen=True)
class FailurePrediction:
    prediction_id: uuid.UUID               # deterministic UUID5
    payment_attempt_id: str                # payment_id from trigger event
    prediction_version: int                # monotonically increasing per payment_attempt_id
    predicted_at: datetime                 # T: ingested_at of trigger event
    prediction_horizon: str                # e.g. "30m"
    failure_probability: Optional[float]   # None when status != PREDICTED
    risk_band: Optional[str]               # LOW/ELEVATED/HIGH or None
    prediction_status: str                 # PREDICTED/INSUFFICIENT_DATA/NOT_ELIGIBLE
    model_name: str
    model_version: str
    feature_schema_version: str
    feature_snapshot: dict                 # full audit JSON
    input_fingerprint: str                 # sha256: prefixed
    created_at: datetime                   # wall-clock insertion time (informational)
```

### §25B — `PredictionStatus` enum

```python
class PredictionStatus(str, Enum):
    PREDICTED = "PREDICTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
```

### §25C — Deterministic identity

```python
# prediction_id = uuid5(NAMESPACE_STAGE5, '{payment_attempt_id}:{prediction_version}')
NAMESPACE_STAGE5 = uuid.UUID("c3d4e5f6-a7b8-9012-cdef-345678901234")

def make_prediction_id(payment_attempt_id: str, prediction_version: int) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE_STAGE5, f"{payment_attempt_id}:{prediction_version}")
```

The NAMESPACE_STAGE5 UUID is fixed. It must never change after the first prediction is persisted.

---

## §26 — Relationship to existing Stage boundary tests

The existing `tests/boundaries/test_boundaries.py` currently checks:
1. No LLM imports in Stage 1-4 source files.
2. No speculative fields (`failure_probability`, `degradation`, etc.) in `src/core/domain/models.py`.
3. Stage 4 does not contain Stage 5 prediction logic.

Stage 5 implementation must:
- Not introduce `failure_probability` in `src/core/domain/models.py` (the Stage 1 domain model). Stage 5 has its own domain model file.
- Not introduce LLM imports in any Stage 5 source file.
- Extend the boundary test file with Stage 5 boundary assertions (test implementation phase only).

The forbidden field `failure_probability` in `test_no_stage2_plus_fields` checks only `src/core/domain/models.py`. Stage 5's own `failure_prediction_models.py` will legitimately contain this field in a separate module.

---

*End of Stage 5 design specification.*
*Design freeze achieved pending final review. Implementation begins only after explicit implementation authorization.*
