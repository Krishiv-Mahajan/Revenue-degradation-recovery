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

### P1c — PayPal Cross-Provider Isolation Regression Tests (2026-10-15, complete)

- **Commit:** `9ba75f7b6c1b00a3aea6b7cfad79dcf02aed0c5a` — `test: cover PayPal cross-provider isolation risks`
- **File:** `tests/unit/test_paypal_cross_provider_isolation.py` (new).
- **Scope:** regression and characterization tests only. No production code, schema, migration, provider-filter, pipeline or dashboard implementation changes, and no PayPal pipeline activation.
- **Method:** each repository/service query runs against a mocked session; the captured statement's WHERE conditions are evaluated against canonical rows by a test-only evaluator that follows SQL three-valued logic (UNKNOWN does not match) and fails on unsupported condition shapes or unbound parameters. Rows come from the real PayPal parser/normalizer and, for controls, realistic Razorpay webhooks through the real Razorpay parser/normalizer. Synthetic minimal-difference controls (a PayPal-derived row with only `source_system` changed) are used separately to isolate the provider predicate and are not treated as realistic Razorpay events.
- **Existing protections confirmed:**
  - Stage 5 prediction eligibility (`is_eligible_for_prediction`) and terminal labels (`lookup_terminal_label`) exclude PayPal rows, including when `payment_id` overlaps, through the existing `NON_RAZORPAY_PIPELINE_SOURCES` filter and event-type constraints.
  - Stage 6 sizing (`get_payment_event_by_id`) and the Stage 8 attribution amount lookup (`get_canonical_payment_amount`) never select real PayPal events, and exclude even a synthetic PayPal-sourced `payment.authorized` row by provider alone.
  - The live prediction branch in `run_live_pipeline` is entered for `payment.authorized` events: a realistic Razorpay authorization enters it, and none of the six real PayPal canonical events does. The trigger checks event type only, not provider; PayPal stays out because its normalizer never emits `payment.authorized` and the PayPal webhook route never calls `run_live_pipeline`.
- **Confirmed gaps (characterized, not fixed):**
  1. **Stage 6/7 terminal-event queries** — `intervention_repository.check_terminal_event_exists`, `execution_repository.check_terminal_event_exists_before` and `execution_repository.get_canonical_terminal_events` — have no provider predicate (only `payment_id`, `event_type`, `ingested_at`). A real PayPal capture or decline whose `payment_id` overlaps a Razorpay attempt's matches these conditions exactly like the Razorpay terminal event. Today isolation relies only on PayPal capture ids and Razorpay payment ids differing; no schema constraint enforces this.
     - *Code-review inference, not exercised by the tests:* Stage 6 would then treat the attempt as already terminal (pre-terminal gate), Stage 7 would observe the PayPal outcome as `CAPTURED`/`FAILED`, and Stage 8 would attribute from that observation.
  2. **Dashboard `/summary`** (`get_executive_summary` in `src/api/inspection_routes.py`) counts events by type across all providers (the event-count statement has no WHERE clause). PayPal captures inflate `total_captures`, and `overall_success_rate` can exceed 1.0 (2.0 in the test with one Razorpay authorization, one Razorpay capture and one PayPal capture). `total_gmv_minor_units` is unaffected only because PayPal never emits `payment.authorized`.
- **Validation (offline only: `TEST_DATABASE_URL` unset, `DATABASE_URL` pointed at unreachable localhost port 1):**
  - New regression test file: 73 passed on two consecutive runs.
  - Related provider-isolation, lifecycle, parser, normalizer, attribution, intervention, execution, live-pipeline and boundary tests: 542 passed, 3 warnings.
  - `tests/unit` + `tests/boundaries`: 877 passed, 4 warnings (the pre-existing httpx deprecation warning).
  - `tests/api`: 30 passed, 21 skipped (require a database).
  - Sensitivity checks (in memory, no files changed): disabling the provider filter made the filter-dependent tests fail, and simulating a provider predicate in the Stage 6/7 queries made the corresponding characterization tests fail, as expected.
  - No PostgreSQL, Docker or PayPal Sandbox validation.
- **Limitations:**
  - The SQL evaluator supports only the documented subset of conditions (AND of comparisons, `IN`/`NOT IN` against the `payment_events` table) and ignores `ORDER BY` and `LIMIT`; it shows whether a row matches, not which row a query would select.
  - Live-pipeline downstream services are stubbed; routing is tested, not full pipeline execution.
  - Dashboard aggregation (`COUNT ... GROUP BY`, `SUM`) is emulated in Python, not verified against PostgreSQL; the expected counts are calculated manually from the fixtures.
  - The characterization tests pin current behavior and must be deliberately revised when an approved production fix changes it.
- **Remaining work:**
  - Stage 6/7 provider-isolation production fix: not implemented.
  - Dashboard provider-isolation / reporting fix: not implemented.
  - P1b optional `provider_resource_id` schema decision: not started.
  - PayPal pipeline activation: not started.
  - Each production fix requires separate explicit approval.

### Stage 6/7 Terminal-Event Provider Isolation (2026-10-17, complete)

- **Commit:** `99e0d1fd520f5c3d37fff3daddeca531da95754e` — `fix: isolate Stage 6/7 terminal-event lookups from PayPal events`
- **Builds on:** P1c (`9ba75f7b6c1b00a3aea6b7cfad79dcf02aed0c5a`). Closes P1c confirmed gap 1; the P1c section above is kept as the historical record of the pre-fix behavior.
- **Problem:** the three Stage 6/7 terminal-event queries filtered only by `payment_id`, `event_type IN ('payment.captured', 'payment.failed')` and `ingested_at`. The PayPal normalizer emits the same two event types, so a PayPal capture or decline whose `payment_id` overlapped a Razorpay attempt's matched exactly like a Razorpay terminal event.
- **Approved change:** each query now also requires `source_system NOT IN NON_RAZORPAY_PIPELINE_SOURCES`, using the same shared constant and patterns as Stage 5:
  - `InterventionRepository.check_terminal_event_exists` (raw SQL, expanding `excluded_sources` bind parameter).
  - `ExecutionRepository.check_terminal_event_exists_before` (raw SQL, expanding `excluded_sources` bind parameter).
  - `ExecutionRepository.get_canonical_terminal_events` (ORM `source_system.notin_(...)`).
- **Behavior change:** PayPal rows can no longer:
  - make Stage 6 treat a Razorpay attempt as already terminal;
  - move a Stage 7 command from `PENDING` to `NOT_NEEDED` (`TERMINAL_OUTCOME_ALREADY_INGESTED`);
  - become the Stage 7 observed outcome, or add a conflicting event type that sets `conflict_detected`;
  - reach Stage 8, which reads only the Stage 7 observation and so needed no code change.
- **Unchanged:** event types, temporal boundaries (`< t_decide` in Stage 6, `<= as_of` in Stage 7), `ORDER BY ingested_at ASC`, parameter names, return types, Razorpay behavior, and `synthetic` / `test_system` sources, which are not in the exclusion list. No schema, migration, dashboard, API or PayPal pipeline changes.
- **Decisions:**
  - Exclusion list (`NOT IN NON_RAZORPAY_PIPELINE_SOURCES`) rather than a Razorpay-only filter, matching existing convention and preserving synthetic and test-system fixtures.
  - **No backfill:** existing append-only commands, observations and attributions are not re-evaluated or corrected.
- **Tests:**
  - `tests/unit/test_paypal_cross_provider_isolation.py`:
    - The Stage 6/7 known-gap characterization tests were replaced with guarantees: the terminal lookups filter on `source_system`, and an overlapping PayPal terminal row does not match while the realistic Razorpay row and the synthetic source-swapped twin do.
    - The identifier-disjointness test was renamed `test_terminal_lookups_do_not_match_non_overlapping_paypal_captures`.
    - New: `test_canonical_terminal_events_drop_overlapping_paypal_and_keep_ingested_at_order` and `test_paypal_terminal_events_do_not_create_razorpay_outcome_conflicts`.
  - `tests/unit/test_provider_isolation.py`: the three queries were added to `CASES`, covering the bound exclusion parameter, shared-constant use, preserved predicates including `ORDER BY` and the module import. Two raw-SQL parameter tests were also added.
- **Validation (offline only: `TEST_DATABASE_URL` unset, `DATABASE_URL` pointed at unreachable localhost port 1):**
  - Updated tests before the fix: 21 failed, 106 passed; every failure was a new or rewritten assertion.
  - Focused files after the fix: 127 passed on two consecutive runs.
  - Related intervention, execution, observation, attribution, live-pipeline and PayPal parser/normalizer/lifecycle tests: 262 passed, 3 warnings.
  - `tests/unit` + `tests/boundaries`: 889 passed, 4 warnings (the pre-existing httpx deprecation warning).
  - `tests/api`: 30 passed, 21 skipped (require a database).
- **Not validated:**
  - PostgreSQL has not executed the new filters. Database-backed tests skipped (30, `TEST_DATABASE_URL` not set): `tests/integration/test_intervention_temporal.py`, `test_execution_temporal.py`, `test_execution_lifecycle.py`, `test_intervention_lifecycle.py`, `test_boundary_stage5_stage6.py`, `test_attribution_lifecycle.py`, `test_attribution_temporal.py`, `test_execution_persistence.py` and `test_execution_concurrency.py`.
  - No Docker or PayPal Sandbox validation.
- **Known limitations:**
  - No backfill of existing append-only records (see Decisions).
  - Tests evaluate the query filters against in-memory rows; the ordering test reproduces `ORDER BY ingested_at ASC` in Python after asserting it is the statement's final clause.
  - A new provider is included in these lookups by default until it is added to `NON_RAZORPAY_PIPELINE_SOURCES`.
  - Unchanged and out of scope: terminal events with identical `ingested_at` have no secondary tie-breaker, and the terminal branch of `run_live_pipeline` checks event type, not provider (the PayPal route never calls it).
- **Remaining work:**
  - Dashboard provider-isolation / reporting fix: not implemented.
  - P1b optional `provider_resource_id` schema decision: not started.
  - PayPal pipeline activation: not started.
  - Each production fix requires separate explicit approval.

### Razorpay-Only Dashboard Provider Isolation (2026-10-17, complete)

- **Commit:** `c24f549057855c7cbb09e9966208fbc94de80cc0` — `fix: exclude PayPal events from Razorpay dashboard reporting`
- **Builds on:** the Stage 6/7 fix (`99e0d1fd520f5c3d37fff3daddeca531da95754e`). Closes P1c confirmed gap 2 (dashboard `/summary`) and the dashboard "Remaining work" item above; the earlier sections are kept as the historical record.
- **Scope decision:** the inspection dashboard is Razorpay-only for now. PayPal ingestion stays enabled, but PayPal events must not appear in dashboard reporting.
- **Problem:** the three dashboard endpoints that read `payment_events` directly (`src/api/inspection_routes.py`) had no provider filter:
  - `/summary` counted PayPal `payment.captured` events in `total_captures` and `overall_success_rate` (which could exceed 100%).
  - `/payments` (Payment Audit Inspector) listed PayPal payments alongside Razorpay ones, counted them in `total_count` and status filters, and, for an overlapping `payment_id`, merged their terminal status and could take the row's amount and currency from a PayPal event.
  - `/timeline/{payment_id}` returned PayPal events, and for an overlapping `payment_id` could take `payment_info` and `terminal_status` from them.
- **Approved change:** each read now also requires `source_system NOT IN NON_RAZORPAY_PIPELINE_SOURCES` (ORM `source_system.notin_(...)`, using the shared constant from `src/core/domain/source_systems.py`):
  - `/summary`: both `payment_events` statements — the authorization-volume sum and the grouped event count.
  - `/payments`: the terminal-status subquery and the canonical-event (`DISTINCT ON (payment_id)`) subquery. The list and `total_count` both select rows through the canonical-event subquery, so PayPal rows are excluded from items, counts, search and status-filtered results.
  - `/timeline/{payment_id}`: the raw events query. The Stage 5–8 lookups are unchanged; they key on the requested Razorpay payment id, and only the Razorpay pipeline writes those records.
- **Behavior change:** PayPal-only payment ids now return 404 (`Payment {payment_id} not found`) from `/timeline`; the 404 response itself is unchanged.
- **Unchanged:** response fields, ordering, pagination, status semantics and the other dashboard endpoints (`/episodes`, `/rca`, `/recovery`, `/attributions`), which read only Stage 3–8 tables populated by the provider-isolated pipeline. No schema, migration, frontend or Stage 2–8 changes.
- **Tests:**
  - `tests/unit/test_paypal_cross_provider_isolation.py`:
    - The `/summary` known-gap characterization was replaced with `test_dashboard_summary_ignores_paypal_events` (adding all six PayPal event types and an overlapping PayPal capture leaves the summary identical) and `test_dashboard_summary_excludes_paypal_by_provider_alone`.
    - New timeline tests over an emulated session: `test_timeline_ignores_overlapping_paypal_events_and_keeps_the_razorpay_trace` and `test_timeline_returns_404_for_a_paypal_only_payment_id`.
  - `tests/unit/test_provider_isolation.py` (compiled PostgreSQL SQL): `/timeline` added to `CASES`; both `/summary` statements; both `/payments` subqueries for every list/count variant (default, three status filters, search, both lifecycle filters); shared-constant use; and preserved ordering, `LIMIT`/`OFFSET` and status lowercasing.
  - `tests/api/test_inspection_routes.py` (database-backed): `test_summary_excludes_paypal_events`, `test_payments_list_excludes_paypal_events` and `test_timeline_excludes_paypal_events`.
- **Validation (offline only: `TEST_DATABASE_URL` unset, `DATABASE_URL` pointed at unreachable localhost port 1):**
  - Updated P1c, provider-isolation and inspection-route test files before the fix: 17 failed, 128 passed, 23 skipped; every failure was a new or rewritten assertion.
  - Same files after the fix: 145 passed, 23 skipped on two consecutive runs.
  - `tests/unit` + `tests/boundaries`: 906 passed, 4 warnings (the pre-existing httpx deprecation warning).
  - `tests/api`: 30 passed, 24 skipped (require a database).
  - Related live-pipeline, PayPal parser/normalizer/lifecycle/ingestion, attribution-amount and outcome-observation tests: 256 passed, 3 warnings.
- **Not validated:**
  - The three new database-backed API tests above have not run against PostgreSQL (skipped, `TEST_DATABASE_URL` not set), nor have the other database-backed `tests/api` tests or the `tests/integration` suites.
  - PostgreSQL has not executed `DISTINCT ON (payment_id)` combined with the new `source_system` filter in the `/payments` canonical-event subquery.
  - No Docker or PayPal Sandbox validation.
- **Known limitations:**
  - Currency formatting remains INR-oriented: the frontend formats all amounts as INR (M2.2, unchanged).
  - No backfill of existing records.
  - Offline tests evaluate filters against in-memory rows or compiled SQL; they are not PostgreSQL validation.
- **Remaining work:**
  - Currency-correct API and dashboard (M2.2): not implemented.
  - P1b optional `provider_resource_id` schema decision: not started.
  - PayPal pipeline activation: not started.
  - Each production fix requires separate explicit approval.

### Event and Attribution Provenance (2026-10-18, complete)

- **Commit:** `6648909c8dfae0e74e42b4e0ec2bdb296a3dd7fa` — `fix: correct event and attribution provenance`
- **Problems:**
  - `scripts/generate_live_context.py` wrote its generated events as `source_system="razorpay"`, making them indistinguishable from genuine Razorpay events.
  - Stage 8 classified any Stage 5 model whose name lacked "synthetic" as `EMPIRICAL_PRODUCTION`, so the live and demo `DeterministicBaselineModel` heuristic received full empirical weight.
- **Approved changes:**
  - The live-context generator labels every raw and canonical event it writes `source_system="synthetic"` (`GENERATED_SOURCE_SYSTEM`), and its lookback check counts synthetic traffic.
  - Stage 8 provenance comes from `classify_model_provenance(model_name, model_version)` in `src/core/attribution/attribution_service.py`:
    - names in `DEVELOPMENT_MODEL_NAMES` (`DeterministicBaselineModel`) or containing "synthetic" → `SYNTHETIC_DEVELOPMENT`;
    - an exact `(model_name, model_version)` pair in `VERIFIED_EMPIRICAL_MODELS` → `EMPIRICAL_PRODUCTION`;
    - anything else → `UNKNOWN`.
  - `VERIFIED_EMPIRICAL_MODELS` is empty: no Stage 5 model is registered as empirically validated. Adding an entry requires an explicitly reviewed validation.
  - `is_synthetic_baseline` is now true whenever provenance is not `EMPIRICAL_PRODUCTION`.
- **Behavior change:**
  - Provenance weights are unchanged (`EMPIRICAL_PRODUCTION` 1.0, `SYNTHETIC_DEVELOPMENT` 0.6, `UNKNOWN` 0.3). Attribution confidence is `c_provenance × c_diagnosis × c_timing`, attributions below the 0.25 floor become `INDETERMINATE_INSUFFICIENT_EVIDENCE`, and protected GMV is `amount × p0 × confidence`, capped at the loss exposure.
  - Future attributions from the deterministic heuristic therefore carry provenance weight 0.6 instead of 1.0: about 40% lower confidence and protected GMV, and some fall below the floor.
  - Live Razorpay payments no longer count generated live-context events in their Stage 5 global failure rate, because that rate is keyed by the payment's own `source_system`. The live-context demo's prediction for its target payment drops from 0.925 (ACT) to 0.55 (MONITOR), computed offline with the deterministic model. Degradation and RCA context from the generated traffic still apply.
- **Unchanged:** provenance weights, the confidence floor and attribution formula, Stage 2–7 behavior, schema and migrations. No backfill: rows written earlier by the generator stay labelled `razorpay` (identifiable by `pay_live_ctx_`, `order_live_ctx_`, `evt_live_auth_` and `evt_live_term_` ids), and earlier attributions keep their recorded provenance.
- **Tests:** `tests/unit/test_attribution_provenance.py` (new) and `tests/unit/test_live_context_generator.py` (updated).
- **Validation (offline only: `TEST_DATABASE_URL` unset, `DATABASE_URL` pointed at unreachable localhost port 1):** verified independently on HEAD plus only these four files: 43 focused attribution and generator tests passed; `tests/unit` + `tests/boundaries`: 919 passed, 4 warnings (the existing httpx deprecation warning).
- **Not validated:** `tests/integration/test_attribution_lifecycle.py` and `test_attribution_temporal.py` (the real attribution service against PostgreSQL) have not run.

### Stage 5 Provider-Evaluation Mode (2026-10-18, complete; not production-validated)

- **Commit:** `383c0b9deda55f82be4616613f9bcf3935a28ff5` — `feat: add opt-in Stage 5 provider-evaluation mode with fingerprinted provider models`
- **Builds on:** the provenance commit above (`6648909c8dfae0e74e42b4e0ec2bdb296a3dd7fa`).
- **Purpose:** an opt-in, offline training/evaluation path for Stage 5 that uses genuine Razorpay events only and refuses data that generated events could have shaped. It does not change live predictions, Stages 6–8, or PayPal handling, and it does not implement PayPal API integration or production intervention.
- **Populations** (`TrainingPopulation` in `src/core/ml/training.py`, chosen when constructing `Stage5PipelineValidator`):
  - `SYNTHETIC_DEVELOPMENT` (default): today's extract over every non-PayPal source, including synthetic and test data. Its SQL, its behavior and `SyntheticLogisticRegressionModel` output are unchanged.
  - `PROVIDER_EVALUATION` (opt-in): samples and labels require `source_system = 'razorpay'` (in addition to the PayPal exclusion), with `event_id` breaking `ingested_at` ties in both queries.
- **Entry point:** none. `scripts/train_synthetic_model.py` and every other caller use the default mode; the live pipeline does not use the training path.
- **Legacy live-context detection:** `is_legacy_live_context_event()` matches only the generator's full id pattern with one shared token: `pay_live_ctx_` plus 12 lowercase hex digits, `order_live_ctx_` plus the same token, and `evt_live_auth_` or `evt_live_term_` plus the same token. Matching samples are dropped. There is no `raw_payload` check.
- **Contamination guard** (`assert_evaluation_window_uncontaminated`):
  - It runs before any label or feature query, over [earliest sample − `CONTAMINATION_LOOKBACK`, latest sample + `LABEL_HORIZON`]. `CONTAMINATION_LOOKBACK` is 4 weeks + 24 hours: the Stage 2 baseline and Stage 4 RCA history plus the 24-hour failure-rate features. `LABEL_HORIZON` is 30 minutes.
  - It refuses (`EvaluationDataContaminationError`, counts only) if the window, by `timestamp` or `ingested_at`, holds any event whose source is neither `razorpay` nor `paypal`, or any legacy live-context event. Legacy candidates are pre-filtered with an escaped `LIKE` prefix match and confirmed by the full id check.
  - It refuses rather than filtering because snapshots, episodes and RCA records carry no source.
- **Consistent snapshot:**
  - Provider `extract_dataset` refuses a session already in a transaction, begins one with `isolation_level="REPEATABLE READ"`, and requires `SHOW transaction_isolation` to report `repeatable read` or `serializable` before any data query (`EvaluationIsolationError` otherwise).
  - Sample selection, the guard, labels and feature reconstruction all use that one session and transaction. Afterwards, the same root transaction must still be current and active, or the extraction fails.
  - In provider mode, `get_authorized_events`, `get_terminal_label` and the guard refuse to run outside that snapshot, including before and after extraction. Default mode does not check.
- **Checked-dataset binding:**
  - Each provider extraction clears any previous checked state. It records the guarded window and a dataset fingerprint only after the guard, labels, features and the final transaction check all succeed.
  - In provider mode, `run_pipeline` refuses without a successful check, with no samples, or when its inputs' fingerprint differs from the checked one.
- **Dataset fingerprint** (`compute_dataset_fingerprint`):
  - SHA-256 over the ordered samples: every `FeatureSnapshot` field (key order ignored), the label and the sample time normalized to UTC. Timezone-naive times are rejected, and sample order matters.
  - It excludes training code, hyperparameters, library versions, artifact bytes and payment ids, so identical data trained differently yields the same fingerprint.
- **Provider model identity:**
  - Provider training returns `ProviderLogisticRegressionModel` (`src/core/ml/model.py`), a sibling of `SyntheticLogisticRegressionModel`, not a subclass.
  - Its version is `provider-eval-` plus the first 12 hex characters of the fingerprint, validated on construction, with status "PROVIDER-TRAINED — NOT VALIDATED".
  - Its metadata records the population, sample window, sample count, metrics and `metrics_computed`, the contamination window and lookback, the label horizon and the full fingerprint. Metadata is evidence for review, not proof of validation.
  - Stage 8 classifies it as `UNKNOWN` unless its exact name/version pair is registered in `VERIFIED_EMPIRICAL_MODELS` (empty).
  - `load_synthetic_development_model` rejects provider artifacts.
- **Compatibility:**
  - `SyntheticLogisticRegressionModel` keeps its class path, constructor and identity. Its prediction now goes through a shared helper that is behaviorally identical (parity-tested against the original implementation).
  - New class-level defaults (`training_population = "SYNTHETIC_DEVELOPMENT"`, `training_metadata = None`) let the committed `models/synthetic-development-v1.joblib` load unchanged.
- **Tests:** `tests/unit/test_training_provider_evaluation.py` and `tests/unit/test_provider_model_provenance.py` (new, 104 tests), all using mocked sessions and in-memory datasets.
- **Validation (offline only, same environment):**
  - New provider tests: 104 passed (repeated).
  - `tests/unit` + `tests/boundaries`: 1,025 passed, 4 warnings (the existing httpx deprecation warning).
  - `tests/api`: 30 passed, 24 skipped (require a database).
  - `tests/integration`: 60 skipped (require a database).
  - These offline results do not replace database-backed validation.
- **Not validated:**
  - No provider evaluation has been run against PostgreSQL. Unverified: asyncpg applying `REPEATABLE READ` through `session.connection`, the `SHOW` result, the snapshot covering every statement, transaction identity across statements, isolation reset when connections return to the pool, the escaped `LIKE`, and guard query plans over 4+ weeks of data.
  - The database-backed default training test (`test_synthetic_training_pipeline.py`) and feature-reconstruction tests have not run.
  - Provider metrics are not validated production metrics.
- **Safety requirements and known limitations:**
  - **Transaction cleanup belongs to the caller.** The validator never commits or rolls back: a successful extraction, and every failure after the transaction begins, leave it open. Callers must use a fresh session per evaluation (e.g. `async with Session() as session:`) or explicitly roll back/close after success or failure. Reusing a session with an open transaction is refused.
  - The contamination guard inspects `payment_events` only. It cannot see long-running episodes that began before the window, or fabricated Stage 2 snapshots with no corresponding events (`generate_live_context.py` writes baseline snapshots this way). Provider evaluation should run against a database that has never held generated data.
  - Model identity does not cover training code, hyperparameters or artifact bytes. A registry entry is not pinned to an artifact hash, and `joblib.load` executes the pickle.
  - Frozen Stage 5 feature queries (`get_payment_event_by_id`; step 2 of `get_active_episode_context`) can return tied rows in varying order. Duplicate `payment.authorized` events for one payment become duplicate samples, as in default mode.
  - `UNKNOWN` (0.3) is weighted below `SYNTHETIC_DEVELOPMENT` (0.6). Read-only is not enforced. Provider mode still logs "SYNTHETIC / DEVELOPMENT".
  - No LLM participates in core financial calculations, policy gates, provenance classification or provider evaluation.
- **Remaining work:**
  - A provider-evaluation entry point (fresh session per run, explicit cleanup).
  - Database-backed validation against an isolated PostgreSQL test database, and a provider-evaluation smoke test.
  - Optional: pin registry entries to artifact hashes.
  - Each production change requires separate explicit approval.

## Current State

The repository is currently **Stage 8 complete and frozen**, with the approved post-Stage 8 extensions recorded above.

- **Branch:** `main`
- **Test Count (offline, as of `383c0b9deda55f82be4616613f9bcf3935a28ff5`):** `tests/unit` + `tests/boundaries`: 1,025 passed; `tests/api`: 30 passed, 24 skipped; `tests/integration`: 60 skipped (database required). Database-backed tests have not been run for the recent extensions.
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
