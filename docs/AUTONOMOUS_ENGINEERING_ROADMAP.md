# Revenue Guardian — Autonomous Engineering Roadmap

Owner: principal engineer (AI agent). Started 2026-11-03 at commit `5c601c4`.
This document tracks the work that takes the engine from its audited state to a complete, demonstrable
payment recovery system. `PROJECT_CONTEXT.md` remains the historical record of frozen stages; this file is
the forward-looking plan and progress log. Every status below is backed by code and tests that ran, and
anything not verified is marked as such.

## 1. Verified starting state (2026-11-03, `5c601c4`, CI run 41667680881 green)

| Area | State | Evidence |
|---|---|---|
| Stage 1 ingestion | Real: Razorpay HMAC webhooks and PayPal verify-webhook-signature ingestion, deduplicated by `(source_system, source_event_id)` | `src/api/routes.py`, ingestion services, unit and API tests |
| Stage 2 analytics | Real, Razorpay/synthetic only: 5-minute windows, baseline = median of 4 same-weekday windows | `src/core/analytics/` |
| Stage 3 degradation | Real, threshold rule (absolute and relative drop) with hysteresis (3 BAD to enter, 2 NORMAL to exit); **no Z-score** | `src/core/degradation/` |
| Stage 4 RCA | Real, deterministic excess-failure contribution | `src/core/rca/` |
| Stage 5 prediction | Real persistence; live model is `DeterministicBaselineModel` (heuristic); logistic models exist only offline | `src/core/ml/model.py` |
| Stage 6 decisioning | Real gates and utility; **route costs INR-only**; runner **calls Stage 6 only when p > 0.5** | `src/core/intervention/`, `live_pipeline.py` |
| Stage 7 execution | State machine real; **only `SimulatorInterventionExecutor`**; executor receives an empty context | `src/core/execution/` |
| Stage 8 attribution | Real methodology; results so far come from simulated execution and a heuristic model | `src/core/attribution/` |
| Database integrity | Composite provider and same-attempt FKs (Revision C), preflight-guarded migrations, single head `f4a5b6c7d8e9` | `migrations/versions/`, `tests/migration/` |
| Pipeline durability | **Background task only**: an event committed before a crash never runs Stages 2–8 | `src/api/routes.py` |
| Dashboard | Real API-backed, but sends no `provider`; PayPal excluded from `/payments` and `/timeline` | `src/static/` |
| PayPal | Client does OAuth and webhook verification only; **no sandbox credentials are configured in this environment** | `src/infrastructure/paypal_client.py`, `.env` |

Structural facts that shape the plan:
- Stages 2–4 are Razorpay/synthetic-only, so PayPal attempts never carry degradation or RCA context. With the
  heuristic, a PayPal attempt's probability is at most `0.025 + 0.5 × failure_rate ≤ 0.525`, below the ACT
  threshold of 0.70. PayPal attempts can therefore not ACT without provider-partitioned Stages 2–4. That work
  is tracked below and is not faked.
- Stage-boundary tests forbid provider HTTP calls in core Stage 7 files and Stage 7/8 vocabulary in Stage 6
  files, so the provider executor belongs in `src/infrastructure/`.

## 2. Milestones, dependencies and acceptance criteria

Order: A → B → C → D → E → F → G → H (D and E need B's executor context; G and H need B to F).

### A. Database integrity — verify
- Accept: Revision C constraints present at head; migration round trips exact; same-attempt and provider
  rejections proven at the statement level; CI green.
- Tests: `tests/migration`, `tests/integration/test_stage5_8_same_attempt_constraints.py`.

### B. Intervention decisioning
- B1 Multi-currency route costs: explicit per-currency cost schedules (no FX conversion); USD schedule in the
  default policy; Gate 6 passes for any currency that has a schedule and still fails closed otherwise; policy
  version bumped.
- B2 Remove the runner pre-gate: Stage 6 runs for every Stage 5 prediction and records ACT, MONITOR or
  NO_ACTION (including gate-forced NO_ACTION for INSUFFICIENT_DATA).
- B3 Explicit model provenance in the decision audit payload (model name, version, provenance class).
- B4 Executor context: the runner passes the decision's recorded payment facts (amount, currency, provider,
  reference) to the executor.
- Accept: a USD PayPal attempt is scored under the default policy; p=0.40 records MONITOR; p=0.30 records
  NO_ACTION; INR behaviour unchanged apart from the policy version; characterization tests revised deliberately.

### C. Failure prediction
- Evaluate heuristic vs. logistic models; configurable live-model selection (`STAGE5_MODEL_MODE`) with validated
  metrics required for ML artifacts and a safe fallback to the heuristic; selection recorded in logs and in
  prediction provenance. No LLM in the decision path.
- Accept: invalid, missing or unvalidated artifacts fall back to the heuristic with a recorded reason; tests.

### D. Real PayPal recovery executor (sandbox)
- Action: **create a PayPal Orders v2 checkout** (`POST /v2/checkout/orders`, intent CAPTURE) for the
  at-risk amount and give the payer its approval link — a fallback payment link. No money moves when an
  order is created; the payer must approve, and capture is a separate merchant step this engine never
  performs automatically.
- Idempotency (`PayPal-Request-Id` = command idempotency key), bounded retries on timeouts and 5xx with the same
  request id, error classification, sandbox-only guard (live refused), kill switch
  (`PAYPAL_RECOVERY_EXECUTION_ENABLED`), explicit `is_simulation=false` provenance, persisted recovery
  link (order id, approval URL, status) and an order-status observation path.
- Accept: MockTransport tests cover success, idempotent replay, retries, 4xx/5xx/timeout classification, kill
  switch and live refusal; a sandbox smoke script runs when credentials exist.
- Blocker: real sandbox calls need `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` for a sandbox REST app.

### E. Durable pipeline
- Transactional outbox: a `pipeline_runs` row is written in the same transaction as the ingested event;
  the background task claims and completes it; a reconciler re-runs PENDING/FAILED runs with bounded attempts;
  replays are idempotent (including the known capture-replay failure).
- Accept: an event whose background task never ran is processed exactly once by the reconciler; tests.

### F. Statistical detection
- Z-score evidence alongside the threshold rule, from Stage 2 historical statistics (mean, standard deviation,
  sample count), with guards for cold start, insufficient observations, missing data and low variance;
  opt-in combined rule for false-positive prevention; hysteresis unchanged.
- Accept: unit tests for every guard; default behaviour unchanged unless the mode is enabled.

### G. Frontend
- Provider selector, PayPal attempt visibility (payments and timeline), provider-specific metrics and scopes,
  currency-correct money, decisions, execution status (real vs. simulated), attribution.
- Accept: every view reads real endpoints; API tests for the new backend behaviour; formatter tests.

### H. Reproducible demonstration
- A webhook-driven script: signed events through the real routes, degradation, RCA, prediction, decision,
  execution (real PayPal sandbox when configured, otherwise the simulator, clearly labeled), observation
  and attribution, with a printed report.

## 3. Progress log

| Milestone | Status | Commit | Notes |
|---|---|---|---|
| A | Done | (with B) | Revision C verified: constraints, round trip and rejections against PostgreSQL; CI run 41667680881 green on `5c601c4`. In the live pipeline, attempt registration and Stage 5–8 writes commit in one transaction. No code change. |
| B | Done | see B log | USD cost schedule; pre-gate removed; model identity in the decision audit; executor context |
| C | Done | see C log | Pinned, validated live-model selection with heuristic fallback |
| D | Done (sandbox calls blocked by credentials) | see D log | Real PayPal Orders v2 recovery checkout executor, recovery outcome observation, webhook routing |
| E | Done | see E log | Transactional outbox, claim/lease, reconciler, idempotent capture replay |
| F | Done | see F log | Z-score evidence on every signal; opt-in combined rule; hysteresis unchanged |
| G | Done | see G log | Provider selector, PayPal payments/timeline, scopes, real-vs-simulated execution, recovery checkout |
| H | Done (PayPal step blocked, see below) | see H log | `scripts/demo_end_to_end.py`: webhook-driven Stage 1–8 run with a provenance report |

### Milestone B log (2026-11-03)

- **Implemented:**
  - **Per-currency cost schedules** (`CurrencyCostSchedule`, `InterventionPolicy.cost_schedule()`). The default policy keeps its INR schedule and adds an explicit USD schedule. These are business assumptions, not FX conversions: RETRY_SECONDARY_GATEWAY 5, PROMPT_PAYMENT_METHOD_SWITCH 10, DYNAMIC_RETRY_BACKOFF 2, DEGRADATION_CIRCUIT_BYPASS 15 and FALLBACK_PAYMENT_LINK 10 cents, with a minimum utility of 2 cents.
  - Gate 6 passes only for a currency with a schedule. Each payment is scored against its own schedule, including the minimum utility.
  - The policy version moved 1.0.0 → 1.1.0; INR costs and all thresholds are unchanged.
  - **Pre-gate removed:** the runner (and the demo orchestrator) call Stage 6 for every prediction, so ACT, MONITOR, NO_ACTION and gate-forced NO_ACTION (insufficient data, not eligible, terminal outcome already ingested) are all recorded.
  - **Model identity:** the decision audit records the name and version of the model behind its probability. Provenance classification stays with Stage 8 and the dashboard.
  - **Executor context:** `execution_context(decision, attempt)` gives executors the amount and currency Stage 6 recorded, plus the attempt's provider identity.
- **Files:** `src/core/intervention/{policy,gates,calculator,service}.py`, `src/core/services/live_pipeline.py`, `scripts/demo_orchestrator.py`, `tests/unit/test_intervention_{cost_currency,policy}.py`, `tests/unit/test_execution_context.py`, `tests/integration/test_live_pipeline_e2e.py`.
- **Deliberately revised characterization tests** (each now asserts the recorded verdict and gate instead of "no decision row"):
  - Cold start: gate-forced NO_ACTION, FAILED_STAGE5_INELIGIBLE.
  - Low risk (p = 0.025): NO_ACTION, gate PASSED.
  - Thresholds: 0.30 → NO_ACTION; 0.35, 0.40 and 0.50 → MONITOR; 0.70 → ACT.
  - USD PayPal attempt: ACT, costed from the USD schedule.
  - The "foreign currency never acts" test now uses JPY, which has no schedule.
- **Results:**
  - Offline `tests/unit` + `tests/boundaries`: 1,575 passed, 0 skipped.
  - PostgreSQL `tests/integration` + `tests/api`: 681 passed, 0 skipped.
- **Pre-existing issue found:** running `tests/unit` and `tests/api` in one process fails 2 `tests/api/test_routes.py` tests. The same happens on `5c601c4`, and CI runs those suites in separate jobs. Not caused by this work; to be fixed under E or G.

### Milestone C log (2026-11-03)

- **Evaluation:**
  - **Heuristic** (`DeterministicBaselineModel`, `untrained-heuristic-v2`): deterministic and auditable, and adds bounded increments for recent failure rate, degradation severity and segment-matching RCA. It is not validated on outcomes. It reaches ACT (≥ 0.70) only under a CRITICAL episode with STRONG segment-matching RCA and a recent failure rate ≥ 0.35. That is a genuine severe-outage profile, not a typical payment.
  - **Logistic models:** `SyntheticLogisticRegressionModel` trains on generated data, and `ProviderLogisticRegressionModel` trains offline in provider-evaluation mode. The committed `models/synthetic-development-v1.joblib` carries **no held-out metrics**, so it is not eligible to predict live.
  - **Training data:** `Stage5PipelineValidator` extracts authorizations and their 30-minute terminal labels, then reconstructs features as of each authorization. Provider mode uses genuine Razorpay events only, under a contamination guard and a REPEATABLE READ snapshot.
  - **Quality:** the time-ordered held-out split reports ROC AUC and Brier score into `training_metadata` (`src/core/ml/training.py`).
- **Implemented:** `src/core/ml/selection.py` with `select_live_model`, used by the live pipeline.
  - `STAGE5_MODEL_MODE=heuristic` is the default. `artifact` requires `STAGE5_MODEL_PATH` and a **SHA-256 pin** (`STAGE5_MODEL_SHA256`), which is checked before `joblib.load` executes the pickle.
  - An artifact is accepted only if it implements the interface, uses a v1.0.0 feature schema, and has computed held-out ROC AUC ≥ 0.60 and Brier ≤ 0.25.
  - Every failure falls back to the heuristic with a logged, recorded reason. Prediction rows keep recording `model_name` / `model_version`, and Stage 8 provenance classification is unchanged. Passing selection is not empirical validation.
  - No LLM anywhere in the path; the boundary tests enforce this.
- **Tests:** `tests/unit/test_live_model_selection.py` (17 tests, including a check that the committed synthetic artifact is refused).
- **Results:**
  - Offline: 1,594 passed, 0 skipped.
  - PostgreSQL integration + API: 681 passed, 0 skipped.
- **Remaining:** a provider-trained model can only be accepted after an offline provider evaluation on genuine traffic, which this environment does not have.

### Milestone D log (2026-11-03)

- **Recovery action:** for an ACT decision on FALLBACK_PAYMENT_LINK, create a PayPal Orders v2 checkout (`POST /v2/checkout/orders`, intent CAPTURE) for the amount and currency Stage 6 recorded, and record its approval link.
  - **No money moves when an order is created.** The payer must approve it at PayPal, and capture is a separate merchant step that this engine never performs.
  - The link is **not delivered** to the payer by the engine; the recorded payload says so. Delivery needs a merchant channel.
  - This was chosen because it is a documented, legitimate, non-financial API action that genuinely gives the customer a second way to pay.
- **Client** (`src/infrastructure/paypal_client.py`):
  - `create_order` and `get_order`, using the existing OAuth token cache.
  - `PayPal-Request-Id` set to the command's idempotency key, so PayPal deduplicates retries and replays (HTTP 200 marks a replay).
  - Bounded retries (0.5 s, then 1 s) on timeouts, transport errors, 429 and 5xx, with the same request id; one token refresh on 401.
  - Typed errors: `PayPalTimeoutError`, `PayPalRequestRejectedError` (carries PayPal's issue code), `PayPalAuthError`, `PayPalUnavailableError`.
  - Amounts are formatted by currency exponent, failing closed for unsupported currencies.
  - An approval URL is accepted only if it is https on a paypal.com host.
- **Executor** (`src/infrastructure/paypal_recovery_executor.py`):
  - `PayPalRecoveryCheckoutExecutor` is sandbox-only: live is refused before any request. The kill switch `PAYPAL_RECOVERY_EXECUTION_ENABLED=true` is checked at execution time.
  - Every outcome is classified, never raised: SUCCESS, TIMEOUT, or FAILURE with an explicit code.
  - Results are always `is_simulation=false` under executor name `PAYPAL_ORDERS_V2_SANDBOX`.
  - `RecoveryRoutingExecutor` sends unsupported routes to the labelled simulator.
  - `build_intervention_executor` returns the simulator unless recovery is enabled and a sandbox config is present.
  - Persistence: the command and append-only execution attempt record the order id, status, approval URL, amount, debug id, replay flag and attempt count.
- **Observation** (`observation_service.py`, `execution_repository.py`):
  - Recovery orders come only from real, successful execution attempts.
  - Their verified PayPal capture webhooks resolve the command. A completed recovery capture is CAPTURED, even after the original order was declined, and the audit records it under `recovery`.
  - A capture on both the original and the recovery order is flagged as a conflict (possible double payment).
  - Commands without a recovery order produce exactly the same audit as before.
- **Routing** (`live_pipeline.py`): PayPal events for a recovery order are never registered as attempts. Their capture outcomes run observation and attribution for the command that created the order.
- **Tests:**
  - `tests/unit/test_paypal_recovery_executor.py` (MockTransport, isolated testing only): success, replay, retries with the same request id, token refresh, timeout, 5xx, 422 issue codes, auth rejection without secrets, non-PayPal approval URLs, malformed responses, refusals that make no request, routing, factory and kill switch.
  - `tests/integration/test_live_pipeline_e2e.py::test_real_paypal_recovery_checkout_is_executed_observed_and_attributed` (PostgreSQL, real routes): ACT → real-executor checkout → original decline (FAILED) → recovery approval (no new attempt) → recovery capture (CAPTURED via the recovery order) → attribution ATTRIBUTED with `is_simulated_execution=false`.
  - Two unit stubs were updated for the new recovery-order lookup query; their assertions now check the lookup explicitly.
- **Sandbox smoke:** `scripts/paypal_sandbox_recovery_smoke.py` creates, replays and reads one sandbox order. It refuses live and touches no database.
- **Results:**
  - Offline: 1,638 passed, 0 skipped.
  - PostgreSQL integration + API: 682 passed, 0 skipped.
- **Blocked:** a real sandbox call (smoke script or pipeline) needs sandbox REST credentials, which are not configured. Run `PAYPAL_CLIENT_ID=… PAYPAL_CLIENT_SECRET=… python -m scripts.paypal_sandbox_recovery_smoke` to verify.

### Milestone E log (2026-11-03)

- **Problem:** the route committed the event and then ran Stages 2–8 in a FastAPI background task, so a crash or restart in between lost the run silently.
- **Implemented:**
  - **Outbox:** Revision D (`a7b8c9d0e1f2`) adds `pipeline_runs`.
    - Columns: event id, provider, status, attempts, last failed stage, last error *type*, and timestamps.
    - Constraints: status and attempts CHECKs, and a composite FK `(event_id, source_system) → payment_events`.
    - `SQLIngestionRepository.save_ingestion` writes the PENDING row in the **same commit** as the raw record and event, for every event that needs the pipeline. `pipeline_required()` is shared with the PayPal route, so scheduling and the outbox cannot drift apart.
    - No backfill: the processing state of earlier events is unknown.
  - **Claim/lease:** the background path (`run_live_pipeline` without a session) atomically claims the run (PENDING/FAILED, or RUNNING older than a 10-minute lease), runs the unchanged pipeline, then records SUCCEEDED or FAILED.
    - At most 5 attempts.
    - Only the exception type is stored, never the message, because messages can carry SQL parameters.
    - A completed or exhausted run is skipped, and an event without a row (stored before the outbox existed) runs untracked as before.
  - **Reconciler:** `reconcile_pipeline_runs()` replays PENDING runs older than 30 s, FAILED runs with attempts left, and abandoned RUNNING runs. Available as `scripts/reconcile_pipeline_runs.py` (cron) and as an optional in-app loop (`PIPELINE_RECONCILE_INTERVAL_SECONDS`, off by default).
  - **Idempotent replay:** a terminal event replayed at an already-recorded as-of boundary no longer fails on the `(command_id, as_of_timestamp)` unique constraint. The recorded observation and attribution are reused, and the same applies to recovery-order outcomes.
- **Tests:**
  - New `tests/integration/test_pipeline_outbox.py` (8 tests, PostgreSQL): the row is stored with the event; events that need no pipeline get no row; a lost background task is replayed exactly once and a late task is skipped; a failure is retried and records only the error type; exhausted runs stay unclaimed; a lease is respected, then reclaimed; untracked legacy events still run; the outbox FK is enforced.
  - New `tests/migration/test_revision_d.py`: exact C ↔ D round trip, no backfill.
- **Revised tests:**
  - The capture-replay characterization now asserts success with no new rows.
  - Two route tests and one bridge test now include the outbox row in the ingestion transaction.
  - Head pins moved to Revision D, and the Revision C round trip re-upgrades to C explicitly.
- **Results:**
  - Offline: 1,648 passed.
  - PostgreSQL integration + API: 690 passed.
  - Migration: see the commit message.

### Milestone F log (2026-11-03)

- **Implemented:**
  - **Stage 2:** besides the median baseline, each snapshot now stores the mean, sample standard deviation and count of the same historical windows (`calculate_baseline_statistics`).
  - **Stage 3** (`src/core/degradation/zscore.py`): every signal records `z = (rate − mean) / max(stddev, 0.02)` and a status.
    - Statuses: OK, LOW_VARIANCE (the floor prevents a stable history from exploding a tiny dip), COLD_START, INSUFFICIENT_OBSERVATIONS (fewer than 3), MISSING_RATE.
    - `DEGRADATION_DETECTION_MODE=threshold` is the default and leaves the frozen rule deciding. `threshold_and_zscore` keeps a threshold BAD only if usable evidence confirms z ≤ −3.
    - This prevents false positives in volatile segments. Without usable evidence the threshold rule decides alone, so cold starts are never blinded, and a Z-score alone never creates BAD.
    - Hysteresis is unchanged, because it consumes the resulting signal types.
  - **Revision E** (`b8c9d0e1f2a3`) adds the five nullable columns, with no backfill.
- **Tests:**
  - `tests/unit/test_degradation_zscore.py` (26 tests): every guard, the confirmation threshold, mode parsing, default-mode evidence recording, false-positive suppression, confirmed degradation, cold-start fallback, no Z-only BAD, unchanged pause signals, 3-window hysteresis, and Stage 2 statistics.
  - `tests/integration/test_degradation_zscore_persistence.py` (PostgreSQL, both modes) and `tests/migration/test_revision_e.py`.
- **Results:**
  - Offline: 1,682 passed.
  - PostgreSQL integration + API: 692 passed.
  - Migration: see the commit message.

### Milestone G log (2026-11-03)

- **Backend** (`src/api/inspection_routes.py`):
  - `/payments?provider=paypal` lists PayPal orders, one per approved order, identified by attempt id. It has captured/failed/approved status, provider-exact decision and attribution joins, and search, status and attribution filters. This replaces the previous 400.
  - `/timeline/paypal-order:<id>` traces the order's own events and its captures.
  - Timeline Stage 5–8 lookups are now provider-exact for every provider.
  - Timeline payloads add the executor response code, the real `recovery_checkout` (order, status, approval URL, environment, amount, delivery note), `recovered_via_recovery_order`, and the decision's Stage 5 model and cost-schedule currency.
  - Razorpay `/payments` rows carry `payment_attempt_id`.
- **Frontend** (`src/static/`):
  - A header provider selector drives `/summary`, `/recovery`, `/attributions` and `/payments`, and the API's scope labels are shown.
  - Not-applicable PayPal metrics say so instead of showing 0%.
  - Protected GMV is shown per provider and currency.
  - The interventions KPI names each executor as real or simulated.
  - Payment rows show the provider and open their attempt's trace.
  - The audit drawer shows the real recovery checkout and approval link, and a recovered outcome.
  - Pure text helpers live in `descriptions.js`. No mock data anywhere.
- **Tests:**
  - New API test `test_paypal_payments_are_listed_and_traced_by_their_attempt_id`.
  - The real-recovery e2e test now also checks the timeline.
  - `tests/frontend/dashboard_descriptions.test.mjs` (4 tests); the Node runner now runs every `tests/frontend/*.test.mjs`.
  - The Checkpoint 4 contract test now expects a PayPal listing instead of 400.
  - The emulated timeline test asserts the decision lookup is provider-exact.
- **Results:**
  - Offline: 1,688 passed.
  - PostgreSQL integration + API: 693 passed.
  - Frontend: 11/11.

### Milestone H log (2026-11-03)

- **Script:** `scripts/demo_end_to_end.py` (`DATABASE_URL=<disposable test DB> python -m scripts.demo_end_to_end --reset`).
  - Refuses any database that is neither the disposable test database nor demo-allowlisted, and checks `current_database()` before an optional reset.
  - Generates the synthetic outage context, then sends 60 HMAC-signed Razorpay webhooks (60% failed card payments) and a target authorization and capture through the real `/ingest/razorpay` route, in-process.
  - Prints each stage's record with its provenance and, for the execution, why it was or was not real.
- **Verified run** (2026-11-03, disposable PostgreSQL, real heuristic, nothing forced):
  - 62 pipeline runs SUCCEEDED (outbox).
  - CRITICAL episodes on GLOBAL and payment_method=card, with a 0.70 peak drop.
  - RCA SEGMENT_SPECIFIC.
  - p = 0.825 (0.025 + 0.5 × 0.6 + 0.30 CRITICAL + 0.20 STRONG RCA).
  - ACT on DEGRADATION_CIRCUIT_BYPASS, gates PASSED.
  - Execution SIMULATOR_V1 (simulated: that route has no PayPal action).
  - Outcome CAPTURED.
  - ATTRIBUTED ₹1,237.50 (250,000 × 0.825 × 0.6), flagged as simulated execution and synthetic baseline.
- **Dashboard check** against that data (uvicorn on the test database, throwaway credentials):
  - The overview, scope labels, per-provider protected GMV, the real/simulated executor KPI, the simulation notice and PayPal not-applicable metrics rendered correctly.
  - The audit drawer, opened through `selectPayment`, showed the complete Stage 1–8 trace with simulated execution and estimate labels.
  - Opening the page with credentials in the URL (an automation shortcut) left the original module's click handlers unbound. Normal Basic-auth login does not use this form, but it was not verified in this environment.
- **Why the demo cannot yet show a genuine PayPal recovery** (honest constraints, not faked):
  1. No PayPal sandbox credentials are configured.
  2. Under the default policy, the PayPal-backed route (FALLBACK_PAYMENT_LINK) wins only when no diagnosis-backed route is eligible. During a diagnosed outage, DEGRADATION_CIRCUIT_BYPASS has higher utility.
  3. PayPal attempts never carry degradation or RCA context, because Stages 2–4 are Razorpay-only, so the heuristic tops out at p ≈ 0.525 (MONITOR) for them.

  The real PayPal path is proven end to end in `test_real_paypal_recovery_checkout_is_executed_observed_and_attributed` (real routes, pipeline and PostgreSQL; only PayPal's HTTP API mocked in-process).

## 5. Final status and remaining work (2026-11-03)

**Completed:** all eight stages are integrated through the real webhook routes. The work delivered:
- Durable, idempotent pipeline runs.
- Provider and same-attempt isolation enforced by the database.
- Multi-currency decisioning that records every verdict.
- Validated, pinned model selection.
- Z-score evidence.
- A real, sandbox-only PayPal recovery executor with outcome observation and honest attribution.
- A provider-aware dashboard.
- A reproducible, provenance-labelled demo.

**Remaining, in priority order:**
1. **Provide sandbox credentials** and run `scripts/paypal_sandbox_recovery_smoke.py`, then the pipeline with `PAYPAL_RECOVERY_EXECUTION_ENABLED=true`. This needs `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` and a public webhook URL (or tunnel) for PayPal events.
2. **Provider-partitioned Stages 2–4** (a provider segment with its own baselines, provider-aware RCA, and Stage 5 episode matching for PayPal attempts), so PayPal degradation can drive PayPal recovery decisions.
3. **Route policy review:** whether the PayPal checkout should also execute PROMPT_PAYMENT_METHOD_SWITCH (offering PayPal *is* a method switch). This is a product decision, so it is not made unilaterally.
4. **Provider-trained Stage 5 model** evaluated on genuine traffic, then registered in `VERIFIED_EMPIRICAL_MODELS` after review.
5. **Push and CI:** commits `d40a857`…(H) are local and need approval to push.
6. **Pre-existing test isolation issue:** `tests/unit` + `tests/api` in one process fail 2 route tests; CI runs them separately.

## 4. External blockers

- PayPal sandbox REST credentials (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, and `PAYPAL_WEBHOOK_ID` for webhooks)
  are not configured. Real sandbox execution cannot be verified until they are provided.
- Sandbox recovery completion requires a sandbox buyer account to approve the checkout in a browser, and a
  public webhook URL (or polling) to observe it.
