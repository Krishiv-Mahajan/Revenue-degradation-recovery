"""
Milestone 1 provider isolation: PayPal events must not reach the existing Stage 2-5 reads.

Each changed query is executed against a mocked session; the captured statement is compiled
with the PostgreSQL dialect and its source_system exclusion is checked against the bound
parameter values. No database is used.
"""
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import TextClause

from src.core.domain import source_systems
from src.core.domain.source_systems import NON_RAZORPAY_PIPELINE_SOURCES, PAYPAL_SOURCE_SYSTEM
from src.core.ml.training import Stage5PipelineValidator
from src.core.normalizers import paypal as paypal_normalizer
from src.core.rca.service import RCAService
from src.core.services.failure_prediction_service import FailurePredictionService
from src.infrastructure.analytics_repository import AnalyticsRepository
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.infrastructure.models import PaymentEventModel

PG = postgresql.dialect()
T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
WINDOW_END = T + timedelta(minutes=5)

_ORM_EXCLUSION = re.compile(r"payment_events\.source_system NOT IN \(__\[POSTCOMPILE_(\w+)\]\)")
_TEXT_EXCLUSION = re.compile(r"\bsource_system NOT IN \(__\[POSTCOMPILE_(\w+)\]\)")


def _session():
    session = AsyncMock()
    session.execute.return_value = MagicMock()
    return session


async def _analytics_window(session):
    await AnalyticsRepository(session).get_events_in_window(T, WINDOW_END)


async def _analytics_late(session):
    await AnalyticsRepository(session).get_events_ingested_after(T)


async def _rca_episode_window(session):
    await RCAService(session, MagicMock())._get_events_in_window(T, WINDOW_END)


async def _rca_hist_error_counts(session):
    await RCAService(session, MagicMock())._get_hist_error_counts([T - timedelta(weeks=1)], "error_code", "BAD_GATEWAY")


async def _rca_hist_events_for_dim(session):
    await RCAService(session, MagicMock())._get_hist_events_for_dim([T - timedelta(weeks=1)], "bank", "HDFC")


async def _feature_rate_currency(session):
    await FeatureReconstructionRepository(session).get_historical_failure_rate(PaymentEventModel.currency, "INR", T, 30)


async def _feature_rate_global(session):
    await FeatureReconstructionRepository(session).get_historical_failure_rate(
        PaymentEventModel.source_system, "razorpay", T, 30
    )


async def _feature_auth_event(session):
    await FeatureReconstructionRepository(session).get_payment_event_by_id("pay_1", T)


def _prediction_service(session):
    return FailurePredictionService(session, MagicMock(), MagicMock(), MagicMock())


async def _prediction_eligibility(session):
    await _prediction_service(session).is_eligible_for_prediction("pay_1", T)


async def _prediction_terminal_label(session):
    await _prediction_service(session).lookup_terminal_label("pay_1", T, observation_window_minutes=30)


async def _training_authorized_events(session):
    await Stage5PipelineValidator(session).get_authorized_events()


async def _training_terminal_label(session):
    await Stage5PipelineValidator(session).get_terminal_label("pay_1", T)


# (query, module holding the constant, expected statement count, existing predicates that must remain)
CASES = {
    "analytics.get_events_in_window": (
        _analytics_window, "src.infrastructure.analytics_repository", 1,
        ["payment_events.timestamp >= ", "payment_events.timestamp < "],
    ),
    "analytics.get_events_ingested_after": (
        _analytics_late, "src.infrastructure.analytics_repository", 1,
        ["payment_events.ingested_at > "],
    ),
    "rca._get_events_in_window": (
        _rca_episode_window, "src.core.rca.service", 1,
        ["payment_events.timestamp >= ", "payment_events.timestamp < "],
    ),
    "rca._get_hist_error_counts": (
        _rca_hist_error_counts, "src.core.rca.service", 2,
        ["payment_events.event_type = "],
    ),
    "rca._get_hist_events_for_dim": (
        _rca_hist_events_for_dim, "src.core.rca.service", 2,
        ["payment_events.bank = "],
    ),
    "feature.get_historical_failure_rate[currency]": (
        _feature_rate_currency, "src.infrastructure.feature_reconstruction_repository", 1,
        ["payment_events.currency = ", "payment_events.event_type IN "],
    ),
    "feature.get_historical_failure_rate[global]": (
        _feature_rate_global, "src.infrastructure.feature_reconstruction_repository", 1,
        ["payment_events.source_system = ", "payment_events.event_type IN "],
    ),
    "feature.get_payment_event_by_id": (
        _feature_auth_event, "src.infrastructure.feature_reconstruction_repository", 1,
        ["payment_events.payment_id = ", "payment_events.event_type = "],
    ),
    "prediction.is_eligible_for_prediction": (
        _prediction_eligibility, "src.core.services.failure_prediction_service", 1,
        ["WHERE payment_id = ", "event_type IN ('payment.captured', 'payment.failed')", "ingested_at < "],
    ),
    "prediction.lookup_terminal_label": (
        _prediction_terminal_label, "src.core.services.failure_prediction_service", 1,
        ["payment_events.payment_id = ", "payment_events.ingested_at >= ", "payment_events.ingested_at <= "],
    ),
    "training.get_authorized_events": (
        _training_authorized_events, "src.core.ml.training", 1,
        ["payment_events.event_type = ", "ORDER BY payment_events.ingested_at ASC"],
    ),
    "training.get_terminal_label": (
        _training_terminal_label, "src.core.ml.training", 1,
        ["payment_events.payment_id = ", "payment_events.ingested_at > ", "payment_events.ingested_at <= "],
    ),
}


async def _executed(query):
    session = _session()
    await query(session)
    return [(call.args[0], call.args[1] if len(call.args) > 1 else {}) for call in session.execute.call_args_list]


def _compile(statement):
    compiled = statement.compile(dialect=PG)
    return str(compiled), compiled.params


def _excluded_sources(statement, executed_params):
    """Returns the values bound to the statement's single source_system exclusion."""
    sql, compiled_params = _compile(statement)
    if isinstance(statement, TextClause):
        names = _TEXT_EXCLUSION.findall(sql)
        assert len(names) == 1, sql
        return list(executed_params[names[0]])
    names = _ORM_EXCLUSION.findall(sql)
    assert len(names) == 1, sql
    return list(compiled_params[names[0]])


@pytest.mark.parametrize("case", list(CASES))
async def test_query_excludes_paypal_via_bound_parameter(case):
    query, _, expected_statements, existing_predicates = CASES[case]
    executed = await _executed(query)

    assert len(executed) == expected_statements
    for statement, params in executed:
        assert _excluded_sources(statement, params) == [PAYPAL_SOURCE_SYSTEM]
        sql, _ = _compile(statement)
        for predicate in existing_predicates:
            assert predicate in sql, f"{case} lost existing predicate {predicate!r}"


@pytest.mark.parametrize("case", list(CASES))
async def test_query_reads_the_shared_constant(case, monkeypatch):
    query, module, _, _ = CASES[case]
    monkeypatch.setattr(f"{module}.NON_RAZORPAY_PIPELINE_SOURCES", ("sentinel-source",))
    for statement, params in await _executed(query):
        assert _excluded_sources(statement, params) == ["sentinel-source"]


async def test_hist_error_counts_isolates_both_statements():
    executed = await _executed(_rca_hist_error_counts)
    total_sql, error_sql = (_compile(statement)[0] for statement, _ in executed)
    assert "payment_events.error_code = " not in total_sql
    assert "payment_events.error_code = " in error_sql
    for statement, params in executed:
        assert _excluded_sources(statement, params) == [PAYPAL_SOURCE_SYSTEM]


async def test_eligibility_raw_sql_binds_the_exclusion_as_an_expanding_parameter():
    [(statement, params)] = await _executed(_prediction_eligibility)
    assert isinstance(statement, TextClause)
    assert "source_system NOT IN :excluded_sources" in statement.text
    assert "paypal" not in statement.text
    assert params == {"payment_id": "pay_1", "T": T, "excluded_sources": [PAYPAL_SOURCE_SYSTEM]}


async def test_global_feature_keeps_its_source_dimension_filter():
    [(statement, _)] = await _executed(_feature_rate_global)
    sql, compiled_params = _compile(statement)
    match = re.search(r"payment_events\.source_system = %\((\w+)\)s", sql)
    assert match and compiled_params[match.group(1)] == "razorpay"


def test_excluded_sources_contain_only_paypal():
    assert NON_RAZORPAY_PIPELINE_SOURCES == (PAYPAL_SOURCE_SYSTEM,) == ("paypal",)


@pytest.mark.parametrize("source", ["razorpay", "synthetic", "TEST", "test", "test_system"])
def test_existing_sources_are_not_excluded(source):
    assert source not in NON_RAZORPAY_PIPELINE_SOURCES


def test_normalizer_sources_match_the_isolation_constant():
    assert paypal_normalizer.SOURCE_SYSTEM == PAYPAL_SOURCE_SYSTEM
    assert "razorpay" not in NON_RAZORPAY_PIPELINE_SOURCES


@pytest.mark.parametrize("module", sorted({module for _, module, _, _ in CASES.values()}))
def test_modules_import_the_shared_constant(module):
    import importlib
    assert importlib.import_module(module).NON_RAZORPAY_PIPELINE_SOURCES is source_systems.NON_RAZORPAY_PIPELINE_SOURCES
