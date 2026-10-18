"""
Stage 5 training populations: the default SYNTHETIC_DEVELOPMENT extract is unchanged, while the
opt-in PROVIDER_EVALUATION extract uses genuine Razorpay samples and labels only and refuses to run
when generated data could have shaped the derived features of its evaluation window.

All queries run against mocked sessions; no database is used.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from scripts.generate_live_context import seed_upstream_synthetic_traffic
from src.core.domain.source_systems import NON_RAZORPAY_PIPELINE_SOURCES
from src.core.ml.training import (
    CONTAMINATION_LOOKBACK,
    LABEL_HORIZON,
    EvaluationDataContaminationError,
    EvaluationIsolationError,
    Stage5PipelineValidator,
    TrainingPopulation,
    is_legacy_live_context_event,
)
from src.infrastructure.models import PaymentEventModel

PG = postgresql.dialect()
T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
TOKEN = "0123456789ab"
LEGACY = (f"pay_live_ctx_{TOKEN}", f"order_live_ctx_{TOKEN}", f"evt_live_auth_{TOKEN}")
GENUINE = ("pay_P1cOverlap00001", "order_P1cControl0001", "evt_live_1")


def _compile(statement):
    compiled = statement.compile(dialect=PG)
    return str(compiled), compiled.params


def _row(payment_id, order_id, source_event_id, ingested_at=T):
    return SimpleNamespace(payment_id=payment_id, order_id=order_id, source_event_id=source_event_id,
                           ingested_at=ingested_at)


def _session_returning_rows(rows):
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session.execute.return_value = result
    return session


def _snapshot_session(isolation="repeatable read", transaction_began=False, calls=None, results=None):
    """
    A session double for provider evaluation's transaction setup: no transaction in progress, a
    connection procured with execution options, SHOW transaction_isolation answered with `isolation`,
    other statements answered from `results` in order, and one root transaction that stays active
    unless a test replaces it.
    """
    calls = [] if calls is None else calls
    results = list(results or [])
    session = MagicMock()
    session.in_transaction = MagicMock(return_value=transaction_began)

    async def connection(**kwargs):
        calls.append(("connection", kwargs))

    show_result = MagicMock()
    show_result.scalar.return_value = isolation

    async def execute(statement, *args, **kwargs):
        calls.append(("execute", str(statement)))
        if str(statement) == "SHOW transaction_isolation":
            return show_result
        return results.pop(0) if results else MagicMock()

    session.connection = AsyncMock(side_effect=connection)
    session.execute = AsyncMock(side_effect=execute)
    session.transaction = SimpleNamespace(is_active=True)
    session.sync_session.get_transaction = MagicMock(side_effect=lambda: session.transaction)
    return session


def _rows_result(rows):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    result.scalars.return_value.first.return_value = rows[0] if rows else None
    return result


def _guard_session(non_provider_count, candidate_rows):
    count_result, candidate_result = MagicMock(), MagicMock()
    count_result.scalar.return_value = non_provider_count
    candidate_result.all.return_value = candidate_rows
    return _snapshot_session(results=[count_result, candidate_result])


def _data_calls(session):
    """The session's executed statements other than the isolation check."""
    return [c for c in session.execute.call_args_list if str(c.args[0]) != "SHOW transaction_isolation"]


async def _in_snapshot(validator):
    await validator._begin_provider_snapshot()
    return validator


# --- legacy live-context identification -------------------------------------------------------

async def test_every_event_written_by_the_live_context_generator_is_identified():
    session = AsyncMock()
    session.add_all = MagicMock()
    windows = [(T, T + timedelta(minutes=5))]
    await seed_upstream_synthetic_traffic(session, windows, pm="card", curr="INR", seed=42)
    raw_records, payment_events = (call.args[0] for call in session.add_all.call_args_list)

    assert len(payment_events) == 120
    assert all(is_legacy_live_context_event(e.payment_id, e.order_id, e.source_event_id) for e in payment_events)
    assert all(is_legacy_live_context_event(e.payment_id, e.order_id, r.source_event_id)
               for e, r in zip(payment_events, raw_records))


@pytest.mark.parametrize("ids", [
    pytest.param(LEGACY, id="authorization"),
    pytest.param((LEGACY[0], LEGACY[1], f"evt_live_term_{TOKEN}"), id="terminal"),
])
def test_legacy_ids_with_one_shared_token_are_identified(ids):
    assert is_legacy_live_context_event(*ids)


@pytest.mark.parametrize("ids", [
    pytest.param(GENUINE, id="genuine-razorpay-and-test-fixture-ids"),
    pytest.param(("pay_P1cOverlap00001", LEGACY[1], LEGACY[2]), id="genuine-payment-id"),
    pytest.param((LEGACY[0], LEGACY[1], "evt_live_1"), id="test-fixture-event-id"),
    pytest.param((LEGACY[0], "order_live_ctx_ba9876543210", LEGACY[2]), id="order-token-mismatch"),
    pytest.param((LEGACY[0], LEGACY[1], "evt_live_auth_ba9876543210"), id="event-token-mismatch"),
    pytest.param((LEGACY[0], None, LEGACY[2]), id="missing-order-id"),
    pytest.param((LEGACY[0], LEGACY[1], None), id="missing-event-id"),
    pytest.param((None, LEGACY[1], LEGACY[2]), id="missing-payment-id"),
    pytest.param((f"pay_live_ctx_{TOKEN.upper()}", f"order_live_ctx_{TOKEN.upper()}",
                  f"evt_live_auth_{TOKEN.upper()}"), id="uppercase-token"),
    pytest.param((f"pay_live_ctx_{TOKEN[:-1]}", f"order_live_ctx_{TOKEN[:-1]}",
                  f"evt_live_auth_{TOKEN[:-1]}"), id="short-token"),
    pytest.param((f"pay_live_ctx_{TOKEN}0", f"order_live_ctx_{TOKEN}0", f"evt_live_auth_{TOKEN}0"),
                 id="long-token"),
    pytest.param((f"payXliveXctxX{TOKEN}", LEGACY[1], LEGACY[2]), id="underscore-wildcard-lookalike"),
    pytest.param((f"{LEGACY[0]}\n", LEGACY[1], LEGACY[2]), id="trailing-newline"),
    pytest.param(("pay_live_ctx_", "order_live_ctx_", "evt_live_auth_"), id="prefixes-only"),
])
def test_ids_that_do_not_fully_match_the_generator_are_not_legacy(ids):
    assert not is_legacy_live_context_event(*ids)


# --- default mode: unchanged synthetic-development behavior -------------------------------------

def _original_authorized_statement():
    return select(PaymentEventModel).where(
        PaymentEventModel.event_type == "payment.authorized",
        PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES)
    ).order_by(PaymentEventModel.ingested_at.asc())


def _original_label_statement(payment_id, auth_ingested_at):
    return select(PaymentEventModel).where(
        PaymentEventModel.payment_id == payment_id,
        PaymentEventModel.event_type.in_(["payment.captured", "payment.failed"]),
        PaymentEventModel.ingested_at > auth_ingested_at,
        PaymentEventModel.ingested_at <= auth_ingested_at + timedelta(minutes=30),
        PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES)
    ).order_by(PaymentEventModel.ingested_at.asc()).limit(1)


def test_default_population_is_synthetic_development():
    assert Stage5PipelineValidator(AsyncMock()).population is TrainingPopulation.SYNTHETIC_DEVELOPMENT


async def test_default_authorized_events_statement_is_unchanged():
    session = _session_returning_rows([])
    await Stage5PipelineValidator(session).get_authorized_events()
    [call] = session.execute.call_args_list
    assert _compile(call.args[0]) == _compile(_original_authorized_statement())


async def test_default_terminal_label_statement_is_unchanged():
    session = _session_returning_rows([])
    await Stage5PipelineValidator(session).get_terminal_label("pay_1", T)
    [call] = session.execute.call_args_list
    assert _compile(call.args[0]) == _compile(_original_label_statement("pay_1", T))


async def test_default_mode_keeps_legacy_rows():
    rows = [_row(*LEGACY), _row(*GENUINE)]
    returned = await Stage5PipelineValidator(_session_returning_rows(rows)).get_authorized_events()
    assert returned == rows


async def test_default_extract_never_runs_the_contamination_guard():
    validator = Stage5PipelineValidator(AsyncMock())
    validator.get_authorized_events = AsyncMock(return_value=[_row(*LEGACY)])
    validator.get_terminal_label = AsyncMock(return_value=-1)
    validator.assert_evaluation_window_uncontaminated = AsyncMock()

    await validator.extract_dataset()

    validator.assert_evaluation_window_uncontaminated.assert_not_awaited()
    validator.get_terminal_label.assert_awaited_once()


async def test_default_extract_leaves_the_session_transaction_untouched():
    session = _snapshot_session()
    validator = Stage5PipelineValidator(session)
    validator.get_authorized_events = AsyncMock(return_value=[_row(*GENUINE)])
    validator.get_terminal_label = AsyncMock(return_value=-1)

    await validator.extract_dataset()

    session.in_transaction.assert_not_called()
    session.connection.assert_not_awaited()
    session.execute.assert_not_awaited()
    session.sync_session.get_transaction.assert_not_called()


# --- provider-evaluation mode: genuine Razorpay samples and labels ------------------------------

def _provider_validator(session):
    return Stage5PipelineValidator(session, population=TrainingPopulation.PROVIDER_EVALUATION)


async def _provider_statement(query):
    session = _snapshot_session(results=[_rows_result([])])
    validator = await _in_snapshot(_provider_validator(session))
    if query == "authorized":
        await validator.get_authorized_events()
        original = _original_authorized_statement()
    else:
        await validator.get_terminal_label("pay_1", T)
        original = _original_label_statement("pay_1", T)
    [call] = _data_calls(session)
    return _compile(call.args[0]), _compile(original)


@pytest.mark.parametrize("query", ["authorized", "label"])
async def test_provider_mode_selects_razorpay_events_only(query):
    (sql, params), (original_sql, _) = await _provider_statement(query)

    assert "payment_events.source_system = %(source_system_2)s" in sql
    assert params["source_system_2"] == "razorpay"
    assert list(params["source_system_1"]) == list(NON_RAZORPAY_PIPELINE_SOURCES)
    provider_only = (" AND payment_events.source_system = %(source_system_2)s", ", payment_events.event_id ASC")
    for fragment in provider_only:
        sql = sql.replace(fragment, "")
    assert sql == original_sql


@pytest.mark.parametrize("query", ["authorized", "label"])
async def test_provider_mode_breaks_ingested_at_ties_by_event_id(query):
    (sql, _), (original_sql, _) = await _provider_statement(query)
    assert "ORDER BY payment_events.ingested_at ASC, payment_events.event_id ASC" in sql
    assert "payment_events.event_id ASC" not in original_sql


async def test_provider_mode_drops_legacy_samples_and_keeps_genuine_ones():
    genuine = [_row(*GENUINE), _row("pay_live_ctx_notahexvalue", "order_x", "evt_x")]
    rows = [_row(*LEGACY), genuine[0], _row(LEGACY[0], LEGACY[1], f"evt_live_term_{TOKEN}"), genuine[1]]
    validator = await _in_snapshot(_provider_validator(_snapshot_session(results=[_rows_result(rows)])))
    assert await validator.get_authorized_events() == genuine


async def test_provider_extract_guards_the_full_feature_and_label_window_before_labelling():
    validator = _provider_validator(_snapshot_session())
    first, last = _row(*GENUINE, ingested_at=T), _row(*GENUINE, ingested_at=T + timedelta(days=2))
    validator.get_authorized_events = AsyncMock(return_value=[first, last])
    validator.get_terminal_label = AsyncMock(return_value=-1)
    validator.assert_evaluation_window_uncontaminated = AsyncMock(
        side_effect=EvaluationDataContaminationError("contaminated")
    )

    with pytest.raises(EvaluationDataContaminationError):
        await validator.extract_dataset()

    validator.assert_evaluation_window_uncontaminated.assert_awaited_once_with(
        T - CONTAMINATION_LOOKBACK, T + timedelta(days=2) + LABEL_HORIZON
    )
    validator.get_terminal_label.assert_not_awaited()


def test_contamination_window_covers_derived_feature_history_and_label_horizon():
    assert CONTAMINATION_LOOKBACK == timedelta(weeks=4, hours=24)
    assert LABEL_HORIZON == timedelta(minutes=30)


async def test_provider_extract_with_no_samples_returns_empty_without_guarding():
    validator = _provider_validator(_snapshot_session())
    validator.get_authorized_events = AsyncMock(return_value=[])
    validator.assert_evaluation_window_uncontaminated = AsyncMock()

    assert await validator.extract_dataset() == ([], [], [])
    validator.assert_evaluation_window_uncontaminated.assert_not_awaited()


# --- provider-evaluation mode: one consistent snapshot ------------------------------------------

def _recording_provider_validator(session, calls, auth_events):
    validator = _provider_validator(session)

    def record(name, result=None):
        async def step(*args, **kwargs):
            calls.append(name)
            return result
        return AsyncMock(side_effect=step)

    validator.get_authorized_events = record("authorized_events", auth_events)
    validator.assert_evaluation_window_uncontaminated = record("contamination_guard")
    validator.get_terminal_label = record("terminal_label", 0)
    validator.reconstructor.reconstruct_features = record("features", SimpleNamespace())
    return validator


async def test_provider_extract_sets_and_verifies_isolation_before_any_data_query(monkeypatch):
    monkeypatch.setattr("src.core.ml.training.asdict", lambda snapshot: {})
    calls = []
    session = _snapshot_session(calls=calls)
    validator = _recording_provider_validator(session, calls, [_row(*GENUINE)])

    X_raw, y_raw, times = await validator.extract_dataset()

    assert calls == [
        ("connection", {"execution_options": {"isolation_level": "REPEATABLE READ"}}),
        ("execute", "SHOW transaction_isolation"),
        "authorized_events",
        "contamination_guard",
        "terminal_label",
        "features",
    ]
    session.in_transaction.assert_called_once_with()
    assert (len(X_raw), y_raw, times) == (1, [0], [T])


@pytest.mark.parametrize("isolation", ["serializable", "REPEATABLE READ"])
async def test_provider_extract_accepts_snapshot_isolation_levels(isolation):
    validator = _recording_provider_validator(_snapshot_session(isolation=isolation), [], [])
    assert await validator.extract_dataset() == ([], [], [])


async def test_provider_extract_refuses_a_session_whose_transaction_already_began():
    calls = []
    session = _snapshot_session(transaction_began=True, calls=calls)
    validator = _recording_provider_validator(session, calls, [_row(*GENUINE)])

    with pytest.raises(EvaluationIsolationError, match="no transaction in progress"):
        await validator.extract_dataset()

    assert calls == []
    session.connection.assert_not_awaited()


@pytest.mark.parametrize("isolation", ["read committed", "read uncommitted", None, ""])
async def test_provider_extract_refuses_weaker_or_unreported_isolation(isolation):
    calls = []
    validator = _recording_provider_validator(_snapshot_session(isolation=isolation, calls=calls), calls,
                                              [_row(*GENUINE)])

    with pytest.raises(EvaluationIsolationError, match="REPEATABLE READ or SERIALIZABLE"):
        await validator.extract_dataset()

    assert "authorized_events" not in calls


async def test_provider_extract_propagates_isolation_setup_failures_before_any_data_query():
    calls = []
    session = _snapshot_session(calls=calls)
    session.connection.side_effect = RuntimeError("isolation_level not supported")
    validator = _recording_provider_validator(session, calls, [_row(*GENUINE)])

    with pytest.raises(RuntimeError, match="isolation_level not supported"):
        await validator.extract_dataset()

    assert calls == []


async def test_provider_extract_refuses_when_no_transaction_is_active_after_setup():
    session = _snapshot_session()
    session.transaction = None
    validator = _recording_provider_validator(session, [], [_row(*GENUINE)])

    with pytest.raises(EvaluationIsolationError, match="no active transaction"):
        await validator.extract_dataset()
    validator.get_authorized_events.assert_not_awaited()


@pytest.mark.parametrize("change", ["replaced", "ended"])
async def test_provider_extract_refuses_results_if_the_transaction_changes_during_extraction(change, monkeypatch):
    monkeypatch.setattr("src.core.ml.training.asdict", lambda snapshot: {})
    session = _snapshot_session()
    validator = _recording_provider_validator(session, [], [_row(*GENUINE)])

    async def label_then_commit(*args, **kwargs):
        if change == "replaced":
            session.transaction = SimpleNamespace(is_active=True)
        else:
            session.transaction.is_active = False
        return 0

    validator.get_terminal_label = AsyncMock(side_effect=label_then_commit)

    with pytest.raises(EvaluationIsolationError, match="did not share one snapshot"):
        await validator.extract_dataset()


# --- provider-evaluation mode: protected helpers and checked state ------------------------------

HELPER_CALLS = {
    "authorized_events": lambda v: v.get_authorized_events(),
    "terminal_label": lambda v: v.get_terminal_label("pay_1", T),
    "contamination_guard": lambda v: v.assert_evaluation_window_uncontaminated(START, END),
}


@pytest.mark.parametrize("helper", list(HELPER_CALLS))
async def test_provider_helpers_refuse_to_run_outside_the_snapshot(helper):
    session = _snapshot_session()
    with pytest.raises(EvaluationIsolationError, match="inside extract_dataset"):
        await HELPER_CALLS[helper](_provider_validator(session))
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("helper", list(HELPER_CALLS))
async def test_provider_helpers_refuse_once_the_snapshot_transaction_was_replaced(helper):
    session = _snapshot_session()
    validator = await _in_snapshot(_provider_validator(session))
    session.transaction = SimpleNamespace(is_active=True)
    with pytest.raises(EvaluationIsolationError, match="inside extract_dataset"):
        await HELPER_CALLS[helper](validator)
    assert _data_calls(session) == []


@pytest.mark.parametrize("helper", list(HELPER_CALLS))
async def test_provider_helpers_refuse_after_extraction_finishes(helper):
    session = _snapshot_session(results=[_rows_result([])])
    validator = _provider_validator(session)
    assert await validator.extract_dataset() == ([], [], [])
    with pytest.raises(EvaluationIsolationError, match="inside extract_dataset"):
        await HELPER_CALLS[helper](validator)


@pytest.mark.parametrize("helper", list(HELPER_CALLS))
async def test_default_mode_helpers_need_no_snapshot(helper):
    session = _guard_session(0, []) if helper == "contamination_guard" else _snapshot_session(
        results=[_rows_result([])]
    )
    await HELPER_CALLS[helper](Stage5PipelineValidator(session))
    session.in_transaction.assert_not_called()
    session.sync_session.get_transaction.assert_not_called()


@pytest.mark.parametrize("failure", ["contaminated", "transaction-replaced", "isolation-refused"])
async def test_failed_extraction_clears_previously_checked_state(failure, monkeypatch):
    monkeypatch.setattr("src.core.ml.training.asdict", lambda snapshot: {})
    session = _snapshot_session()
    validator = _recording_provider_validator(session, [], [_row(*GENUINE)])
    await validator.extract_dataset()
    assert validator.checked_contamination_window is not None

    if failure == "contaminated":
        validator.assert_evaluation_window_uncontaminated = AsyncMock(
            side_effect=EvaluationDataContaminationError("contaminated")
        )
        expected = EvaluationDataContaminationError
    elif failure == "transaction-replaced":
        async def label_then_commit(*args, **kwargs):
            session.transaction = SimpleNamespace(is_active=True)
            return 0
        validator.get_terminal_label = AsyncMock(side_effect=label_then_commit)
        expected = EvaluationIsolationError
    else:
        session.in_transaction.return_value = True
        expected = EvaluationIsolationError

    with pytest.raises(expected):
        await validator.extract_dataset()

    assert validator.checked_contamination_window is None
    with pytest.raises(EvaluationIsolationError, match="inside extract_dataset"):
        await Stage5PipelineValidator.get_terminal_label(validator, "pay_1", T)


# --- contamination guard ----------------------------------------------------------------------

START, END = T - timedelta(weeks=4), T + timedelta(minutes=30)


async def test_guard_passes_when_the_window_holds_only_provider_events():
    session = _guard_session(0, [GENUINE, ("pay_live_ctx_lookalike", "order_x", "evt_x")])
    validator = await _in_snapshot(_provider_validator(session))
    await validator.assert_evaluation_window_uncontaminated(START, END)


@pytest.mark.parametrize("non_provider_count, candidates, expected", [
    pytest.param(3, [], "3 non-provider and 0 legacy", id="non-provider-events"),
    pytest.param(0, [LEGACY, GENUINE], "0 non-provider and 1 legacy", id="legacy-live-context-events"),
])
async def test_guard_refuses_contaminated_windows(non_provider_count, candidates, expected):
    validator = await _in_snapshot(_provider_validator(_guard_session(non_provider_count, candidates)))
    with pytest.raises(EvaluationDataContaminationError, match=expected) as excinfo:
        await validator.assert_evaluation_window_uncontaminated(START, END)
    assert LEGACY[0] not in str(excinfo.value)


async def test_guard_queries_bind_the_window_provider_sources_and_an_escaped_legacy_prefix():
    session = _guard_session(0, [])
    validator = await _in_snapshot(_provider_validator(session))
    await validator.assert_evaluation_window_uncontaminated(START, END)
    count_call, candidate_call = _data_calls(session)
    count_sql, count_params = _compile(count_call.args[0])
    candidate_sql, candidate_params = _compile(candidate_call.args[0])

    excluded = [v for k, v in count_params.items() if k.startswith("source_system")]
    assert [list(v) for v in excluded] == [["razorpay", *NON_RAZORPAY_PIPELINE_SOURCES]]
    assert "razorpay" in candidate_params.values()
    assert "LIKE" in candidate_sql and "ESCAPE '/'" in candidate_sql
    assert "pay/_live/_ctx/_" in candidate_params.values()
    for sql, params in ((count_sql, count_params), (candidate_sql, candidate_params)):
        assert "payment_events.timestamp >= " in sql and "payment_events.ingested_at >= " in sql
        assert {START, END} <= set(v for v in params.values() if isinstance(v, datetime))
