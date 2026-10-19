from fastapi import APIRouter, Depends, Request, Header, HTTPException, status, BackgroundTasks
from fastapi.responses import JSONResponse
import logging

from src.api.dependencies import get_ingestion_service, get_paypal_ingestion_service
from src.core.domain.payment_attempts import resolve_pipeline_attempt
from src.core.services.ingestion_service import IngestionService
from src.core.services.paypal_ingestion_service import PayPalIngestionOutcome, PayPalIngestionService
from src.core.services.live_pipeline import run_live_pipeline
from src.infrastructure.paypal_client import (
    PayPalAuthError,
    PayPalConfigurationError,
    PayPalUnavailableError,
    PayPalVerificationRequestRejectedError,
)
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
        
    # Exception messages and chained database errors can carry payload values or SQL parameters;
    # log the exception type only and return fixed details.
    except InvalidWebhookSignatureError as e:
        logger.warning(f"Razorpay webhook signature verification failed ({type(e).__name__})")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")
        
    except MalformedInputError as e:
        logger.error(f"Malformed Razorpay webhook ({type(e).__name__})")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed request")
        
    except SchemaValidationError as e:
        logger.error(f"Razorpay webhook schema validation failed ({type(e).__name__})")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unprocessable webhook event")
        
    except NormalizationError as e:
        logger.error(f"Razorpay webhook normalization failed ({type(e).__name__})")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unprocessable webhook event")
        
    except DuplicateEventConflictError as e:
        logger.warning(f"Razorpay duplicate event conflict ({type(e).__name__})")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Conflicting duplicate event")
        
    except PersistenceError as e:
        logger.error(f"Razorpay webhook persistence failed ({type(e).__name__})")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal Server Error")
    except Exception as e:
        logger.error(f"Unexpected error during Razorpay ingestion ({type(e).__name__})")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal Server Error")


# PayPal webhook events are a few KB; anything far larger is not a legitimate delivery.
MAX_PAYPAL_WEBHOOK_BODY_BYTES = 256 * 1024


async def _read_limited_body(request: Request, limit: int) -> bytes:
    too_large = HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail="Request body too large"
    )
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > limit:
                raise too_large
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length header")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise too_large
    return bytes(body)


@router.post("/webhooks/paypal")
async def ingest_paypal(
    request: Request,
    background_tasks: BackgroundTasks,
    paypal_ingestion_service: PayPalIngestionService = Depends(get_paypal_ingestion_service)
):
    """
    Receives PayPal webhook events, verifies them with PayPal and persists them.
    A newly stored event that starts or resolves a PayPal attempt (order approval, capture outcome)
    is scheduled for pipeline Stages 5-8; duplicates and other event types are persistence only.
    """
    raw_body = await _read_limited_body(request, MAX_PAYPAL_WEBHOOK_BODY_BYTES)

    try:
        result = await paypal_ingestion_service.ingest(request.headers, raw_body)

    except InvalidWebhookSignatureError as e:
        logger.warning(f"PayPal webhook signature verification failed: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    except MalformedInputError as e:
        logger.warning(f"Malformed PayPal webhook: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed request")

    except PayPalVerificationRequestRejectedError as e:
        # Often a webhook_id mismatch on our side; alert if this spikes.
        logger.error(f"PayPal rejected the verification request: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook could not be verified")

    except (PayPalConfigurationError, PayPalAuthError, PayPalUnavailableError) as e:
        logger.error(f"PayPal webhook verification unavailable ({type(e).__name__}): {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook verification temporarily unavailable"
        )

    except (SchemaValidationError, NormalizationError) as e:
        logger.error(f"PayPal webhook could not be processed: {e}")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unprocessable webhook event")

    except DuplicateEventConflictError as e:
        logger.warning(f"PayPal duplicate event conflict: {e}")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Conflicting duplicate event")

    except PersistenceError as e:
        # The message can embed SQL parameters, including the raw payload; log the type only.
        logger.error(f"PayPal webhook persistence failed ({type(e).__name__})")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal Server Error")
    except Exception as e:
        # No message or traceback: chained database errors can carry SQL parameters (the raw payload).
        logger.error(f"Unexpected error during PayPal ingestion ({type(e).__name__})")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal Server Error")

    if result.outcome is PayPalIngestionOutcome.STORED:
        if resolve_pipeline_attempt(result.payment_event) is not None:
            background_tasks.add_task(run_live_pipeline, result.payment_event)
        content = {"status": "stored", "event_id": str(result.payment_event.event_id)}
    else:
        content = {"status": result.outcome.value}
    return JSONResponse(status_code=status.HTTP_200_OK, content=content)
