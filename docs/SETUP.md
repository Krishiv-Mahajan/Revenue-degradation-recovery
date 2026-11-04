# Setup, Configuration, Database and Tests

## Requirements

- Python 3.11 with Poetry (or the checked-in `.venv`), Docker (Compose v2), Node.js 18+ (frontend tests).
- `poetry install` installs the locked dependencies.

## Databases

| Service | Port | Database | Purpose |
|---|---|---|---|
| `db` | 5433 | `payment_recovery` | development/demo data (persistent volume) |
| `db_test` | 5434 | `payment_recovery_test` | disposable: tests, the demo and verification (tmpfs, wiped when stopped) |

```bash
docker compose --profile test up -d db_test     # only the disposable database
```

Safety rails (`src/infrastructure/db_safety.py`):
- Tests read only `TEST_DATABASE_URL`, which must be exactly `localhost:5434/payment_recovery_test`; the
  server's `current_database()` is confirmed before any truncation. There is no fallback.
- Alembic never accepts port 5433, needs `ALEMBIC_TARGET_DATABASE` to name the URL's database exactly, and
  refuses non-test databases unless `ALEMBIC_ALLOW_NON_TEST_DATABASE=yes`.
- `scripts/demo_end_to_end.py` refuses every database except the disposable test database.

Migrate the disposable database:

```bash
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5434/payment_recovery_test \
ALEMBIC_TARGET_DATABASE=payment_recovery_test \
  python -m alembic upgrade head
```

The chain is linear (single head `d0e1f2a3b4c5`). Downgrades that would lose data refuse and change nothing.

## Configuration

Copy `.env.example` to `.env` (git-ignored). Never commit real values.

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Application database (no default). |
| `TEST_DATABASE_URL` | Disposable test database for integration/API tests. |
| `RAZORPAY_WEBHOOK_SECRET` | HMAC secret for `/ingest/razorpay`; missing or placeholder values fail closed (503). |
| `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD` | HTTP Basic credentials for the dashboard, `/static` and `/api/v1`; both required (503 otherwise). |
| `ALLOWED_HOSTS` | Accepted Host headers (default `localhost,127.0.0.1`). |
| `ENABLE_API_DOCS` | Serve `/docs` (behind dashboard credentials) when `true`. |
| `PAYPAL_ENVIRONMENT` | `sandbox` (default). Live execution is always refused. |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | PayPal sandbox REST app credentials. |
| `PAYPAL_WEBHOOK_ID` | Webhook id used to verify PayPal webhook signatures. `/webhooks/paypal` returns 503 without client credentials and never accepts an event it could not verify. |
| `PAYPAL_RECOVERY_EXECUTION_ENABLED` | `true` enables real sandbox checkouts and switch offers; anything else = labelled simulator (kill switch). |
| `RECOVERY_DISABLED_ROUTES` | Comma-separated routes the merchant cannot perform (for example `DEGRADATION_CIRCUIT_BYPASS`). Only removes routes; the decision records the policy variant. |
| `DEGRADATION_DETECTION_MODE` | `threshold` (default) or `threshold_and_zscore` (a BAD signal also needs Z ≤ −3). |
| `PIPELINE_RECONCILE_INTERVAL_SECONDS` | Run the outbox reconciler in-process every N seconds (off by default). |
| `STAGE5_MODEL_MODE`, `STAGE5_MODEL_PATH`, `STAGE5_MODEL_SHA256` | Live Stage 5 model: heuristic (default) or a pinned, validated artifact. |

## Running

```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000 and sign in with the dashboard credentials. Periodic jobs (cron):

```bash
python -m scripts.reconcile_pipeline_runs     # replay pipeline runs left behind
python -m scripts.expire_switch_offers        # expire / abandon switch offers past their deadlines
```

## Tests

```bash
docker compose --profile test up -d db_test
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5434/payment_recovery_test

# offline: unit + boundary (no database)
python -m pytest tests/unit tests/boundaries -q
# PostgreSQL: integration + API (incl. end-to-end scenarios)
python -m pytest tests/integration tests/api -q
# migrations: every revision up/down with exact schema and data equality
RUN_MIGRATION_TESTS=1 python -m pytest tests/migration -q
# frontend formatters and descriptions
node --test tests/frontend/
```

Keep `DATABASE_URL` unset or pointing elsewhere: the test harness refuses a `TEST_DATABASE_URL` that
resolves to the same database as `DATABASE_URL`. CI runs the same four steps
(`.github/workflows/`).
