"""
PayPal sandbox smoke test for the recovery checkout action. Touches no database.

Creates one sandbox checkout order exactly as the recovery executor does (same request shape and
PayPal-Request-Id idempotency), sends the same request again to show PayPal returns the same order,
then reads the order back. Creating an order moves no money; nothing is captured.

Requires PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET of a sandbox REST app (developer.paypal.com).
Refuses to run unless PAYPAL_ENVIRONMENT is sandbox (the default). Prints no credentials or tokens.

Usage:
    python -m scripts.paypal_sandbox_recovery_smoke --amount-minor-units 2500 --currency USD
"""
import argparse
import asyncio
import json
import sys
import uuid

from dotenv import load_dotenv

from src.infrastructure.paypal_client import PayPalClient, PayPalConfig, PayPalConfigurationError, PayPalEnvironment


async def main(amount_minor_units: int, currency: str) -> int:
    try:
        config = PayPalConfig.from_env()
    except PayPalConfigurationError as exc:
        print(f"Not configured: {exc}. Set sandbox PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.", file=sys.stderr)
        return 2
    if config.environment is not PayPalEnvironment.SANDBOX:
        print("Refusing: this smoke test only runs against the PayPal sandbox.", file=sys.stderr)
        return 2

    request_id = f"smoke_{uuid.uuid4().hex}"
    async with PayPalClient(config) as client:
        order_args = dict(
            amount_minor_units=amount_minor_units, currency=currency, request_id=request_id,
            custom_id=request_id, reference_id="sandbox-smoke", description="Payment recovery checkout (sandbox smoke)",
        )
        created = await client.create_order(**order_args)
        repeated = await client.create_order(**order_args)
        fetched = await client.get_order(created.order_id)

    report = {
        "environment": "sandbox",
        "paypal_request_id": request_id,
        "created": {"order_id": created.order_id, "status": created.status, "approve_url": created.approve_url,
                    "debug_id": created.debug_id, "replayed": created.replayed},
        "repeated_request_returned_same_order": repeated.order_id == created.order_id,
        "repeated_replayed": repeated.replayed,
        "fetched_status": fetched.status,
        "note": "No money moved. Approve the order with a sandbox buyer account to exercise the payer flow.",
    }
    print(json.dumps(report, indent=2))
    return 0 if report["repeated_request_returned_same_order"] else 1


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--amount-minor-units", type=int, default=2500)
    parser.add_argument("--currency", default="USD")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.amount_minor_units, args.currency.upper())))
