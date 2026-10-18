import asyncio
import hashlib
import logging
import json
import re
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import List, Dict, Optional, Tuple, Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss, average_precision_score, roc_auc_score
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_, func, or_, text

from src.core.domain.rca_models import HISTORICAL_WEEKS_BACK
from src.core.domain.source_systems import NON_RAZORPAY_PIPELINE_SOURCES
from src.infrastructure.models import PaymentEventModel
from src.core.services.feature_reconstruction_service import FeatureReconstructionService
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.core.ml.model import (
    FailurePredictionModel,
    ProviderLogisticRegressionModel,
    SyntheticLogisticRegressionModel,
)

logger = logging.getLogger(__name__)


from sklearn.frozen import FrozenEstimator


RAZORPAY_SOURCE_SYSTEM = "razorpay"

# Matches get_terminal_label's 30-minute label horizon.
LABEL_HORIZON = timedelta(minutes=30)

# Derived Stage 5 inputs look back 24 hours (failure-rate features) and 4 weeks (the Stage 2
# baseline and Stage 4 RCA history behind degradation and RCA context).
CONTAMINATION_LOOKBACK = timedelta(weeks=HISTORICAL_WEEKS_BACK, hours=24)

# Rows written by scripts/generate_live_context.py before it labelled them 'synthetic': each
# transaction shares one 12-hex-digit token across its payment, order and event ids.
LEGACY_LIVE_CONTEXT_PAYMENT_PREFIX = "pay_live_ctx_"
_LEGACY_LIVE_CONTEXT_PAYMENT_ID = re.compile(r"pay_live_ctx_([0-9a-f]{12})")


def is_legacy_live_context_event(
    payment_id: Optional[str], order_id: Optional[str], source_event_id: Optional[str]
) -> bool:
    match = _LEGACY_LIVE_CONTEXT_PAYMENT_ID.fullmatch(payment_id or "")
    if match is None:
        return False
    token = match.group(1)
    return order_id == f"order_live_ctx_{token}" and source_event_id in (
        f"evt_live_auth_{token}",
        f"evt_live_term_{token}",
    )


class TrainingPopulation(str, Enum):
    # Every non-PayPal source, including synthetic and test data (the original extract).
    SYNTHETIC_DEVELOPMENT = "SYNTHETIC_DEVELOPMENT"
    # Genuine Razorpay events only; refuses windows that generated data could have shaped.
    PROVIDER_EVALUATION = "PROVIDER_EVALUATION"


class EvaluationDataContaminationError(RuntimeError):
    """Raised when provider evaluation would use features derived from generated data."""


class EvaluationIsolationError(RuntimeError):
    """Raised when provider evaluation cannot run inside one consistent PostgreSQL snapshot."""


PROVIDER_EVALUATION_ISOLATION_LEVEL = "REPEATABLE READ"
_SNAPSHOT_ISOLATION_LEVELS = frozenset({"repeatable read", "serializable"})


def compute_dataset_fingerprint(
    X_raw: List[Dict[str, Any]], y_raw: List[int], times: List[datetime]
) -> str:
    """
    SHA-256 over the ordered samples, labels and sample times. Feature key order is ignored and
    timestamps are normalized to UTC, but sample order matters, so callers must supply a stable
    order. It identifies the dataset only: it excludes training code, hyperparameters, artifact
    identity and payment ids, so identical data trained differently yields the same fingerprint.
    """
    if not len(X_raw) == len(y_raw) == len(times):
        raise ValueError("Samples, labels and sample times must align.")
    if any(t.tzinfo is None or t.utcoffset() is None for t in times):
        raise ValueError("Sample times must be timezone-aware.")
    canonical = json.dumps(
        [
            {"features": row, "label": int(label), "time": t.astimezone(timezone.utc).isoformat()}
            for row, label, t in zip(X_raw, y_raw, times)
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _CheckedExtraction:
    window: Tuple[datetime, datetime]
    dataset_fingerprint: str


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
    def __init__(
        self,
        session: AsyncSession,
        population: TrainingPopulation = TrainingPopulation.SYNTHETIC_DEVELOPMENT,
    ):
        self.session = session
        self.population = TrainingPopulation(population)
        self._checked_extraction: Optional[_CheckedExtraction] = None
        self._snapshot_transaction = None
        self.repo = FeatureReconstructionRepository(session)
        self.reconstructor = FeatureReconstructionService(self.repo)

    @property
    def _provider_evaluation(self) -> bool:
        return self.population is TrainingPopulation.PROVIDER_EVALUATION

    @property
    def checked_contamination_window(self) -> Optional[Tuple[datetime, datetime]]:
        return self._checked_extraction.window if self._checked_extraction else None

    def _snapshot_is_current(self) -> bool:
        transaction = self._snapshot_transaction
        return (
            transaction is not None
            and self.session.sync_session.get_transaction() is transaction
            and transaction.is_active
        )

    def _require_provider_snapshot(self) -> None:
        if self._provider_evaluation and not self._snapshot_is_current():
            raise EvaluationIsolationError(
                "Provider-evaluation queries must run inside extract_dataset's REPEATABLE READ snapshot."
            )

    async def _begin_provider_snapshot(self):
        """
        Begins the session's transaction at REPEATABLE READ so sample selection, the contamination
        guard, labels and feature reconstruction all read one PostgreSQL snapshot. The isolation
        level only applies when the session procures its connection, so a session whose
        transaction has already begun is refused rather than silently left at its current level.
        """
        if self.session.in_transaction():
            raise EvaluationIsolationError(
                "Provider evaluation needs a session with no transaction in progress; "
                "its isolation level cannot be changed once a transaction has begun."
            )
        await self.session.connection(
            execution_options={"isolation_level": PROVIDER_EVALUATION_ISOLATION_LEVEL}
        )
        level = (await self.session.execute(text("SHOW transaction_isolation"))).scalar()
        if str(level).strip().lower() not in _SNAPSHOT_ISOLATION_LEVELS:
            raise EvaluationIsolationError(
                f"Provider evaluation requires REPEATABLE READ or SERIALIZABLE isolation, got {level!r}."
            )
        transaction = self.session.sync_session.get_transaction()
        if transaction is None or not transaction.is_active:
            raise EvaluationIsolationError("Provider evaluation found no active transaction after setup.")
        self._snapshot_transaction = transaction

    async def get_authorized_events(self) -> List[PaymentEventModel]:
        self._require_provider_snapshot()
        stmt = select(PaymentEventModel).where(
            PaymentEventModel.event_type == "payment.authorized",
            PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES)
        ).order_by(PaymentEventModel.ingested_at.asc())
        if self._provider_evaluation:
            stmt = stmt.where(PaymentEventModel.source_system == RAZORPAY_SOURCE_SYSTEM).order_by(
                PaymentEventModel.event_id.asc()
            )
        
        result = await self.session.execute(stmt)
        events = list(result.scalars().all())
        if self._provider_evaluation:
            events = [
                e for e in events
                if not is_legacy_live_context_event(e.payment_id, e.order_id, e.source_event_id)
            ]
        return events

    async def assert_evaluation_window_uncontaminated(
        self, window_start: datetime, window_end: datetime
    ) -> None:
        """
        Refuses provider evaluation when generated events fall inside the window. Snapshots,
        episodes and RCA records carry no source, so contaminated derived features cannot be
        filtered out afterwards.
        """
        self._require_provider_snapshot()
        in_window = or_(
            and_(PaymentEventModel.timestamp >= window_start, PaymentEventModel.timestamp <= window_end),
            and_(PaymentEventModel.ingested_at >= window_start, PaymentEventModel.ingested_at <= window_end),
        )
        non_provider = await self.session.execute(
            select(func.count()).select_from(PaymentEventModel).where(
                in_window,
                PaymentEventModel.source_system.notin_(
                    (RAZORPAY_SOURCE_SYSTEM, *NON_RAZORPAY_PIPELINE_SOURCES)
                ),
            )
        )
        non_provider_count = non_provider.scalar() or 0

        candidates = await self.session.execute(
            select(
                PaymentEventModel.payment_id,
                PaymentEventModel.order_id,
                PaymentEventModel.source_event_id,
            ).where(
                in_window,
                PaymentEventModel.source_system == RAZORPAY_SOURCE_SYSTEM,
                PaymentEventModel.payment_id.startswith(LEGACY_LIVE_CONTEXT_PAYMENT_PREFIX, autoescape=True),
            )
        )
        legacy_count = sum(1 for row in candidates.all() if is_legacy_live_context_event(*row))

        if non_provider_count or legacy_count:
            raise EvaluationDataContaminationError(
                f"Provider evaluation refused: {non_provider_count} non-provider and {legacy_count} "
                f"legacy synthetic live-context payment events between {window_start.isoformat()} and "
                f"{window_end.isoformat()}. Derived Stage 2-5 features in this window may include them; "
                "evaluate against a database that has never held generated data."
            )

    async def get_terminal_label(self, payment_id: str, auth_ingested_at: datetime) -> int:
        """
        Retrieves the earliest terminal event strictly after T.
        Returns 1 for failure, 0 for capture, -1 for unresolved.
        """
        self._require_provider_snapshot()
        stmt = select(PaymentEventModel).where(
            PaymentEventModel.payment_id == payment_id,
            PaymentEventModel.event_type.in_(["payment.captured", "payment.failed"]),
            PaymentEventModel.ingested_at > auth_ingested_at,
            PaymentEventModel.ingested_at <= auth_ingested_at + timedelta(minutes=30),
            PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES)
        ).order_by(PaymentEventModel.ingested_at.asc()).limit(1)
        if self._provider_evaluation:
            stmt = stmt.where(PaymentEventModel.source_system == RAZORPAY_SOURCE_SYSTEM).order_by(
                PaymentEventModel.event_id.asc()
            )
        
        result = await self.session.execute(stmt)
        terminal_event = result.scalars().first()
        
        if not terminal_event:
            return -1 # Unresolved
            
        return 1 if terminal_event.event_type == "payment.failed" else 0

    async def extract_dataset(self) -> Tuple[List[Dict], List[int], List[datetime]]:
        if not self._provider_evaluation:
            auth_events = await self.get_authorized_events()
            return await self._label_and_reconstruct(auth_events)

        self._checked_extraction = None
        await self._begin_provider_snapshot()
        try:
            auth_events = await self.get_authorized_events()
            window = None
            if auth_events:
                sample_times = [e.ingested_at for e in auth_events]
                window = (min(sample_times) - CONTAMINATION_LOOKBACK, max(sample_times) + LABEL_HORIZON)
                await self.assert_evaluation_window_uncontaminated(*window)
            X_raw, y_raw, times = await self._label_and_reconstruct(auth_events)
            if not self._snapshot_is_current():
                raise EvaluationIsolationError(
                    "Provider evaluation transaction ended during extraction; its reads did not share one snapshot."
                )
        finally:
            self._snapshot_transaction = None
        if window is not None:
            self._checked_extraction = _CheckedExtraction(
                window=window, dataset_fingerprint=compute_dataset_fingerprint(X_raw, y_raw, times)
            )
        return X_raw, y_raw, times

    async def _label_and_reconstruct(
        self, auth_events: List[PaymentEventModel]
    ) -> Tuple[List[Dict], List[int], List[datetime]]:
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

    def _require_checked_dataset(self, X_raw, y_raw, times) -> Tuple[_CheckedExtraction, str]:
        checked = self._checked_extraction
        if checked is None:
            raise RuntimeError(
                "Provider evaluation training requires a successful extract_dataset contamination check first."
            )
        if not times:
            raise RuntimeError("Provider evaluation training requires at least one extracted sample.")
        fingerprint = compute_dataset_fingerprint(X_raw, y_raw, times)
        if fingerprint != checked.dataset_fingerprint:
            raise RuntimeError(
                "Provider evaluation inputs do not match the dataset extracted under the contamination check."
            )
        return checked, fingerprint

    def run_pipeline(self, X_raw, y_raw, times):
        if self._provider_evaluation:
            checked, fingerprint = self._require_checked_dataset(X_raw, y_raw, times)
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
        
        metrics_computed = True
        try:
            pr_auc = average_precision_score(y_test, probs)
            roc_auc = roc_auc_score(y_test, probs)
            brier = brier_score_loss(y_test, probs)
        except ValueError:
            # Handle edge cases where test set only has 1 class due to synthetic randomness
            metrics_computed = False
            pr_auc = 0.0
            roc_auc = 0.5
            brier = 0.0
            
        metrics = {
            "pr_auc": pr_auc,
            "roc_auc": roc_auc,
            "brier_score": brier
        }
        
        logger.info(f"SYNTHETIC / DEVELOPMENT - Metrics: {metrics}")
        
        if self._provider_evaluation:
            training_metadata = {
                "training_population": self.population.value,
                "sample_window": {"start": min(times).isoformat(), "end": max(times).isoformat()},
                "sample_count": len(X_raw),
                "metrics": {name: float(value) for name, value in metrics.items()},
                "metrics_computed": metrics_computed,
                "contamination_window": {
                    "start": checked.window[0].isoformat(),
                    "end": checked.window[1].isoformat(),
                },
                "contamination_lookback_seconds": int(CONTAMINATION_LOOKBACK.total_seconds()),
                "label_horizon_seconds": int(LABEL_HORIZON.total_seconds()),
                "dataset_fingerprint": fingerprint,
            }
            model_instance = ProviderLogisticRegressionModel(
                eval_model,
                feature_names,
                encoders,
                model_version=f"provider-eval-{fingerprint[:12]}",
                training_metadata=training_metadata,
            )
        else:
            model_instance = SyntheticLogisticRegressionModel(eval_model, feature_names, encoders)
        
        return model_instance, metrics
