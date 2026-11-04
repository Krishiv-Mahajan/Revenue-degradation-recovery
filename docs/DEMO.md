# Running the Demonstration

## One command

```bash
./scripts/run_demo.sh
```

It starts only the disposable `db_test` container (port 5434, wiped when stopped), migrates it to the Alembic
head and runs `python -m scripts.demo_end_to_end --reset`, which refuses any other database. It prints a
JSON report in about 10 seconds. The development database is never touched.

Equivalent manual sequence:

```bash
docker compose --profile test up -d db_test
export DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5434/payment_recovery_test
ALEMBIC_TARGET_DATABASE=payment_recovery_test python -m alembic upgrade head
python -m scripts.demo_end_to_end --reset
```

## What it shows

Every event goes through the real webhook routes (signature check, dedup, outbox) and the real eight-stage
pipeline. Nothing forces a decision; the expected results below are what the unchanged policy produces.

| Scenario | Input | Expected result |
|---|---|---|
| 1 Healthy baseline | 50 captured Razorpay UPI payments, then a UPI authorization | no episode, p = 0.025, **NO_ACTION** |
| 2 Razorpay card outage | generated card-outage context, 60 card webhooks (60% failed), authorization + capture | CRITICAL episodes (GLOBAL, card), RCA SEGMENT_SPECIFIC card STRONG, p = 0.825, **ACT on DEGRADATION_CIRCUIT_BYPASS** (simulated), observed CAPTURED, estimated protected GMV ₹1,237.50 (marked simulated) |
| 3 Customer-approved switch | a USD 20.00 card authorization in the same outage, for a merchant without a circuit bypass | p = 0.825, **ACT on PROMPT_PAYMENT_METHOD_SWITCH**. Without sandbox credentials: simulated, **no offer** (the report says why). With credentials: a real sandbox checkout offer awaiting the customer |
| 4 PayPal-wide outage | generated PayPal history, 150 PayPal capture webhooks (80% declined), an approved order | PayPal-scope CRITICAL episodes, RCA SYSTEMIC, p = 0.725, **ACT on RETRY_SECONDARY_GATEWAY** (simulated); Razorpay scope unaffected |
| 5 Duplicates | the capture and the approval delivered again | no new pipeline run, prediction, decision or command |

The report labels every result: **generated** (demo inputs), **real** (this system's verification and
processing), **simulated** (executions with `is_simulation=true`), **estimated** (Stage 8 protected GMV) and
**PayPal-verified** (money PayPal captured through a real recovery; "none" unless a real sandbox capture
happened). No payment is fabricated.

## Viewing it in the dashboard

```bash
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5434/payment_recovery_test \
DASHBOARD_USERNAME=<choose> DASHBOARD_PASSWORD=<choose> \
  uvicorn src.main:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000:
- **Overview**: provider health (Razorpay + synthetic and PayPal both DEGRADED/CRITICAL after the demo),
  estimated protected GMV versus verified recovered payments, interventions with their simulator provenance,
  switch offers.
- **Incidents**: the four episodes with their analytics scope, RCA candidates and decisions.
- **Recovery**: the ledger names each route (gateway retry, circuit bypass, customer-approved PayPal switch)
  with ACT/MONITOR/NO_ACTION, simulated versus real execution, outcomes and attribution; the offers table.
- **Payments**: per-payment audit trail from ingestion to attribution.

## A real PayPal sandbox recovery

Follow [PAYPAL_SANDBOX_SETUP.md](PAYPAL_SANDBOX_SETUP.md): with sandbox credentials,
`PAYPAL_RECOVERY_EXECUTION_ENABLED=true` and a public webhook URL, scenario 3 creates a real sandbox offer;
approving it with a sandbox buyer and capturing it as the merchant completes it, and the dashboard's
Verified Recovered Payments shows the PayPal-captured amount.

## Stop

```bash
docker compose --profile test stop db_test     # discards all demo data
```
