import os
from fastapi import Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import AsyncGenerator

from src.infrastructure.database import get_db_session
from src.infrastructure.repository import SQLIngestionRepository
from src.core.services.ingestion_service import IngestionService


def get_razorpay_webhook_secret() -> str:
    # In production, this should be fetched securely, e.g., from AWS Secrets Manager or env
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "dummy_secret_for_local_dev")
    return secret


async def get_ingestion_service(
    session: AsyncSession = Depends(get_db_session),
    webhook_secret: str = Depends(get_razorpay_webhook_secret)
) -> IngestionService:
    repository = SQLIngestionRepository(session)
    return IngestionService(repository, webhook_secret)
