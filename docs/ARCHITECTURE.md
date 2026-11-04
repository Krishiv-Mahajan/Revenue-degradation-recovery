# Revenue Guardian — Architecture

Revenue Guardian detects payment-provider degradation from live payment events, diagnoses its cause,
predicts which in-flight payments are at risk, decides whether and how to intervene, executes the
intervention, observes the real outcome and attributes recovered revenue conservatively. Everything is
one FastAPI service over PostgreSQL; every stage writes append-only, versioned, auditable records.

## Request path

```
Razorpay webhook ──HMAC──┐                                   ┌─ Stage 2 analytics   (per analytics scope)
                          ├─ Stage 1 ingest ─ outbox row ──── ├─ Stage 3 degradation (per analytics scope)
PayPal webhook ─PayPal───┘   (raw record,     (pipeline_runs) ├─ Stage 4 RCA         (per analytics scope)
  signature API              canonical event)                 ├─ Stage 5 prediction  (per payment attempt)
                                                              ├─ Stage 6 decision    (policy + gates)
                                                              ├─ Stage 7 execution + offers + observation
                                                              └─ Stage 8 attribution
```

- **Ingestion** (`src/api/routes.py`, `src/core/services/*ingestion_service.py`): Razorpay webhooks are
  verified with HMAC-SHA256 (constant-time compare); PayPal webhooks with PayPal's
  verify-webhook-signature API (fails closed with 503 when unconfigured). Both are deduplicated by
  provider event id and stored with their raw payload and a canonical `payment_events` row.
- **Durable processing** (`pipeline_runs`, `src/core/services/pipeline_reconciler.py`): the event and its
  pipeline run are committed in one transaction (transactional outbox). The background task claims the
  run with a lease; a reconciler replays runs left PENDING, FAILED (bounded attempts) or RUNNING past
  their lease. Every stage is idempotent, so replays never duplicate records.

## The eight stages

| Stage | What it does | Key code |
|---|---|---|
| 1 Ingestion | Verify, deduplicate, normalize to canonical events | `src/core/parsers`, `src/core/normalizers` |
| 2 Health analytics | 5-minute tumbling windows per segment; baseline = median of the same window 1-4 weeks back, plus mean/stddev for Z-scores | `src/core/analytics` |
| 3 Degradation | BAD/NORMAL/LOW_VOLUME/NO_BASELINE signals from absolute/relative drop thresholds (Z-score evidence recorded; optional combined rule); hysteresis: 3 BAD windows open an episode, 2 NORMAL close it; severity MODERATE/HIGH/CRITICAL | `src/core/degradation` |
| 4 RCA | Excess-failure decomposition over structural dimensions with historical baselines; SEGMENT_SPECIFIC / SYSTEMIC / UNKNOWN; supporting error evidence | `src/core/rca` |
| 5 Prediction | Point-in-time features (as-of the decision time, never later data) and a deterministic heuristic (or a pinned, validated artifact) | `src/core/services/failure_prediction_service.py`, `src/core/ml` |
| 6 Decision | Hard gates, then route eligibility, cost/utility scoring and ACT / MONITOR / NO_ACTION | `src/core/intervention` |
| 7 Execution | Command (idempotent per decision), claim, executor, append-only attempts; switch offers; outcome observation from canonical events only | `src/core/execution`, `src/infrastructure/paypal_recovery_executor.py` |
| 8 Attribution | Risk-weighted counterfactual estimate with treatment, timing and provenance evidence | `src/core/attribution` |

## Provider isolation

- **Payment attempts** (`payment_attempts`): one per Razorpay payment, one per PayPal order. Every Stage 5-8
  row carries `(payment_attempt_id, source_system)` with composite foreign keys, so a record can never be
  linked to another provider's attempt or to another attempt's decision/command.
- **Analytics scopes** (Stages 2-4, `src/core/domain/analytics_scopes.py`): `razorpay_pipeline`
  (Razorpay + synthetic) and `paypal` have separate snapshots, baselines, signals, episodes and RCA. A
  payment only ever sees episodes of its own scope. PayPal segments by GLOBAL and currency (the only fields
  PayPal events carry) and never reports a population-wide segment as a root cause.

## Decisioning (Stage 6)

1. Gates (any failure forces NO_ACTION): kill switch, valid amount/currency, no terminal outcome before
   the decision, a usable Stage 5 prediction, episode not invalidated and diagnosis reliable, a cost
   schedule for the payment's currency (INR and USD), global ACT rate limit.
2. Route eligibility: diagnosis-backed routes need an RCA diagnosis of the right dimension (and STRONG or
   MODERATE evidence where required); cooldowns per attempt; merchant-disabled routes
   (`RECOVERY_DISABLED_ROUTES`) are ineligible.
3. Utility = p × amount × route recovery rate − route cost (integer minor units, per currency). The best
   eligible route is chosen; ACT at p ≥ 0.70, MONITOR at p ≥ 0.35, else NO_ACTION.

Routes (policy `default-recovery-v1` 1.2.0):

| Route | Needs | Execution |
|---|---|---|
| RETRY_SECONDARY_GATEWAY | diagnosis on BANK, or SYSTEMIC | simulator (no provider implements it) |
| DEGRADATION_CIRCUIT_BYPASS | STRONG/MODERATE diagnosis on BANK or PAYMENT_METHOD | simulator |
| PROMPT_PAYMENT_METHOD_SWITCH | STRONG/MODERATE PAYMENT_METHOD diagnosis matching the payment; attempt not already PayPal; a currency whose PayPal payments can be verified | real PayPal sandbox checkout offer when enabled, else simulator |
| FALLBACK_PAYMENT_LINK | none (GLOBAL) | real PayPal sandbox checkout when enabled, else simulator |
| DYNAMIC_RETRY_BACKOFF | none | simulator |

## Customer-approved payment method switch

`src/core/domain/switch_offer_models.py`, `src/core/execution/switch_offer_service.py`, tables
`payment_method_switch_offers` and `payment_method_switch_offer_events`.

```
OFFERED ──PayPal approval before expiry──> ACCEPTED ──PayPal verified capture of the offered amount──> COMPLETED
   │                                          │──capture declined──> PAYMENT_FAILED
   ├──explicit decline (merchant channel)──> DECLINED   └──not paid within 1 h of acceptance──> ABANDONED
   └──no response within 30 min──> EXPIRED
```

- Only a real sandbox execution creates an offer (one per command); simulations never do.
- Consent is PayPal's signature-verified `CHECKOUT.ORDER.APPROVED` for the offer's order. The engine never
  captures, charges or switches anything; capturing an approved order is the merchant's step.
- Deadlines are applied before any signal; terminal offers never change (later events are recorded as
  ignored); a capture delivered before the approval is held until consent is recorded.
- Database constraints: sandbox only, the provider must change, one offer per command and per checkout
  order, consent/completion/resolution invariants; append-only events linked to the PayPal event behind
  each step (replays are no-ops).

## Outcome observation and attribution

- Stage 7 observes outcomes only from canonical terminal events, as-of a declared boundary. A recovery
  checkout's PayPal capture resolves the original attempt; for a switch offer only the capture the offer
  lifecycle verified counts, and only once the customer's acceptance had been ingested.
- Stage 8 attributes only CAPTURED, treated payments whose terminal event follows the execution. A real
  switch offer counts as treatment only if the customer accepted it, and a payment completed without the
  offer is not credited to it. Offers and checkout creation are never revenue.
- **Estimated protected GMV** (Stage 8) is a risk-weighted counterfactual estimate. **Verified recovered
  payments** (dashboard) is the money PayPal actually captured through a real recovery; it excludes all
  simulated executions.

## Data model and migrations

PostgreSQL 15, SQLAlchemy async, Alembic with a single linear head (`d0e1f2a3b4c5`, 17 revisions).
Destructive downgrades refuse while data would be lost. Alembic never accepts the development port (5433),
needs the target database named explicitly (`ALEMBIC_TARGET_DATABASE`), and refuses non-test databases
unless `ALEMBIC_ALLOW_NON_TEST_DATABASE=yes`. See [SETUP.md](SETUP.md).

## Dashboard

Static ES-module app (`src/static`) over authenticated read-only inspection APIs (`/api/v1/*`), plus the
offers API (`/api/v1/offers`, with a merchant-reported decline). It shows provider health per scope,
incidents with RCA, predictions, decisions, simulated versus real execution, switch offers (awaiting,
accepted, paid, declined, expired, abandoned), verified recovered money versus estimates, and a full
per-payment audit trail.
