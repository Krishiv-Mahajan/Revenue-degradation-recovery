from fastapi import FastAPI
from src.api.routes import router as ingestion_router

app = FastAPI(title="Payment Recovery - Ingestion API")

app.include_router(ingestion_router)

@app.get("/health")
async def health_check():
    return {"status": "ok"}
