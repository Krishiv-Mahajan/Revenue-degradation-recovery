import logging
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from src.api import dependencies
from src.api.routes import router as ingestion_router
from src.api.inspection_routes import router as inspection_router
from src.api.security import (
    AuthenticatedStaticFiles,
    ConfiguredTrustedHostMiddleware,
    require_api_docs_enabled,
    require_dashboard_auth,
)
from src.infrastructure.database import get_db_session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One PayPalClient per process so the OAuth token cache and connection pool are shared.
    app.state.paypal_client = dependencies.create_paypal_client()
    try:
        yield
    finally:
        paypal_client = app.state.paypal_client
        app.state.paypal_client = None
        if paypal_client is not None:
            await paypal_client.aclose()


# The default documentation routes are public; the guarded routes below replace them.
app = FastAPI(
    title="Payment Recovery Engine API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(ConfiguredTrustedHostMiddleware)

# Primary API routers
app.include_router(ingestion_router)
app.include_router(inspection_router)

API_DOCS_GUARDS = [Depends(require_api_docs_enabled), Depends(require_dashboard_auth)]


@app.get("/openapi.json", include_in_schema=False, dependencies=API_DOCS_GUARDS)
async def openapi_schema(request: Request):
    return JSONResponse(request.app.openapi())


@app.get("/docs", include_in_schema=False, dependencies=API_DOCS_GUARDS)
async def swagger_ui():
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")


@app.get("/redoc", include_in_schema=False, dependencies=API_DOCS_GUARDS)
async def redoc():
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")


@app.get("/health")
async def health_check(session: AsyncSession = Depends(get_db_session)):
    """
    Health check endpoint verifying database connectivity.
    """
    try:
        await session.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as e:
        # Driver messages name the host, port, user and database; log the type only.
        logger.error(f"Health check database probe failed ({type(e).__name__})")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unreachable"
        ) from None

# Static file serving for Phase 7 Dashboard
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", AuthenticatedStaticFiles(directory=STATIC_DIR), name="static")

    @app.api_route(
        "/", methods=["GET", "HEAD"], include_in_schema=False, dependencies=[Depends(require_dashboard_auth)]
    )
    async def serve_dashboard():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
