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
- **Stage 7 (`e74785e`)**: Intervention execution + outcome observation.
- **Stage 8**: Counterfactual attribution + protected GMV measurement.

The current repository should be treated as **Stage 8 complete and frozen** unless explicitly instructed otherwise.

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

### Stage 8 — Counterfactual Attribution + Protected GMV (Frozen)

Stage 8 operationalizes the measurement of protected GMV using an auditable, risk-weighted counterfactual model under explicit assumptions:

**Important Invariants & Decisions:**
- **Semantic Separation:**
  - `p0` is the **Stage 5 pre-intervention failure-risk estimate used as the counterfactual risk proxy**. It is NOT an empirically validated causal probability of $Y(0)$.
  - `attribution_confidence` / $\alpha$ is a deterministic **evidence-weighting factor**, NOT a statistical confidence interval.
  - Protected GMV is an **auditable risk-weighted attribution estimate under explicit assumptions**, not experimentally proven causal impact.
- **Strict Inequality Bounding Invariant:**
  - $0 \le \text{attributed\_protected\_gmv} \le \text{counterfactual\_loss\_exposure} \le \text{payment\_amount}$
  - Under no circumstances can protected GMV exceed the counterfactual revenue at risk ($A_i \cdot p_0$).
- **Numeric Typing Rules:**
  - Monetary amounts are strictly integer minor units (`INTEGER` / paise).
  - Probability and confidence are fixed-point `Decimal` backed by PostgreSQL `NUMERIC(6, 4)`.
  - Zero binary floating-point arithmetic is permitted in the attribution calculator.
- **Treatment Prerequisites:**
  - Requires: Decision ACT + Execution SUCCESS + Temporal Precedence ($T_{\text{exec\_completed}} \le T_{\text{terminal\_event}}$).
  - If execution failed, timed out, or completed after terminal outcome: treatment status is not `TREATED`, and protected GMV is strictly 0.
- **Factual Payment Outcome Gate:**
  - Outcome must be factually `CAPTURED`. If `FAILED` or `UNKNOWN_IN_FLIGHT`, protected GMV is strictly 0.
- **Deterministic Evidence Scoring ($\alpha_{\text{confidence}}$):**
  - $\alpha_{\text{confidence}} = c_{\text{prediction}} \times c_{\text{diagnosis}} \times c_{\text{timing}}$.
  - Model provenance is explicitly discounted: `SYNTHETIC_DEVELOPMENT` evaluates at $0.6000$; `EMPIRICAL_PRODUCTION` at $1.0000$.
  - Confidence floor: if $\alpha < 0.2500$, status is `INDETERMINATE_INSUFFICIENT_EVIDENCE` and protected GMV is 0.
- **Append-Only & Versioned:**
  - `counterfactual_attributions` records are append-only. Late-arriving events append a new version without mutating historical versions.
- **Audit Preservation:**
  - Audit payload preserves methodology name/version, Stage 5 model provenance, risk proxy $p_0$, execution evidence, outcome evidence, conflicting events, timing delta, timestamps, and explicit methodology assumptions.
- **Prohibitions:**
  - Zero LLMs, zero new external ML models, zero mutation of Stages 1–7 tables.
  - Stage 8 is the final terminal stage of the approved product flow.

## Approved Post-Stage 8 Extensions

Narrowly scoped changes to frozen stages made after the Stage 8 checkpoint with explicit approval. They are not part of the original Stage 1–8 implementations recorded above.

### M2.1 — Stage 8 Attribution Amount Lookup (2026-10-14, complete)

- **Commit:** `648400ec68561f40d751bc1c8931e6ed53ba5db8` — `fix: align attribution amount with intervention decision`
- **Builds on:** provider-isolation commit `ef926aa7f9bd08c680a3de11dd53c59b7930ebf8`, which introduced the shared constant `NON_RAZORPAY_PIPELINE_SOURCES` in `src/core/domain/source_systems.py`.
- **Problem:** `AttributionRepository.get_canonical_payment_amount` filtered only by `payment_id` and `ingested_at` and took `LIMIT 1` without ordering, so any lifecycle row (capture, refund, order) could supply the attributed payment amount.
- **Approved change:** Stage 8 now uses the same authorization amount Stage 6 used to size the decision:
  - Selects `payment.authorized` events only.
  - Excludes PayPal via `source_system NOT IN NON_RAZORPAY_PIPELINE_SOURCES`.
  - Selects the latest eligible authorization (`ORDER BY ingested_at DESC LIMIT 1`), matching Stage 6 `get_payment_event_by_id`.
  - Lookup cutoff is `min(decision.decided_at, as_of)`: Stage 6's decision cutoff, never reading beyond the attribution `as_of` information barrier.
  - A missing authorized amount still yields `payment_amount = 0` and zero protected GMV, as before.
- **Unchanged:** schema, migrations, API responses, treatment/outcome/timing semantics, and the attribution calculator.
- **Tests:** offline `tests/unit` + `tests/boundaries`: 731 passed, 4 pre-existing httpx deprecation warnings. Database-backed attribution integration tests were skipped (`TEST_DATABASE_URL` not set).
- **Known limitations:**
  - No secondary tie-breaker: authorizations with identical `ingested_at` may resolve to different rows in Stage 6 and Stage 8.
  - Backdated inserts (rows written with an earlier explicit `ingested_at`, e.g. replay or backfill scripts) can change which authorization a historical lookup selects.
  - Currency-safe reporting remains unfinished: attribution records and summary totals carry no currency, and the dashboard formats all amounts as INR.
- **Next planned milestone:** M2.2 — currency-correct API and dashboard.

### P0 — Verified PayPal Webhook Ingestion (2026-10-14, complete)

- **Commit:** `5143a64ae91d4aef2cf7abb6aabe30839037387b` — `feat: add verified PayPal webhook ingestion`
- **Context:** first milestone of the approved PayPal-first direction. Builds on `18282a5dfc35eae7d7f2df7ddfb74fa05f03ffdd` (`feat: add PayPal OAuth client`), `66cedb5dbaf41b0a932b2d4d0c7f9c94c78f8167` (`feat: add PayPal webhook signature verification`) and provider isolation `ef926aa7f9bd08c680a3de11dd53c59b7930ebf8`, which keeps PayPal rows out of Stages 2–5 and the Stage 8 amount lookup.
- **Scope:** intake and persistence only. PayPal events are stored as Stage 1 evidence and canonical events; no Stage 2–8 processing runs for them.
- **Endpoint:** `POST /webhooks/paypal` (`ingest_paypal` in `src/api/routes.py`), served by `PayPalIngestionService` in `src/core/services/paypal_ingestion_service.py`.
- **Request flow:**
  1. The PayPal client dependency is resolved before the database session; if no client is configured the request fails closed with 503 and no session is opened.
  2. The body is read with a 256 KiB limit (`MAX_PAYPAL_WEBHOOK_BODY_BYTES`): a larger declared `Content-Length` is rejected with 413, and streamed bodies are counted and rejected once they exceed the limit.
  3. `PayPalClient.verify_webhook_signature` verifies the exact raw bytes with PayPal. Its only pre-verification step is a JSON syntax check needed to embed the body in the verification request; schema parsing, normalization and persistence happen only after `SUCCESS`.
  4. `parse_paypal_event` → deterministic payload hash → duplicate check → `normalize_paypal_event` → `SQLIngestionRepository.save_ingestion` (raw record and canonical event in one transaction).
- **Client lifecycle:** one application-owned `PayPalClient` per process, built from `PayPalConfig.from_env()` in the FastAPI lifespan (`src/main.py`), stored on `app.state.paypal_client` and closed on shutdown. Missing configuration leaves it `None`; there are no fallback credentials.
- **Hashing:** `generate_deterministic_hash` moved verbatim to `src/core/normalizers/hashing.py` (SHA-256 over sorted-key, compact JSON of the parsed payload, so formatting-only differences hash equally). `src/core/normalizers/razorpay.py` re-exports it for existing callers.
- **Deduplication:** identity is `(source_system, source_event_id)` = `("paypal", PayPal event id)`, enforced by the existing unique constraints.
  - Same id, same hash: acknowledged as `duplicate` (200); nothing is written.
  - Same id, different hash: 409; the stored evidence is never overwritten.
  - Concurrent deliveries: if the pre-insert check passes for both, the loser's unique violation is rolled back inside `save_ingestion` before `DuplicateEventConflictError` is raised; the service then re-reads the stored raw record and returns `duplicate` (same hash) or 409 (different hash). If no raw record is found the conflict is re-raised.
- **HTTP status mapping:**
  - 200 `stored` / `duplicate` / `ignored` (verified but unsupported event type; acknowledged so PayPal stops retrying, not persisted).
  - 400: malformed body (invalid JSON, NaN/Infinity, non-object, too deeply nested — `RecursionError` is mapped to `MalformedInputError`), invalid `Content-Length`, or PayPal rejecting the verification request (HTTP 400, often a `webhook_id` mismatch; logged as an error).
  - 401: missing/conflicting transmission headers or verification `FAILURE`.
  - 413: body over 256 KiB.
  - 422: verified supported event that fails schema validation or normalization (not persisted; PayPal retries non-2xx responses for up to about 3 days).
  - 409: conflicting duplicate.
  - 503: missing configuration or `webhook_id`, credentials rejected, PayPal timeouts/outages/malformed responses.
  - 500: persistence or unexpected errors.
  - All response bodies are fixed generic strings; no credentials, tokens, signatures, payloads or exception text are returned.
- **Logging:** the `PersistenceError` handler and the catch-all handler log a fixed message plus the exception class only, with no exception message and no traceback, because chained SQLAlchemy errors can include bound SQL parameters (the raw payload).
- **No downstream processing:** the route never calls `run_live_pipeline` and schedules no background tasks.
- **Unchanged:** the Razorpay route and ingestion service, `SQLIngestionRepository`, schema and migrations — apart from the hash extraction and re-export above.
- **Tests:**
  - New: `tests/unit/test_paypal_ingestion_service.py` and `tests/api/test_paypal_webhook_route.py`, plus one test in `tests/unit/test_paypal_webhook_verification.py`. All offline (`httpx.MockTransport` and in-memory or fake-session repositories).
  - Regression tests: deeply nested JSON at the client, service and route boundaries (route returns 400 with no PayPal request, repository call or pipeline run); a `PersistenceError` sentinel and a real `SQLIngestionRepository` over a fake session whose commit raises a sentinel-bearing `IntegrityError` and whose rollback then fails — the sentinel must not appear in logs. Both were confirmed to fail against the pre-fix code.
  - Results: PayPal client, verification, ingestion-service, parser, normalizer and route tests: 383 passed. `tests/unit` + `tests/boundaries`: 769 passed, 4 pre-existing httpx deprecation warnings. `tests/api`: 30 passed, 21 skipped (require a database). `git diff --check`: clean.
- **Not validated / known limitations:**
  - Real PostgreSQL integration was not tested, including the concurrent unique-violation → rollback → re-read path and asyncpg constraint-name matching.
  - PayPal Sandbox verification was not tested.
  - Raw request bytes are not archived, so stored events cannot be re-verified later; only the parsed JSON is kept.
  - Whether 422 (days of PayPal retries) or acknowledge-and-quarantine is the right policy for verified but unprocessable events is undecided.
  - M2.2 recorded above remains unimplemented.
- **Next planned milestone:** P1 — PayPal lifecycle identity and event semantics (not implemented).
  - Goal: correlate order approval, capture success, capture decline and refunds correctly using PayPal order identity, without treating these events as interchangeable payment outcomes.
  - An approved order is not a completed capture.
  - A refund is not a failed payment or a recovered payment.
  - Downstream pipeline activation for PayPal events is a separate, later milestone.

### P1a — PayPal Lifecycle Semantics Contract (2026-10-14, complete)

- **Commit:** `6918168ce0e4e2895e3d7d2bb5767d9ffe612e88` — `feat: define PayPal lifecycle semantics`
- **Files:** `src/core/domain/paypal_lifecycle.py` (new), `tests/unit/test_paypal_lifecycle.py` (new).
- **Scope:** pure lifecycle classification and helper functions over canonical PayPal `PaymentEvent`s. No I/O, schema changes, migrations, provider-filter changes, Stage 2–8 behavior changes or pipeline activation. The module is not imported by any application code.
- **Lifecycle semantics (keyed by canonical `event_type`):**
  - `order.approved` — order consent (`ORDER_CONSENT`). A candidate amount for possible future pre-outcome sizing; not a capture and not a terminal outcome. Being a sizing candidate does not mean the order was captured, successful or recovered.
  - `order.completed` — informational (`ORDER_INFORMATIONAL`); not proof of a capture and never counted as captured GMV.
  - `payment.pending` — non-terminal capture state (`CAPTURE_PENDING`).
  - `payment.captured` — successful capture (`CAPTURE_SUCCEEDED`, outcome `CAPTURED`); contributes to gross captured GMV.
  - `payment.failed` — failed capture (`CAPTURE_FAILED`, outcome `FAILED`).
  - `payment.refunded` — a separate post-settlement adjustment (`REFUND`). It does not turn the original capture into a failure, and it is not terminal, not a sizing input and not gross captured GMV.
  - Unknown event types and non-PayPal events raise `PayPalLifecycleError` (fail closed).
- **Outcomes:**
  - Capture outcomes are per capture; captured GMV is gross (refunds are not netted out).
  - The order-level outcome when a decline is followed by a successful capture remains **undecided**. No order-level outcome resolver exists; this must be resolved in a future lifecycle-aware pipeline milestone.
- **Identity and correlation:**
  - Existing canonical identifiers and persisted values are unchanged: `source_event_id` remains the webhook-deduplication identity; `payment_id` remains the order id for order events, the capture id for capture events and the refunded capture's id for refunds.
  - `order_id` is the lifecycle correlation key only when the payload provides it. Events without an explicit `order_id` remain stored but are classified `NOT_CORRELATABLE`; relationships are never inferred from other identifiers.
  - The refund's own resource id is kept only in the raw payload, so distinct refunds of one capture are distinguishable only by webhook event id.
- **Tests:** lifecycle, PayPal parser, PayPal normalizer and provider-isolation tests: 234 passed offline, with `TEST_DATABASE_URL` unset and `DATABASE_URL` pointed at an unreachable localhost address:
  `env -u TEST_DATABASE_URL DATABASE_URL="postgresql+asyncpg://nobody:nobody@127.0.0.1:1/unreachable_db" .venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_paypal_lifecycle.py tests/unit/test_paypal_parser.py tests/unit/test_paypal_normalizer.py tests/unit/test_provider_isolation.py`
- **Not validated:** no real PostgreSQL or PayPal Sandbox validation was performed.
- **Remaining work (not implemented):**
  - **P1b:** decide whether to add an optional, nullable `provider_resource_id` column and migration (e.g. to store refund resource ids canonically).
  - **P1c:** provider-isolation regression tests only, without changing frozen Stage 2–8 behavior. Stage 6–7 terminal-event queries (`intervention_repository.check_terminal_event_exists`, `execution_repository`) do not filter by source system; any change to their query behavior needs separate explicit approval.
  - PayPal remains excluded from the existing downstream pipeline via `NON_RAZORPAY_PIPELINE_SOURCES`.
  - Reporting issues (e.g. the inspection dashboard summary counts `payment.captured` across all providers) and full PayPal pipeline activation remain separate work.

## Current State

The repository is currently **Stage 8 complete and frozen**.

- **Branch:** `main`
- **Test Count:** 210 tests passing cleanly.
- **Working Tree:** Clean.

## Terminal Status

Stages 1–8 of the Revenue Protection & Recovery Engine are complete, verified, and frozen.

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
