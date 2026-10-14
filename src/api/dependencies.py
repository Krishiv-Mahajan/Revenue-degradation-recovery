import os
import logging
from fastapi import Request, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import AsyncGenerator, Optional

from src.infrastructure.database import get_db_session
from src.infrastructure.repository import SQLIngestionRepository
from src.infrastructure.paypal_client import PayPalClient, PayPalConfig, PayPalConfigurationError
from src.core.services.ingestion_service import IngestionService
from src.core.services.paypal_ingestion_service import PayPalIngestionService

logger = logging.getLogger(__name__)


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


def create_paypal_client() -> Optional[PayPalClient]:
    """
    Builds the shared PayPalClient from environment settings.
    Returns None when configuration is missing so PayPal routes fail closed with 503.
    """
    try:
        config = PayPalConfig.from_env()
    except PayPalConfigurationError as e:
        logger.warning(f"PayPal webhook intake disabled: {e}")
        return None
    return PayPalClient(config)


def get_paypal_client(request: Request) -> PayPalClient:
    paypal_client = getattr(request.app.state, "paypal_client", None)
    if paypal_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PayPal webhook intake is not configured"
        )
    return paypal_client


async def get_paypal_ingestion_service(
    # Resolved before the session so an unconfigured intake never opens a database session.
    paypal_client: PayPalClient = Depends(get_paypal_client),
    session: AsyncSession = Depends(get_db_session),
) -> PayPalIngestionService:
    return PayPalIngestionService(SQLIngestionRepository(session), paypal_client)
