# Payment Recovery Engine

A production-grade, multi-stage **Revenue Protection & Recovery Engine** for payment failures.

The engine detects systemic and segment-specific payment degradations in real time, diagnoses root causes, predicts transaction-level failure risk, deterministically selects and executes recovery interventions, and measures financial impact through counterfactual attribution.

---

## Architecture: Stage 1–8 Pipeline

The engine operates under strict temporal boundaries across 8 decoupled stages:

1. **Stage 1 — Ingestion & Normalization**: Authenticates webhooks (e.g., Razorpay), parses raw payloads into canonical, immutable `PaymentEvent` models with deterministic deduplication and financial integer minor units.
2. **Stage 2 — Payment Health Analytics**: Computes 5-minute tumbling window metrics across global and dimensional segments (`bank`, `payment_method`, `currency`, `wallet`) and establishes day-of-week/time-of-day baselines.
3. **Stage 3 — Degradation Detection**: Deterministic state machine evaluating sustained rate drops to identify, track, and transition `DegradationEpisode` lifecycles (`ACTIVE`, `RECOVERED`) with canonical severities (`MODERATE`, `HIGH`, `CRITICAL`).
4. **Stage 4 — Root Cause Analysis (RCA)**: Identifies primary structural failure dimensions, computes excess failure contributions, and produces ranked candidate causes with audited evidence strength (`STRONG`, `MODERATE`, `WEAK`).
5. **Stage 5 — Failure Prediction**: Assembles point-in-time feature snapshots strictly as-of transaction time $T$ and applies deterministic calibrated scoring to estimate failure probability $P_{\text{fail}} \in [0.0, 1.0]$.
6. **Stage 6 — Intervention Decisioning**: Evaluates hard safety gates (kill switches, currency allowances, invariance, episode liveness) and scores permitted candidate routes by policy utility to decide `NO_ACTION`, `MONITOR`, or `ACT`.
7. **Stage 7 — Execution & Outcome Observation**: Dispatches intervention commands (`FALLBACK_PAYMENT_LINK`, `DYNAMIC_RETRY_BACKOFF`), executes via pluggable executors, and observes post-treatment terminal payment outcomes (`CAPTURED`, `FAILED`).
8. **Stage 8 — Counterfactual Attribution**: Applies risk-weighted causal attribution ($p_0$) to calculate genuine protected GMV with rigorous audit provenance.

> **Note on Simulator**: The intervention executor used in the local demonstration (`SimulatorInterventionExecutor`) is demonstration infrastructure that simulates provider execution, not an active third-party banking integration.

---

## Quick Start & Demonstration

### 1. Prerequisites & Environment
- Python 3.11+
- Docker & Docker Compose
- Virtual environment installed via Poetry / pip

### 2. Start PostgreSQL
Start the local PostgreSQL container (mapped to port 5433 by default):

```bash
docker compose up -d
```

Verify that the database is running:
```bash
docker ps
```

### 3. Run the End-to-End Demonstration
Run the canonical demonstration orchestrator. By default, it drops and recreates tables to provide a clean, 100% reproducible demonstration:

```bash
PYTHONPATH=. python scripts/demo_orchestrator.py
```

To run against an existing populated database without wiping tables, pass the `--no-reset` flag:
```bash
PYTHONPATH=. python scripts/demo_orchestrator.py --no-reset
```

The script will:
- Seed 2 hours of baseline and synthetic payment events.
- Simulate an incident window where HDFC UPI experiences an 80% failure spike.
- Process all 8 pipeline stages in 5-minute tumbling windows.
- Print an executive summary of detected episodes, failure predictions, executed interventions, and protected GMV.

### 4. Start the FastAPI Service
Launch the application server:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Razorpay Webhook Ingress (Buildathon Live Demo)
To connect Razorpay Sandbox webhooks to the local application:

1. Configure your webhook secret in your environment:
   ```bash
   export RAZORPAY_WEBHOOK_SECRET=<your_razorpay_test_webhook_secret>
   ```
2. Start the FastAPI server:
   ```bash
   uvicorn src.main:app --host 0.0.0.0 --port 8000
   ```
3. Expose the server using an external local tunnel (e.g., ngrok or cloudflared):
   ```bash
   ngrok http 8000
   ```
4. In your Razorpay Dashboard (Settings → Webhooks), register the webhook endpoint:
   ```
   https://<public-tunnel-domain>/ingest/razorpay
   ```
   Set Secret to match `RAZORPAY_WEBHOOK_SECRET` and enable event subscriptions (`payment.authorized`, `payment.captured`, `payment.failed`).
5. Generate current-time recovery context (Buildathon Test Mode):
   ```bash
   python scripts/generate_live_context.py --pm card --curr INR --seed 42
   ```
   Wait for output confirming:
   - `ACTIVE` degradation episode with `CRITICAL` severity
   - RCA candidate `PAYMENT_METHOD = card` with `STRONG` evidence
   - Preceding 30-minute terminal volume $\ge 50$
6. Perform a real Razorpay TEST payment via Card checkout (within the active tumbling window).
7. Open `http://localhost:8000` to inspect the real payment progressing from Stage 1 through Stage 8 on the operator dashboard.

---

## Read-Only Inspection API

The engine exposes internal read-only inspection endpoints designed for operator dashboards, audit verification, and demonstrations.

> **Scope**: These endpoints are internal/demo inspection tools and execute strictly read-only queries with no state mutations.

- **Health Check (DB-Aware)**:
  ```bash
  curl http://localhost:8000/health
  # Response: {"status": "ok"}
  ```

- **Executive Summary Metrics**:
  ```bash
  curl http://localhost:8000/api/v1/summary
  # Returns total processed GMV, protected GMV, active episodes, successful interventions, overall capture rate.
  ```

- **Degradation Episodes**:
  ```bash
  curl "http://localhost:8000/api/v1/episodes?limit=50"
  # Returns detected episodes, dimensions, severity, and start/end windows.
  ```

- **RCA Root Cause Evaluation**:
  ```bash
  curl http://localhost:8000/api/v1/rca/{episode_id}
  # Returns ranked candidate causes, evidence strengths, and excess failure contributions.
  ```

- **Counterfactual Attributions**:
  ```bash
  curl "http://localhost:8000/api/v1/attributions?limit=50"
  # Returns attributed protected GMV records with treatment status and confidence scores.
  ```

- **End-to-End Payment Timeline**:
  ```bash
  curl http://localhost:8000/api/v1/timeline/{payment_id}
  # Traces an individual payment chronologically from Stage 1 through Stage 8.
  ```

---

## Running the Test Suite

Run the full automated regression suite across all stages:

```bash
pytest tests/
```
