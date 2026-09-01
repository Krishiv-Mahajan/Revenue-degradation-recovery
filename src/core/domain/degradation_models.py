from enum import Enum
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import uuid

# Configuration parameters
MIN_ABSOLUTE_RATE_DROP = 0.05
MIN_RELATIVE_RATE_DROP = 0.10
MIN_BASELINE_RATE_FOR_RELATIVE_TEST = 0.05
REQUIRED_CONSECUTIVE_WINDOWS = 3
RECOVERY_CONSECUTIVE_WINDOWS = 2
SEVERITY_HIGH_ABSOLUTE_DROP = 0.15
SEVERITY_HIGH_RELATIVE_DROP = 0.25
SEVERITY_CRITICAL_ABSOLUTE_DROP = 0.30
SEVERITY_CRITICAL_RELATIVE_DROP = 0.50

NAMESPACE_EPISODE = uuid.UUID("3d2c8c4a-67a1-4389-9a70-8b1b017f8d44")

class SignalType(str, Enum):
    NORMAL = "NORMAL"
    BAD = "BAD"
    LOW_VOLUME = "LOW_VOLUME"
    NO_BASELINE = "NO_BASELINE"

class EpisodeStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RECOVERED = "RECOVERED"
    INVALIDATED = "INVALIDATED"

class Severity(str, Enum):
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

class StateMachineState(str, Enum):
    HEALTHY = "HEALTHY"
    BAD_PENDING = "BAD_PENDING"
    DEGRADED = "DEGRADED"
    RECOVERY_PENDING = "RECOVERY_PENDING"

@dataclass
class DegradationSignal:
    signal_id: uuid.UUID
    snapshot_id: uuid.UUID
    segment_dimension: str
    segment_value: str
    window_start: datetime
    evaluation_version: int
    signal_type: SignalType
    baseline_success_rate: Optional[float]
    absolute_drop: Optional[float]
    relative_drop: Optional[float]
    evaluation_timestamp: datetime

@dataclass
class DegradationEpisode:
    episode_id: uuid.UUID
    segment_dimension: str
    segment_value: str
    started_at_window: datetime
    status: EpisodeStatus
    peak_absolute_drop: float
    affected_window_count: int
    severity: Severity
    ended_at_window: Optional[datetime] = None
