# Project Context

This file serves as the persistent, model-agnostic source of truth for the Razorpay Buildathon **Revenue Protection & Recovery Engine**. Development switches between different AI models with limited context windows, so this document must be read to understand the project architecture, boundaries, and current state.

## Product

We are building a production-quality **Revenue Protection & Recovery Engine for payment failures**.

The long-term system is intended to:
1. Ingest payment events.
2. Normalize them into canonical payment events.
3. Measure payment health.
4. Detect payment degradation.
5. Identify likely root causes.
6. Predict payment failures.
7. Estimate revenue at risk / recoverable revenue where appropriate.
8. Decide whether/how to intervene.
9. Execute interventions.
10. Observe factual outcomes.
11. Perform counterfactual attribution.
12. Measure recovered/protected GMV and system effectiveness.

**Strict Stage Boundary:** Development is strictly staged. Later-stage functionality must never leak into earlier stages.

## Frozen Checkpoints

The repository development is strictly versioned by Git checkpoints. 

- **Stage 1 baseline (`4522b3f`)**: Razorpay ingestion foundation + canonical payment segmentation fields.
- **Stage 2 (`cf81152`)**: Payment health analytics.
- **Stage 3 (`ed8ea3f`)**: Degradation detection.
- **Stage 4 (`c9f1e2d`)**: Root cause analysis (RCA).
- **Stage 5 (`d91953e`)**: Failure prediction.
- **Stage 6 (`f5965c3`)**: Intervention decisioning.
- **Stage 7**: Intervention execution + outcome observation.

The current repository should be treated as **Stage 7 complete and frozen** unless explicitly instructed otherwise.

## Architecture & Implementation History

### Stage 1 — Razorpay Ingestion (Frozen)

**Architecture Flow:**
`Razorpay webhook → FastAPI boundary → signature verification → Pydantic schema validation → normalization → canonical PaymentEvent → deterministic hashing / deduplication → PostgreSQL persistence.`

**Important Invariants:**
- Razorpay is the current provider integration.
- Webhook authenticity is verified using the Razorpay webhook signature before JSON processing.
- `x-razorpay-event-id` is the source event identity.
- Deduplication identity is `(source_system, source_event_id)`.
- Payload hash is separate from webhook signature.
- Raw payload is retained as immutable evidence.
- Conflicting reuse of the same source event ID must not overwrite historical evidence.
- Money is represented in integer minor units.
- Timestamps are timezone-aware UTC.
- Canonical segmentation fields include factual provider fields: `payment_method`, `bank`, and `wallet`.
- **Boundaries:** No prediction, RCA, health score, intervention, protected GMV, or revenue-at-risk logic. No speculative or inferred fields.

### Stage 2 — Payment Health Analytics (Frozen)

Stage 2 consumes Stage 1 canonical `payment_events` and produces `payment_health_snapshots`.

**Important Semantics:**
- Five-minute tumbling UTC windows. Time boundaries are based on event occurrence `timestamp` using half-open intervals: `[window_start, window_end)`.
- Describes observed lifecycle events; it does not collapse payment history into a final state.
- `payment.failed` contributes to failed transaction metrics / failed GMV.
- `payment.captured` contributes to successful transaction metrics / successful GMV. Other events are ignored for outcome metrics.
- GMV is factual observed event GMV, not revenue-at-risk or protected GMV.
- Missing segmentation values are represented consistently.
- Segmentation includes global and provider dimensions.
- Baseline uses the day-of-week/time-of-day historical median approach.
- Low-volume windows are explicitly flagged with `insufficient_volume`.
- Late-arriving events cause historical snapshots to be recalculated.
- **Boundaries:** No degradation detection, RCA, prediction, intervention, or financial forecasting.

### Stage 3 — Degradation Detection (Frozen)

Stage 3 consumes Stage 2 `payment_health_snapshots` and produces `DegradationSignal` and `DegradationEpisode`.

**Important Invariants:**
- Degradation is deterministic rule-based detection, no formal statistical significance testing.
- `absolute_drop = baseline_success_rate - success_rate`
- `relative_drop = (baseline_success_rate - success_rate) / baseline_success_rate`
- Normal baseline rule requires both configured absolute and relative thresholds. Baselines below the configured floor (`MIN_BASELINE_RATE_FOR_RELATIVE_TEST = 0.05`) use only the absolute threshold.
- `LOW_VOLUME` and `NO_BASELINE` are pause signals. They do not trigger degradation or recovery and do not reset pending counters.
- Persistence requires consecutive BAD windows (`REQUIRED_CONSECUTIVE_WINDOWS = 3`).
- Recovery requires consecutive NORMAL windows (`RECOVERY_CONSECUTIVE_WINDOWS = 2`).
- Severity is monotonic (`MODERATE → HIGH → CRITICAL`) and depends only on observed rate degradation.
- `affected_window_count` counts BAD windows only.
- Episode identity uses deterministic UUID5 based on segment identity and episode start window.
- `DegradationSignal` is physically immutable and append-only.
- Signals are versioned using `evaluation_version` via database unique constraints ensuring concurrency safety.
- Historical signal rows are NEVER updated or deleted.
- Latest truth is derived by selecting the highest evaluation version for a logical window.
- Replay appends new signal versions rather than mutating historical signals.
- Episodes are mutable aggregations and may become `ACTIVE`, `RECOVERED`, or `INVALIDATED`.
- Late-event replay reconciles state without destroying audit history (invalidates stale episodes, upserts valid episodes).

### Stage 6 — Intervention Decisioning (Frozen)

Stage 6 consumes outputs from Stages 3, 4, and 5 and deterministically decides among:
- `NO_ACTION`
- `MONITOR`
- `ACT` (selecting exactly one permitted intervention route from the static catalogue)

**Important Invariants & Decisions:**
- **Strict Separation of Probabilities & Confidences:**
  - `failure_probability` = Stage 5 ML prediction outcome in $[0.0, 1.0]$
  - `diagnosis_confidence` = Stage 4 RCA evidence strength (`STRONG`, `MODERATE`, `WEAK`)
  - `decision_confidence` = Stage 6 confidence in its own verdict in $[0.0, 1.0]$, mathematically clamped and bounded
- **Hard Safety Gates:**
  Sequential checks (Kill Switch, Amount/Currency Validity, Pre-Terminal Invariance, Stage 5 Prediction Validity, Episode Invalidation / Traffic Shift, Global Rate Limit) that force an absolute `NO_ACTION` upon failure.
- **Atomic Global Rate-Limit Admission:**
  Rate limit checks and `ACT` insertions are serialized via a dedicated global advisory lock (`GLOBAL_RATE_LIMIT_LOCK`) to prevent concurrency races across competing payment attempts.
- **Monetary Invariant & Policy Utility:**
  `policy_utility` calculation uses `Decimal` fixed-point arithmetic rounded half-even to integer minor units. Utility functions strictly as an internal policy ranking heuristic and viability threshold, never as a financial forecast or `protected_gmv`.
- **Append-Only Persistence & Idempotency:**
  `intervention_decisions` table is immutable and append-only. Idempotency is keyed by SHA-256 fingerprint over evaluation identity (`payment_attempt_id` + $T_{decide}$ + upstream state + policy version). Monotonic versioning per payment attempt.
- **Prohibitions:**
  Zero intervention execution, zero external payment provider API calls, zero outcome observation, zero `protected_gmv` calculation, zero counterfactual attribution, zero LLMs, and zero dynamic route discovery.

### Stage 7 — Intervention Execution + Outcome Observation (Frozen)

Stage 7 operationalizes Stage 6 `ACT` decisions without compromising safety, idempotency, or temporal boundaries:

**Important Invariants & Decisions:**
- **Decoupled Lifecycle Phases:**
  - **Phase 7A: Execution Engine**: `InterventionExecutionService` validates `ACT` verdict, creates deterministic `InterventionCommand` (UUID5 derived from `decision_id`), evaluates pre-execution information barriers, atomically claims the command (`PENDING → EXECUTING`), dispatches via `InterventionExecutor` protocol, and records append-only `ExecutionAttempt` records.
  - **Phase 7B: Outcome Observation**: `PaymentOutcomeObservationService` independently queries Stage 1 canonical payment events strictly as-of an evaluation boundary `as_of_timestamp` to record append-only `PaymentOutcomeObservation` (`CAPTURED`, `FAILED`, `UNKNOWN_IN_FLIGHT`).
- **Command Mutability & Record Semantics:**
  - Command identity and payload are immutable; `command_status`, `status_reason`, and `updated_at` are authoritative mutable lifecycle fields.
  - `intervention_execution_attempts` and `payment_outcome_observations` tables are append-only.
- **Execution Result ≠ Payment Outcome:**
  - Execution success means ONLY that the intervention mechanism executed without error (e.g. gateway switch request acknowledged). It does NOT mean the payment was captured.
  - Payment outcome is factually observed from Stage 1 canonical events, never inferred from execution status.
- **Strict State Machine Semantics:**
  - `PENDING → NOT_NEEDED`: Allowed ONLY when a terminal payment event (`payment.captured` or `payment.failed`) is ingested before claim/dispatch/executor invocation.
  - **No `EXECUTING → NOT_NEEDED`**: Once execution starts, the attempt is factual. If a payment becomes terminal in flight, the attempt status is preserved (`SUCCEEDED` or `FAILED`), and payment outcome is separately observed.
  - **Timeout vs. Expiry**: `EXPIRED` means the command never started execution before its deadline (`now >= expires_at`). In-flight executor timeouts transition the command to `FAILED` with `status_reason="EXECUTOR_TIMEOUT"` and attempt status `TIMEOUT`. Never converted to `EXPIRED`.
- **Temporal Correctness & Information Barriers:**
  - Correctness is governed by information barriers (`ingested_at <= as_of_timestamp`), not by an unconditional universal timeline theorem.
  - Replay and reconciliation can declare older `as_of_timestamp` boundaries.
- **Stage 1 Canonical Semantics & Conflict Handling:**
  - Derives primary terminal reference event using canonical ingestion order (`ingested_at.asc()`, consistent with Stage 5).
  - Preserves all conflicting source facts in `observation_audit_payload` with `conflict_detected = True`.
- **Prohibitions:**
  - Zero live external provider calls in core domain (all adapters conform to `InterventionExecutor`).
  - Zero LLMs.
  - Zero Stage 8 counterfactual attribution, uplift estimation, or `protected_gmv` calculation.

## Strict Stage Boundary

The following are **NOT IMPLEMENTED YET** and must not be added unless the appropriate stage has been explicitly designed and approved:
- Stage 8 counterfactual attribution / recovered & protected GMV measurement
- LLM-based reasoning or generative models

Do not "helpfully" implement future stages early.

## Current State

The repository is currently **Stage 7 complete and frozen**.

- **Branch:** `main`
- **Test Count:** 187 tests passing cleanly.
- **Working Tree:** Clean.

## Next Planned Step

The next task is **Stage 8 — Counterfactual Attribution + Protected GMV design/review.**

## AI Operating Rules

Future AI agents working on this repository must:
- Treat this file as persistent project context, but verify important claims against actual code.
- Inspect the repository before modifying anything.
- Never assume a feature exists merely because it is mentioned in this document.
- Never silently change previously frozen semantics.
- Never implement later-stage functionality without explicit approval.
- Prefer deterministic, auditable behavior.
- Preserve immutable historical evidence.
- Keep financial quantities in integer minor units where applicable.
- Avoid speculative fields and inferred facts.
- Add tests for every meaningful behavior and boundary.
- Run the complete test suite before declaring a stage complete.
- Stop at the requested checkpoint rather than continuing automatically.
- Create a git checkpoint only after explicit approval.

## Documentation Maintenance Rule

Whenever a stage is formally completed and committed:
- Update `PROJECT_CONTEXT.md`.
- Record the exact commit hash and commit message.
- Record the final test result.
- Record important architectural decisions.
- Record what remains intentionally unimplemented.
- Do not rewrite historical decisions without explicitly documenting the change.
