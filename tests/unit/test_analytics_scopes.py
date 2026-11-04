"""Stage 2-4 analytics scopes: per-scope dimensions, episode identity, and schema parity."""
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.analytics.calculator import calculate_health_metrics_for_window
from src.core.degradation.state_machine import reconcile_timeline
from src.core.domain.analytics_scopes import (
    ANALYTICS_SCOPES, PAYPAL_SCOPE, RAZORPAY_PIPELINE_DIMENSIONS, RAZORPAY_PIPELINE_SCOPE, SCOPE_CONFIG,
    episode_identity_name, scope_for_source, validate_scope,
)
from src.core.domain.degradation_models import NAMESPACE_EPISODE, DegradationSignal, EpisodeStatus, SignalType
from src.core.domain.models import PaymentEvent
from src.core.domain.rca_models import STRUCTURAL_DIMENSIONS, SUPPORTING_DIMENSIONS
from src.infrastructure.models import ANALYTICS_SCOPE_CHECK

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/c9d0e1f2a3b4_stage2_4_provider_scope.py"


def _event(source, event_type, *, currency="USD", at=T0, **fields):
    return PaymentEvent(
        event_id=uuid.uuid4(), source_system=source, source_event_id=str(uuid.uuid4()),
        payment_id=str(uuid.uuid4()), order_id="O1", timestamp=at + timedelta(minutes=1), event_type=event_type,
        currency=currency, amount_minor_units=1000,
        payment_status="captured" if event_type == "payment.captured" else "failed",
        ingested_at=at + timedelta(minutes=1), **fields,
    )


def _signal(window_start, signal_type, provider_scope):
    return DegradationSignal(
        signal_id=uuid.uuid4(), snapshot_id=uuid.uuid4(), segment_dimension="GLOBAL", segment_value="ALL",
        window_start=window_start, evaluation_version=1, signal_type=signal_type, baseline_success_rate=0.95,
        absolute_drop=0.4 if signal_type == SignalType.BAD else 0.0, relative_drop=0.4,
        evaluation_timestamp=window_start + timedelta(minutes=5), provider_scope=provider_scope,
    )


@pytest.mark.parametrize("source, scope", [
    ("razorpay", RAZORPAY_PIPELINE_SCOPE), ("synthetic", RAZORPAY_PIPELINE_SCOPE), ("TEST", RAZORPAY_PIPELINE_SCOPE),
    ("paypal", PAYPAL_SCOPE),
])
def test_each_provider_belongs_to_exactly_one_scope(source, scope):
    assert scope_for_source(source) == scope


def test_unknown_scopes_are_rejected():
    with pytest.raises(ValueError, match="Unknown analytics scope"):
        validate_scope("stripe")
    with pytest.raises(ValueError, match="Unknown analytics scope"):
        episode_identity_name("stripe", "GLOBAL", "ALL", T0.isoformat())


def test_original_scope_configuration_is_unchanged():
    config = SCOPE_CONFIG[RAZORPAY_PIPELINE_SCOPE]
    assert config.stage2_dimensions == ("GLOBAL", "currency", "payment_method", "bank", "wallet", "error_source")
    assert config.stage2_dimensions == RAZORPAY_PIPELINE_DIMENSIONS
    assert (config.rca_structural_dimensions, config.rca_supporting_dimensions) == (STRUCTURAL_DIMENSIONS, SUPPORTING_DIMENSIONS)
    assert config.exclude_population_wide_candidates is False


def test_paypal_scope_only_uses_fields_paypal_events_carry():
    config = SCOPE_CONFIG[PAYPAL_SCOPE]
    assert config.stage2_dimensions == ("GLOBAL", "currency")
    assert config.rca_structural_dimensions == ("CURRENCY",)
    assert config.rca_supporting_dimensions == ("ERROR_REASON",)
    assert config.exclude_population_wide_candidates is True


def test_original_scope_keeps_its_historical_episode_ids():
    start = T0.isoformat()
    assert episode_identity_name(RAZORPAY_PIPELINE_SCOPE, "bank", "HDFC", start) == f"bank:HDFC:{start}"


def test_equal_segments_in_two_scopes_never_share_an_episode():
    start = T0.isoformat()
    legacy = uuid.uuid5(NAMESPACE_EPISODE, episode_identity_name(RAZORPAY_PIPELINE_SCOPE, "GLOBAL", "ALL", start))
    paypal = uuid.uuid5(NAMESPACE_EPISODE, episode_identity_name(PAYPAL_SCOPE, "GLOBAL", "ALL", start))
    assert episode_identity_name(PAYPAL_SCOPE, "GLOBAL", "ALL", start) == f"paypal|GLOBAL:ALL:{start}"
    assert legacy != paypal


@pytest.mark.parametrize("scope", ANALYTICS_SCOPES)
def test_hysteresis_is_identical_in_every_scope(scope):
    windows = [T0 + timedelta(minutes=5 * i) for i in range(8)]
    pattern = [SignalType.BAD, SignalType.BAD, SignalType.LOW_VOLUME, SignalType.BAD,
               SignalType.NORMAL, SignalType.NORMAL, SignalType.NORMAL, SignalType.NORMAL]
    [episode] = reconcile_timeline([_signal(w, t, scope) for w, t in zip(windows, pattern)])

    assert episode.provider_scope == scope
    assert episode.started_at_window == T0
    assert episode.affected_window_count == 3
    assert episode.status == EpisodeStatus.RECOVERED
    assert episode.episode_id == uuid.uuid5(
        NAMESPACE_EPISODE, episode_identity_name(scope, "GLOBAL", "ALL", T0.isoformat())
    )


def test_stage2_segments_each_scope_by_its_own_dimensions():
    window_end = T0 + timedelta(minutes=5)
    razorpay = [_event("razorpay", "payment.failed", currency="INR", payment_method="card", bank="HDFC",
                       error_source="bank")]
    paypal = [_event("paypal", "payment.failed", currency="USD", error_reason="INSTRUMENT_DECLINED"),
              _event("paypal", "payment.captured", currency="EUR")]

    legacy = calculate_health_metrics_for_window(razorpay, T0, window_end)
    scoped = calculate_health_metrics_for_window(paypal, T0, window_end, provider_scope=PAYPAL_SCOPE)

    assert {s.segment_dimension for s in legacy} == set(RAZORPAY_PIPELINE_DIMENSIONS)
    assert {s.provider_scope for s in legacy} == {RAZORPAY_PIPELINE_SCOPE}
    assert {(s.segment_dimension, s.segment_value) for s in scoped} == {("GLOBAL", "ALL"), ("currency", "USD"), ("currency", "EUR")}
    assert {s.provider_scope for s in scoped} == {PAYPAL_SCOPE}


def test_schema_check_constraint_matches_the_scope_list():
    spec = importlib.util.spec_from_file_location("revision_f", MIGRATION)
    revision_f = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision_f)
    expected = "provider_scope IN (" + ", ".join(f"'{s}'" for s in ANALYTICS_SCOPES) + ")"

    assert ANALYTICS_SCOPE_CHECK == revision_f.SCOPE_CHECK == expected
    assert revision_f.DEFAULT_SCOPE == RAZORPAY_PIPELINE_SCOPE
