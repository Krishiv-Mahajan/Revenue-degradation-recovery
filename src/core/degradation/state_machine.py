from typing import List, Optional
import uuid
from datetime import datetime

from src.core.domain.degradation_models import (
    DegradationSignal,
    DegradationEpisode,
    SignalType,
    EpisodeStatus,
    Severity,
    StateMachineState,
    REQUIRED_CONSECUTIVE_WINDOWS,
    RECOVERY_CONSECUTIVE_WINDOWS,
    NAMESPACE_EPISODE
)
from src.core.degradation.severity import escalate_severity, evaluate_severity

def reconcile_timeline(signals: List[DegradationSignal]) -> List[DegradationEpisode]:
    """
    Reconstructs the episode state given a chronologically ordered list of signals.
    Returns all episodes that were logically created during this timeline.
    """
    if not signals:
        return []
        
    segment_dimension = signals[0].segment_dimension
    segment_value = signals[0].segment_value
    
    current_state = StateMachineState.HEALTHY
    persistence_count = 0
    recovery_count = 0
    first_bad_window = None
    last_bad_window = None
    pending_signals = []
    
    current_episode: Optional[DegradationEpisode] = None
    episodes: List[DegradationEpisode] = []

    for signal in signals:
        sig_type = signal.signal_type
        
        if current_state == StateMachineState.HEALTHY:
            if sig_type == SignalType.BAD:
                current_state = StateMachineState.BAD_PENDING
                persistence_count = 1
                first_bad_window = signal.window_start
                pending_signals = [signal]
            elif sig_type == SignalType.NORMAL:
                pass
            elif sig_type in (SignalType.LOW_VOLUME, SignalType.NO_BASELINE):
                pass
                
        elif current_state == StateMachineState.BAD_PENDING:
            if sig_type == SignalType.BAD:
                persistence_count += 1
                pending_signals.append(signal)
                if persistence_count >= REQUIRED_CONSECUTIVE_WINDOWS:
                    current_state = StateMachineState.DEGRADED
                    
                    episode_id = uuid.uuid5(NAMESPACE_EPISODE, f"{segment_dimension}:{segment_value}:{first_bad_window.isoformat()}")
                    
                    peak_abs = max((s.absolute_drop for s in pending_signals if s.absolute_drop is not None), default=0.0)
                    sev = Severity.MODERATE
                    for s in pending_signals:
                        sev = escalate_severity(sev, evaluate_severity(s, s.baseline_success_rate or 0.0))
                    
                    current_episode = DegradationEpisode(
                        episode_id=episode_id,
                        segment_dimension=segment_dimension,
                        segment_value=segment_value,
                        started_at_window=first_bad_window,
                        status=EpisodeStatus.ACTIVE,
                        peak_absolute_drop=peak_abs,
                        affected_window_count=persistence_count,
                        severity=sev,
                        ended_at_window=None
                    )
            elif sig_type == SignalType.NORMAL:
                current_state = StateMachineState.HEALTHY
                persistence_count = 0
                first_bad_window = None
                pending_signals = []
            elif sig_type in (SignalType.LOW_VOLUME, SignalType.NO_BASELINE):
                pass
                
        elif current_state == StateMachineState.DEGRADED:
            if sig_type == SignalType.BAD:
                current_episode.affected_window_count += 1
                if signal.absolute_drop is not None and signal.absolute_drop > current_episode.peak_absolute_drop:
                    current_episode.peak_absolute_drop = signal.absolute_drop
                new_sev = evaluate_severity(signal, signal.baseline_success_rate or 0.0)
                current_episode.severity = escalate_severity(current_episode.severity, new_sev)
                
            elif sig_type == SignalType.NORMAL:
                current_state = StateMachineState.RECOVERY_PENDING
                recovery_count = 1
            elif sig_type in (SignalType.LOW_VOLUME, SignalType.NO_BASELINE):
                pass
                
        elif current_state == StateMachineState.RECOVERY_PENDING:
            if sig_type == SignalType.NORMAL:
                recovery_count += 1
                if recovery_count >= RECOVERY_CONSECUTIVE_WINDOWS:
                    current_state = StateMachineState.HEALTHY
                    current_episode.status = EpisodeStatus.RECOVERED
                    current_episode.ended_at_window = last_bad_window
                    
                    episodes.append(current_episode)
                    current_episode = None
                    recovery_count = 0
                    first_bad_window = None
            elif sig_type == SignalType.BAD:
                current_state = StateMachineState.DEGRADED
                current_episode.affected_window_count += 1
                if signal.absolute_drop is not None and signal.absolute_drop > current_episode.peak_absolute_drop:
                    current_episode.peak_absolute_drop = signal.absolute_drop
                new_sev = evaluate_severity(signal, signal.baseline_success_rate or 0.0)
                current_episode.severity = escalate_severity(current_episode.severity, new_sev)
                recovery_count = 0
            elif sig_type in (SignalType.LOW_VOLUME, SignalType.NO_BASELINE):
                pass

        if sig_type == SignalType.BAD:
            last_bad_window = signal.window_start

    if current_episode is not None:
        episodes.append(current_episode)
        
    return episodes
