import asyncio
import logging
import joblib
import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.core.ml.training import Stage5PipelineValidator
from src.infrastructure.models import Base

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    engine = create_async_engine(DATABASE_URL, echo=False)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with Session() as session:
        validator = Stage5PipelineValidator(session)
        logger.info("Extracting dataset for training...")
        X_raw, y_raw, times = await validator.extract_dataset()
        
        if not X_raw:
            logger.error("No valid dataset extracted. Ensure synthetic data is generated.")
            return

        model, metrics = validator.run_pipeline(X_raw, y_raw, times)
        logger.info(f"Model trained successfully. Metrics: {metrics}")
        
        # Persist the model
        model_dir = "models"
        if not os.path.exists(model_dir):
            os.makedirs(model_dir)
            
        model_path = os.path.join(model_dir, "synthetic-development-v1.joblib")
        joblib.dump(model, model_path)
        logger.info(f"SYNTHETIC / DEVELOPMENT - Model saved to {model_path}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
