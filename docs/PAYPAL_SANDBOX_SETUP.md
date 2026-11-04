# PayPal Sandbox Setup and Verification

Revenue Guardian talks to PayPal **sandbox only**. Live PayPal is refused before any request, the engine
never captures, refunds or authorizes money, and `PAYPAL_RECOVERY_EXECUTION_ENABLED` is a kill switch.

## What is verified today, and how

| Capability | Verified locally (PayPal's HTTP API replaced in-process) | Needs a real sandbox account |
|---|---|---|
| OAuth token, caching, refresh | unit tests (`tests/unit/test_paypal_client.py`) | `scripts/paypal_sandbox_recovery_smoke.py` |
| Order creation, approval URL, idempotent `PayPal-Request-Id`, retries, error classification | unit tests (`test_paypal_recovery_executor.py`, `test_payment_method_switch.py`) | smoke script |
| Order status retrieval | unit tests | smoke script |
| Webhook signature verification | unit tests (`test_paypal_webhook_verification.py`) | real webhooks to a public URL |
| Webhook processing, dedup, out-of-order delivery, expiry, decline, failure | end-to-end tests over the real routes and pipeline (`tests/integration/test_payment_method_switch_e2e.py`, `test_live_pipeline_e2e.py`) | real webhooks |
| Customer approval and merchant capture | represented by PayPal-format webhooks in tests | sandbox buyer + merchant capture |

A mocked HTTP response proves the code handles PayPal's documented responses; it does not prove the
sandbox accepts these requests. No real sandbox call has been made from this repository yet because no
credentials are configured.

## 1. Create the sandbox app (developer.paypal.com)

1. Log in to the PayPal Developer Dashboard, choose **Sandbox**, and create a REST app (merchant).
2. Copy its **Client ID** and **Secret**.
3. Under **Sandbox accounts**, note a **personal (buyer)** account for approving checkouts.

## 2. Configure locally (never commit these values)

In `.env` (git-ignored):

```
PAYPAL_ENVIRONMENT=sandbox
PAYPAL_CLIENT_ID=<sandbox client id>
PAYPAL_CLIENT_SECRET=<sandbox secret>
```

## 3. Smoke test the Orders API (no database, no money)

```bash
python -m scripts.paypal_sandbox_recovery_smoke --amount-minor-units 2000 --currency USD
```

Expect a created order with an approval URL, the same order returned for the repeated request
(`repeated_request_returned_same_order: true`) and the fetched status. This verifies OAuth, order
creation, approval URL generation, idempotency and order retrieval against the real sandbox.

## 4. Receive webhooks

PayPal must reach `/webhooks/paypal` over public HTTPS:

1. Run the app (`uvicorn src.main:app --host 127.0.0.1 --port 8000`) and expose it with a tunnel (for example
   `ngrok http 8000`). The tunnel publishes the whole app: set strong `DASHBOARD_USERNAME` /
   `DASHBOARD_PASSWORD`, keep `ENABLE_API_DOCS` off, add the tunnel host to `ALLOWED_HOSTS`.
2. In the app's **Webhooks** settings add `https://<tunnel-host>/webhooks/paypal` with events
   `CHECKOUT.ORDER.APPROVED`, `CHECKOUT.ORDER.COMPLETED`, `PAYMENT.CAPTURE.COMPLETED`,
   `PAYMENT.CAPTURE.DECLINED`, `PAYMENT.CAPTURE.PENDING`, `PAYMENT.CAPTURE.REFUNDED`.
3. Copy the webhook's **ID** into `PAYPAL_WEBHOOK_ID` and restart. Every event is verified with PayPal's
   verify-webhook-signature API before it is stored; unverifiable events are rejected.

## 5. Run a real customer-approved switch

```
PAYPAL_RECOVERY_EXECUTION_ENABLED=true
RECOVERY_DISABLED_ROUTES=DEGRADATION_CIRCUIT_BYPASS   # only if the merchant has no circuit bypass
```

1. Produce a diagnosed card outage and a USD card authorization (for example with the demo,
   `scripts/demo_end_to_end.py`, pointed at the disposable database with the PayPal variables set). The
   decision is ACT on `PROMPT_PAYMENT_METHOD_SWITCH`; the executor creates a sandbox checkout and an
   OFFERED offer (`GET /api/v1/offers`).
2. Open the offer's approval URL and approve with the **sandbox buyer**. PayPal sends
   `CHECKOUT.ORDER.APPROVED`: the offer becomes ACCEPTED.
3. Capture the approved order **as the merchant** (PayPal's Orders API capture call, or the sandbox
   tooling). Revenue Guardian never does this. PayPal sends `PAYMENT.CAPTURE.COMPLETED`: the offer becomes
   COMPLETED, the original attempt is observed CAPTURED through the offer, Stage 8 attributes it, and the
   dashboard's **Verified Recovered Payments** shows the captured amount.
4. To exercise a decline: `POST /api/v1/offers/{id}/decline` (JSON body, dashboard credentials). Expiry and
   abandonment: `python -m scripts.expire_switch_offers` after the deadlines.

## External blockers (what only the account owner can provide)

1. Sandbox REST app **Client ID** and **Secret**.
2. A sandbox **webhook** registered to a public HTTPS URL for this app, and its **Webhook ID**.
3. A sandbox **buyer** account to approve checkouts, and a way to perform the **merchant capture** of the
   approved sandbox order.

With 1 alone, step 3 verifies the Orders API. With 1-3, steps 4-5 verify the full approval, webhook and
verified-completion path.
