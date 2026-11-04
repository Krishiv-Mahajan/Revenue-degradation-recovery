"""
One deadline pass over payment method switch offers (suitable for cron).

Offers still OFFERED after expires_at become EXPIRED; offers ACCEPTED but not completed by their
completion deadline become ABANDONED. Nothing is deleted, charged or sent to PayPal. Uses DATABASE_URL
(environment or .env); there is no default database.

Usage:
    python -m scripts.expire_switch_offers
"""
import asyncio
import json
import sys

from dotenv import load_dotenv


async def expire_switch_offers() -> dict:
    from src.core.execution.switch_offer_service import SwitchOfferService
    from src.infrastructure.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        changed = await SwitchOfferService(session).expire_due()
        await session.commit()
        return {"changed": [{"offer_id": str(o.offer_id), "status": o.status} for o in changed]}


def main() -> int:
    load_dotenv()
    from src.infrastructure.db_safety import UnsafeDatabaseError, require_env_database_url

    try:
        require_env_database_url()
    except UnsafeDatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(asyncio.run(expire_switch_offers())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
