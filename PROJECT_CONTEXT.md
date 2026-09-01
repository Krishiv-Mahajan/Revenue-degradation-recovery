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

The current repository should be treated as **Stage 3 complete and frozen** unless explicitly instructed otherwise.

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

## Strict Stage Boundary

The following are **NOT IMPLEMENTED YET** and must not be added unless the appropriate stage has been explicitly designed and approved:
- Stage 4 RCA / root-cause analysis
- Stage 5 payment failure prediction
- Stage 6 intervention decisioning
- Stage 7 intervention execution
- Stage 8 counterfactual attribution / recovery measurement
- LLM-based reasoning
- revenue-at-risk calculations
- recoverable GMV
- expected saved GMV
- protected GMV

Do not "helpfully" implement future stages early.

## Current State

The repository is currently **Stage 3 complete and frozen**.

- **Branch:** `main`
- **Latest Commit:** `ed8ea3f` (feat: implement stage 3 degradation detection)
- **Test Count:** 39 tests passing cleanly.
- **Working Tree:** Clean.

## Next Planned Step

The next task is **Stage 4 — Root Cause Analysis design/review.**

The next AI must **NOT immediately implement Stage 4**.
First:
1. Inspect `PROJECT_CONTEXT.md`.
2. Inspect the actual repository.
3. Verify Stage 1–3 checkpoints.
4. Review the existing architecture and interfaces.
5. Produce a Stage 4 design.
6. Explicitly identify ambiguities, schema requirements, financial semantics, replay implications, and stage-boundary risks.
7. Wait for explicit approval before implementation.

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
