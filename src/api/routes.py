from fastapi import APIRouter, Depends, Request, Header, HTTPException, status, BackgroundTasks
from fastapi.responses import JSONResponse
import logging

from src.api.dependencies import get_ingestion_service
from src.core.services.ingestion_service import IngestionService
from src.core.services.live_pipeline import run_live_pipeline
from src.core.domain.exceptions import (
    InvalidWebhookSignatureError,
    MalformedInputError,
    SchemaValidationError,
    DuplicateEventConflictError,
    NormalizationError,
    PersistenceError
)

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/ingest/razorpay")
async def ingest_razorpay(
    request: Request,
    background_tasks: BackgroundTasks,
    x_razorpay_signature: str = Header(None),
    x_razorpay_event_id: str = Header(None),
    ingestion_service: IngestionService = Depends(get_ingestion_service)
):
    """
    Receives Razorpay webhook events, validates authenticity, and processes them through the IngestionService.
    """
    # X-Razorpay-Event-Id is usually present. If not, we will try to infer or reject.
    # Razorpay standard webhooks use X-Razorpay-Event-Id header.
    if not x_razorpay_event_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Razorpay-Event-Id header"
        )
        
    payload_body = await request.body()
    
    try:
        payment_event = await ingestion_service.ingest_razorpay_webhook(
            payload_body=payload_body,
            signature_header=x_razorpay_signature,
            source_event_id=x_razorpay_event_id
        )

        # Trigger the live recovery pipeline only for newly accepted (non-duplicate) events
        if not getattr(ingestion_service, "last_event_is_duplicate", False):
            background_tasks.add_task(run_live_pipeline, payment_event)

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "success", "event_id": str(payment_event.event_id)}
        )
        
    except InvalidWebhookSignatureError as e:
        logger.warning(f"Webhook signature verification failed: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
        
    except MalformedInputError as e:
        logger.error(f"Malformed input: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        
    except SchemaValidationError as e:
        logger.error(f"Schema validation failed: {e}")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
        
    except NormalizationError as e:
        logger.error(f"Normalization failed: {e}")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
        
    except DuplicateEventConflictError as e:
        logger.warning(f"Duplicate event conflict: {e}")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        
    except PersistenceError as e:
        logger.error(f"Persistence error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal Server Error")
    except Exception as e:
        logger.exception("Unexpected error during ingestion")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal Server Error")
