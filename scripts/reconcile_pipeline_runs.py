"""
One reconciliation pass over the pipeline_runs outbox (suitable for cron).

Replays events whose pipeline run is PENDING past its grace period, FAILED with attempts left, or
RUNNING past its lease. Uses DATABASE_URL (environment or .env); there is no default database.

Usage:
    python -m scripts.reconcile_pipeline_runs [--limit 100]
"""
import argparse
import asyncio
import json
import sys

from dotenv import load_dotenv


def main() -> int:
    load_dotenv()
    from src.infrastructure.db_safety import UnsafeDatabaseError, require_env_database_url

    try:
        require_env_database_url()
    except UnsafeDatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    from src.core.services.pipeline_reconciler import reconcile_pipeline_runs

    parser = argparse.ArgumentParser(description="Replay pipeline runs left behind by crashes or failures.")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(reconcile_pipeline_runs(limit=args.limit))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
