"""
Stage 3 statistical evidence: the Z-score of a window's success rate against its own history.

    z = (success_rate - baseline_mean) / max(baseline_stddev, MIN_STDDEV_FLOOR)

baseline_mean, baseline_stddev (sample standard deviation) and the sample count come from Stage 2, over
the same historical windows (same weekday and time of day, 4 weeks back) as the median baseline.

Guards, each reported as a status instead of a number:
- COLD_START: no historical windows with a success rate.
- INSUFFICIENT_OBSERVATIONS: fewer than MIN_ZSCORE_OBSERVATIONS historical rates.
- MISSING_RATE: the window itself has no success rate.
- LOW_VARIANCE: the historical spread is below MIN_STDDEV_FLOOR. The floor is used as the denominator, so
  a perfectly stable history cannot turn a tiny dip into an enormous Z-score.

Detection modes (DEGRADATION_DETECTION_MODE):
- "threshold" (default): the frozen threshold rule decides; Z-score evidence is recorded only.
- "threshold_and_zscore": a threshold BAD window stays BAD only when the Z-score, where it can be computed,
  is at or below -ZSCORE_BAD_THRESHOLD. This prevents false positives in naturally volatile segments.
  Without a usable Z-score (cold start, too few observations) the threshold rule decides alone, so cold
  starts are never blinded. NORMAL never becomes BAD on a Z-score alone, and hysteresis (consecutive-window
  persistence) is unchanged because it consumes the resulting signal types.
"""
import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional

MIN_ZSCORE_OBSERVATIONS = 3
MIN_STDDEV_FLOOR = 0.02
ZSCORE_BAD_THRESHOLD = 3.0
DETECTION_MODE_ENV = "DEGRADATION_DETECTION_MODE"


class ZScoreStatus(str, Enum):
    OK = "OK"
    LOW_VARIANCE = "LOW_VARIANCE"
    COLD_START = "COLD_START"
    INSUFFICIENT_OBSERVATIONS = "INSUFFICIENT_OBSERVATIONS"
    MISSING_RATE = "MISSING_RATE"


class DetectionMode(str, Enum):
    THRESHOLD = "threshold"
    THRESHOLD_AND_ZSCORE = "threshold_and_zscore"


USABLE_STATUSES = frozenset({ZScoreStatus.OK, ZScoreStatus.LOW_VARIANCE})


@dataclass(frozen=True)
class ZScoreEvidence:
    z_score: Optional[float]
    status: ZScoreStatus

    @property
    def usable(self) -> bool:
        return self.status in USABLE_STATUSES and self.z_score is not None


def compute_z_score(
    success_rate: Optional[float],
    baseline_mean: Optional[float],
    baseline_stddev: Optional[float],
    sample_count: Optional[int],
) -> ZScoreEvidence:
    if not sample_count or baseline_mean is None:
        return ZScoreEvidence(None, ZScoreStatus.COLD_START)
    if sample_count < MIN_ZSCORE_OBSERVATIONS or baseline_stddev is None:
        return ZScoreEvidence(None, ZScoreStatus.INSUFFICIENT_OBSERVATIONS)
    if success_rate is None:
        return ZScoreEvidence(None, ZScoreStatus.MISSING_RATE)
    status = ZScoreStatus.LOW_VARIANCE if baseline_stddev < MIN_STDDEV_FLOOR else ZScoreStatus.OK
    z = (success_rate - baseline_mean) / max(baseline_stddev, MIN_STDDEV_FLOOR)
    return ZScoreEvidence(round(z, 6), status)


def confirms_degradation(evidence: ZScoreEvidence) -> bool:
    """Whether usable Z-score evidence agrees that the window is unusually bad (z <= -threshold)."""
    return evidence.usable and evidence.z_score <= -ZSCORE_BAD_THRESHOLD


def detection_mode(env: Optional[Mapping[str, str]] = None) -> DetectionMode:
    """The configured mode; anything unrecognised keeps the frozen threshold rule."""
    env = os.environ if env is None else env
    try:
        return DetectionMode((env.get(DETECTION_MODE_ENV) or DetectionMode.THRESHOLD.value).strip().lower())
    except ValueError:
        return DetectionMode.THRESHOLD
