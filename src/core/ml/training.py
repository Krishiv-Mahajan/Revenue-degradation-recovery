import asyncio
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss, average_precision_score, roc_auc_score
from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import text

from src.infrastructure.models import PaymentEventModel
from src.core.services.feature_reconstruction_service import FeatureReconstructionService
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.core.ml.model import FailurePredictionModel

logger = logging.getLogger(__name__)

class SyntheticLogisticRegressionModel(FailurePredictionModel):
    def __init__(self, model, feature_names, encoders):
        self.model = model
        self.feature_names = feature_names
        self.encoders = encoders
        self.model_name = "SyntheticLogisticRegressionModel"
        self.model_version = "synthetic-development-v1"
        self._feature_schema_version = "v1.0.0-synthetic"

    def predict(self, feature_vector: dict) -> float:
        row = []
        for feature in self.feature_names:
            val = feature_vector.get(feature)
            # Categorical encoding fallback
            if feature in self.encoders:
                encoded = self.encoders[feature].get(val, 0)
                row.append(encoded)
            else:
                row.append(float(val) if val is not None else 0.0)
                
        X = np.array([row])
        # Logistic Regression predict_proba returns [prob_0, prob_1]
        prob = self.model.predict_proba(X)[0][1]
        return float(prob)

    def get_feature_schema_version(self) -> str:
        return self._feature_schema_version


class Stage5PipelineValidator:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = FeatureReconstructionRepository(session)
        self.reconstructor = FeatureReconstructionService(self.repo)

    async def get_authorized_events(self) -> List[PaymentEventModel]:
        stmt = select(PaymentEventModel).where(
            PaymentEventModel.event_type == "payment.authorized"
        ).order_by(PaymentEventModel.ingested_at.asc())
        
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_terminal_label(self, payment_id: str, auth_ingested_at: datetime) -> int:
        """
        Retrieves the earliest terminal event strictly after T.
        Returns 1 for failure, 0 for capture, -1 for unresolved.
        """
        stmt = select(PaymentEventModel).where(
            PaymentEventModel.payment_id == payment_id,
            PaymentEventModel.event_type.in_(["payment.captured", "payment.failed"]),
            PaymentEventModel.ingested_at > auth_ingested_at,
            PaymentEventModel.ingested_at <= auth_ingested_at + timedelta(minutes=30)
        ).order_by(PaymentEventModel.ingested_at.asc()).limit(1)
        
        result = await self.session.execute(stmt)
        terminal_event = result.scalars().first()
        
        if not terminal_event:
            return -1 # Unresolved
            
        return 1 if terminal_event.event_type == "payment.failed" else 0

    async def extract_dataset(self) -> Tuple[List[Dict], List[int], List[datetime]]:
        auth_events = await self.get_authorized_events()
        
        X_raw = []
        y_raw = []
        times = []
        
        for auth_evt in auth_events:
            T = auth_evt.ingested_at
            payment_id = auth_evt.payment_id
            
            # Label
            label = await self.get_terminal_label(payment_id, T)
            if label == -1:
                continue # Skip unresolved
            
            # Features
            snapshot = await self.reconstructor.reconstruct_features(payment_id, T)
            if snapshot is None:
                continue
                
            X_raw.append(asdict(snapshot))
            y_raw.append(label)
            times.append(T)
            
        return X_raw, y_raw, times

    def preprocess_features(self, X_raw: List[Dict]) -> Tuple[np.ndarray, List[str], Dict]:
        # Simple processing for synthetic validation
        feature_names = [
            "amount_minor_units", "hour_of_day", "day_of_week",
            "global_30m_failure_rate", "payment_method_30m_failure_rate"
        ]
        categorical_features = ["payment_method", "currency", "bank", "wallet", "degradation_severity"]
        
        # Build naive dictionary encoders for categoricals
        encoders = {cat: {} for cat in categorical_features}
        for row in X_raw:
            for cat in categorical_features:
                val = row.get(cat)
                if val not in encoders[cat]:
                    encoders[cat][val] = len(encoders[cat])
                    
        feature_names.extend(categorical_features)
        
        X_processed = []
        for row in X_raw:
            processed_row = []
            for f in feature_names:
                if f in categorical_features:
                    processed_row.append(encoders[f].get(row.get(f), 0))
                else:
                    val = row.get(f)
                    processed_row.append(float(val) if val is not None else 0.0)
            X_processed.append(processed_row)
            
        return np.array(X_processed), feature_names, encoders

    def run_pipeline(self, X_raw, y_raw, times):
        logger.info("SYNTHETIC / DEVELOPMENT - Running Training Pipeline")
        
        # Chronological Split
        # Train (used for train+calibration via cv=3): first 80%, Test: last 20%
        n = len(X_raw)
        train_idx = int(n * 0.8)
        
        X, feature_names, encoders = self.preprocess_features(X_raw)
        y = np.array(y_raw)
        
        X_train, y_train = X[:train_idx], y[:train_idx]
        X_test, y_test = X[train_idx:], y[train_idx:]
        
        logger.info(f"Chronological Split: Train={len(y_train)}, Test={len(y_test)}")
        
        # Train and Calibrate
        # We use cv=3 on the training data so it cross-validates internally
        base_model = LogisticRegression(class_weight='balanced', max_iter=1000)
        calibrated_model = CalibratedClassifierCV(estimator=base_model, cv=3)
        calibrated_model.fit(X_train, y_train)
        
        # Evaluate on Test Set
        probs = calibrated_model.predict_proba(X_test)[:, 1]
        
        try:
            pr_auc = average_precision_score(y_test, probs)
            roc_auc = roc_auc_score(y_test, probs)
            brier = brier_score_loss(y_test, probs)
        except ValueError:
            # Handle edge cases where test set only has 1 class due to synthetic randomness
            pr_auc = 0.0
            roc_auc = 0.5
            brier = 0.0
            
        metrics = {
            "pr_auc": pr_auc,
            "roc_auc": roc_auc,
            "brier_score": brier
        }
        
        logger.info(f"SYNTHETIC / DEVELOPMENT - Metrics: {metrics}")
        
        model_instance = SyntheticLogisticRegressionModel(calibrated_model, feature_names, encoders)
        
        return model_instance, metrics
