import os
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from src.api.routes import router as ingestion_router
from src.api.inspection_routes import router as inspection_router
from src.infrastructure.database import get_db_session

app = FastAPI(title="Payment Recovery Engine API")

# Primary API routers
app.include_router(ingestion_router)
app.include_router(inspection_router)

@app.get("/health")
async def health_check(session: AsyncSession = Depends(get_db_session)):
    """
    Health check endpoint verifying database connectivity.
    """
    try:
        await session.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unreachable: {str(e)}"
        )

# Static file serving for Phase 7 Dashboard
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_dashboard():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
