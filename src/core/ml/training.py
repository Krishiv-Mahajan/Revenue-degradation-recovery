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
from src.core.ml.model import FailurePredictionModel, SyntheticLogisticRegressionModel

logger = logging.getLogger(__name__)


from sklearn.frozen import FrozenEstimator


class PredefinedCalibrationSplit:
    """
    Temporally safe single-split generator for probability calibration.
    Yields all validation indices as the evaluation fold for calibration,
    avoiding standard K-fold temporal mixing where future observations
    are used to train models that predict past observations.
    """
    def split(self, X, y=None, groups=None):
        yield np.arange(len(X)), np.arange(len(X))

    def get_n_splits(self, X=None, y=None, groups=None):
        return 1


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
        
        # Chronological Split (§11A):
        # Training set: first 60% (older)
        # Validation set: middle 20% (intermediate, used strictly for calibration per §11C)
        # Test set: last 20% (strictly newer, held-out evaluation)
        n = len(X_raw)
        train_end = int(n * 0.6)
        val_end = int(n * 0.8)
        
        X, feature_names, encoders = self.preprocess_features(X_raw)
        y = np.array(y_raw)
        
        X_train, y_train = X[:train_end], y[:train_end]
        X_val, y_val = X[train_end:val_end], y[train_end:val_end]
        X_test, y_test = X[val_end:], y[val_end:]
        
        train_times = times[:train_end]
        val_times = times[train_end:val_end]
        test_times = times[val_end:]
        
        # Enforce chronological ordering and temporal non-overlap (§11A)
        if len(train_times) > 0 and len(val_times) > 0:
            assert max(train_times) <= min(val_times), (
                "Temporal leakage: training period overlaps validation period"
            )
        if len(val_times) > 0 and len(test_times) > 0:
            assert max(val_times) <= min(test_times), (
                "Temporal leakage: validation period overlaps test period"
            )
        
        logger.info(
            f"Chronological Split: Train={len(y_train)}, Val={len(y_val)}, Test={len(y_test)}"
        )
        
        # Step 1: Fit base model on older training data only (§11A, §11B)
        base_model = LogisticRegression(class_weight='balanced', max_iter=1000)
        base_model.fit(X_train, y_train)
        
        # Step 2: Calibrate on intermediate validation data only (§11C)
        # Uses FrozenEstimator and PredefinedCalibrationSplit to prevent refitting or temporal mixing
        if len(np.unique(y_val)) >= 2:
            calibrated_model = CalibratedClassifierCV(
                estimator=FrozenEstimator(base_model),
                method='sigmoid',
                cv=PredefinedCalibrationSplit()
            )
            calibrated_model.fit(X_val, y_val)
            eval_model = calibrated_model
        else:
            eval_model = base_model
            
        # Step 3: Evaluate on held-out test data strictly in the future of train and val (§12)
        probs = eval_model.predict_proba(X_test)[:, 1]
        
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
        
        model_instance = SyntheticLogisticRegressionModel(eval_model, feature_names, encoders)
        
        return model_instance, metrics
