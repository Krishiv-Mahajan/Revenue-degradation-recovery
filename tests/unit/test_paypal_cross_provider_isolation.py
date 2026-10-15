"""
P1c cross-provider regression tests: can realistic PayPal canonical events satisfy the existing
Razorpay-shaped downstream reads?

Each query runs against a mocked session; the captured statement's WHERE predicates are then
evaluated against canonical rows. The evaluator follows SQL three-valued logic (a predicate that
is UNKNOWN because of a NULL operand does not match) and supports only the predicate shapes
these queries use: anything else, including unbound parameters, fails the test. No database is
used, so this is predicate-level evidence, not PostgreSQL validation.

Fixtures:
  * Realistic events come from the real parsers and normalizers: PayPal samples through
    parse_paypal_event/normalize_paypal_event, Razorpay webhooks through
    parse_razorpay_event/normalize_razorpay_event.
  * Synthetic minimal-difference controls copy a PayPal-derived row and change only
    source_system. They are NOT realistic Razorpay events; they isolate the source_system
    predicate from every other column.

Two groups of tests:
  * Guarantees: Stage 5 eligibility, labels and the live prediction trigger, Stage 6 sizing and
    the Stage 8 amount lookup do not use PayPal rows, even when payment_id overlaps.
  * Known gaps (characterization, not desired behavior): the Stage 6/7 terminal-event lookups
    and the dashboard summary have no provider filter. These tests pin the current behavior;
    changing it needs a separately approved production fix, after which they must be updated.
"""
import json
import operator
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import bindparam, or_, select, text
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList, TextClause

from src.api.inspection_routes import get_executive_summary
from src.core.normalizers.paypal import normalize_paypal_event
from src.core.normalizers.razorpay import normalize_razorpay_event
from src.core.parsers.paypal import parse_paypal_event
from src.core.parsers.razorpay import parse_razorpay_event
from src.core.services import live_pipeline
from src.core.services.failure_prediction_service import FailurePredictionService
from src.infrastructure.attribution_repository import AttributionRepository
from src.infrastructure.execution_repository import ExecutionRepository
from src.infrastructure.feature_reconstruction_repository import FeatureReconstructionRepository
from src.infrastructure.intervention_repository import InterventionRepository
from src.infrastructure.models import PaymentEventModel
from tests.unit import paypal_event_samples as samples

T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
BEFORE_T = T - timedelta(minutes=1)
AFTER_T = T + timedelta(minutes=5)
RAZORPAY_ATTEMPT_ID = "pay_P1cOverlap00001"
RAZORPAY_CREATED_AT = int((T - timedelta(minutes=2)).timestamp())

# (PayPal sample, matching Razorpay webhook event, Razorpay payment status)
TERMINAL_PAIRS = [
    pytest.param(samples.capture_completed, "payment.captured", "captured", id="captured"),
    pytest.param(samples.capture_declined, "payment.failed", "failed", id="failed"),
]
TERMINAL_BUILDERS = [samples.capture_completed, samples.capture_declined]
NON_TERMINAL_BUILDERS = [
    samples.capture_pending, samples.capture_refunded, samples.order_approved, samples.order_completed,
]


# --- realistic provider fixtures --------------------------------------------------------------

def _paypal_event(builder, ingested_at=BEFORE_T):
    return normalize_paypal_event(parse_paypal_event(builder())).model_copy(update={"ingested_at": ingested_at})


def _paypal_row(builder, payment_id=RAZORPAY_ATTEMPT_ID, ingested_at=BEFORE_T):
    """A real normalized PayPal event; payment_id defaults to an overlapping Razorpay attempt id."""
    row = _paypal_event(builder, ingested_at).model_dump()
    assert row["source_system"] == "paypal"
    if payment_id is not None:
        row["payment_id"] = payment_id
    return row


def _razorpay_event(event, status, payment_id=RAZORPAY_ATTEMPT_ID, ingested_at=BEFORE_T):
    """A Razorpay webhook body parsed and normalized by the real Razorpay parser/normalizer."""
    entity = {
        "id": payment_id, "entity": "payment", "amount": 50000, "currency": "inr", "status": status,
        "order_id": "order_P1cControl0001", "method": "upi", "vpa": "buyer@okhdfc",
        "created_at": RAZORPAY_CREATED_AT,
    }
    if status == "failed":
        entity.update(
            error_code="BAD_REQUEST_ERROR", error_description="Payment was declined by the bank.",
            error_source="bank", error_step="payment_authorization", error_reason="payment_failed",
        )
    body = {
        "entity": "event", "account_id": "acc_P1cControl01", "event": event, "contains": ["payment"],
        "payload": {"payment": {"entity": entity}}, "created_at": RAZORPAY_CREATED_AT,
    }
    normalized = normalize_razorpay_event(parse_razorpay_event(json.dumps(body).encode()), f"evt_P1c_{status}")
    return normalized.model_copy(update={"ingested_at": ingested_at})


def _razorpay_row(event, status, ingested_at=BEFORE_T):
    row = _razorpay_event(event, status, ingested_at=ingested_at).model_dump()
    assert row["source_system"] == "razorpay" and row["currency"] == "INR"
    return row


def _synthetic_source_swap(row, **overrides):
    """Minimal-difference control: the same row with only source_system (and overrides) changed.
    Not a realistic Razorpay event."""
    return {**row, "source_system": "razorpay", **overrides}


# --- SQL predicate evaluation (three-valued logic) --------------------------------------------

def _and(results):
    if any(result is False for result in results):
        return False
    if any(result is None for result in results):
        return None
    return True


def _compare(compare, value, bound):
    if value is None or bound is None:
        return None
    return compare(value, bound)


def _in(value, items):
    if not isinstance(items, (list, tuple)):
        raise AssertionError(f"IN requires a bound list, got {items!r}")
    if value is None:
        return None
    present = [item for item in items if item is not None]
    if value in present:
        return True
    return None if len(present) != len(items) else False


def _not_in(value, items):
    result = _in(value, items)
    return None if result is None else not result


_COMPARISONS = {
    "=": operator.eq, "<>": operator.ne, "!=": operator.ne,
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}
_ORM_COMPARISONS = {
    operators.eq: operator.eq, operators.ne: operator.ne,
    operators.lt: operator.lt, operators.le: operator.le,
    operators.gt: operator.gt, operators.ge: operator.ge,
}


def _orm_bound_value(parameter):
    if not isinstance(parameter, BindParameter):
        raise AssertionError(f"Unsupported operand: {parameter}")
    if parameter.required or parameter.callable is not None:
        raise AssertionError(f"Unbound parameter: {parameter.key}")
    return parameter.value


def _orm_predicates(clause):
    """Flattens an AND-only clause into validated (column, operator, bound value) triples."""
    if isinstance(clause, BooleanClauseList) and clause.operator is operators.and_:
        return [p for c in clause.clauses for p in _orm_predicates(c)]
    if isinstance(clause, BinaryExpression) and (
        clause.operator in _ORM_COMPARISONS or clause.operator in (operators.in_op, operators.not_in_op)
    ):
        if getattr(clause.left, "table", None) is not PaymentEventModel.__table__:
            raise AssertionError(f"Unsupported operand: {clause.left}")
        return [(clause.left.key, clause.operator, _orm_bound_value(clause.right))]
    raise AssertionError(f"Unsupported predicate: {clause}")


def _orm_evaluate(clause, row):
    if clause is None:
        return True
    results = []
    for column, op, bound in _orm_predicates(clause):
        value = row[column]
        if op is operators.in_op:
            results.append(_in(value, bound))
        elif op is operators.not_in_op:
            results.append(_not_in(value, bound))
        else:
            results.append(_compare(_ORM_COMPARISONS[op], value, bound))
    return _and(results)


_TEXT_COMPARISON = re.compile(r"^(\w+) (<=|>=|<>|!=|=|<|>) :(\w+)$")
_TEXT_NOT_IN_PARAM = re.compile(r"^(\w+) NOT IN :(\w+)$")
_TEXT_IN_LITERALS = re.compile(r"^(\w+) IN \((.*)\)$")
_STRING_LITERAL_LIST = re.compile(r"'[^']*'(?:, '[^']*')*")


def _text_param(params, name):
    if name not in params:
        raise AssertionError(f"Unbound parameter: {name}")
    return params[name]


def _text_predicates(statement, params):
    """Parses an AND-only text WHERE clause into validated (column, kind, operand) triples."""
    match = re.search(r"\bWHERE\s+(.*?)\s*\)\s*$", statement.text, re.S)
    if match is None:
        raise AssertionError("Unsupported text statement: no trailing WHERE clause")
    parsed = []
    for raw in re.split(r"\s+AND\s+", match.group(1)):
        predicate = " ".join(raw.split())
        if m := _TEXT_COMPARISON.match(predicate):
            parsed.append((m.group(1), m.group(2), _text_param(params, m.group(3))))
        elif m := _TEXT_NOT_IN_PARAM.match(predicate):
            parsed.append((m.group(1), "NOT IN", _text_param(params, m.group(2))))
        elif (m := _TEXT_IN_LITERALS.match(predicate)) and _STRING_LITERAL_LIST.fullmatch(m.group(2)):
            parsed.append((m.group(1), "IN", re.findall(r"'([^']*)'", m.group(2))))
        else:
            raise AssertionError(f"Unsupported predicate: {predicate}")
    return parsed


def _text_evaluate(statement, params, row):
    results = []
    for column, kind, operand in _text_predicates(statement, params):
        value = row[column]
        if kind == "IN":
            results.append(_in(value, operand))
        elif kind == "NOT IN":
            results.append(_not_in(value, operand))
        else:
            results.append(_compare(_COMPARISONS[kind], value, operand))
    return _and(results)


def _matches(executed, row):
    """True only when the WHERE clause is TRUE for the row (UNKNOWN and FALSE do not match)."""
    statement, params = executed
    if isinstance(statement, TextClause):
        return _text_evaluate(statement, params, row) is True
    return _orm_evaluate(statement.whereclause, row) is True


def _predicate_columns(executed):
    statement, params = executed
    if isinstance(statement, TextClause):
        return {column for column, _, _ in _text_predicates(statement, params)}
    return {column for column, _, _ in _orm_predicates(statement.whereclause)}


# --- evaluator self-tests ---------------------------------------------------------------------

M = PaymentEventModel
_ROW = {"payment_id": "pay_1", "source_system": "razorpay", "event_type": "payment.captured", "ingested_at": T}


def _orm(*conditions):
    return (select(M).where(*conditions), {})


def _text(where, params):
    return (text(f"SELECT EXISTS (SELECT 1 FROM payment_events WHERE {where})"), params)


def test_evaluator_not_in_with_a_null_column_does_not_match():
    row = {**_ROW, "source_system": None}
    assert not _matches(_orm(M.source_system.notin_(("paypal",))), row)
    assert not _matches(_text("source_system NOT IN :excluded", {"excluded": ["paypal"]}), row)
    assert _matches(_orm(M.source_system.notin_(("paypal",))), _ROW)


def test_evaluator_equality_with_null_operands_does_not_match():
    null_row = {**_ROW, "payment_id": None}
    assert not _matches(_orm(M.payment_id == "pay_1"), null_row)
    assert not _matches(_orm(M.payment_id == bindparam("pid", value=None)), _ROW)
    assert not _matches(_orm(M.payment_id == bindparam("pid", value=None)), null_row)
    assert not _matches(_text("payment_id = :pid", {"pid": None}), null_row)
    assert not _matches(_orm(M.payment_id != "other"), null_row)
    assert _matches(_orm(M.payment_id != "other"), _ROW)


def test_evaluator_ordering_with_null_operands_does_not_match():
    null_row = {**_ROW, "ingested_at": None}
    assert not _matches(_orm(M.ingested_at <= T), null_row)
    assert not _matches(_text("ingested_at < :t", {"t": None}), _ROW)
    assert _matches(_orm(M.ingested_at <= T), _ROW)


def _bound_list(values):
    return bindparam("ids", value=values, expanding=True)


def test_evaluator_in_lists_containing_null_follow_three_valued_logic():
    assert _matches(_orm(M.payment_id.in_(_bound_list(["pay_1", None]))), _ROW)
    assert not _matches(_orm(M.payment_id.in_(_bound_list(["other", None]))), _ROW)
    assert not _matches(_orm(M.payment_id.notin_(_bound_list(["other", None]))), _ROW)
    assert not _matches(_orm(M.payment_id.notin_(_bound_list(["pay_1", None]))), _ROW)
    assert not _matches(_text("payment_id NOT IN :ids", {"ids": ["other", None]}), _ROW)
    assert not _matches(_orm(M.payment_id.in_(["pay_1"])), {**_ROW, "payment_id": None})


def test_evaluator_and_is_false_before_unknown_and_unknown_never_matches():
    executed = _orm(M.payment_id == "pay_1", M.ingested_at <= T)
    assert _orm_evaluate(executed[0].whereclause, {**_ROW, "ingested_at": None}) is None
    assert _orm_evaluate(executed[0].whereclause, {**_ROW, "payment_id": "x", "ingested_at": None}) is False
    assert not _matches(executed, {**_ROW, "ingested_at": None})


@pytest.mark.parametrize("executed", [
    pytest.param(_orm(M.payment_id == bindparam("pid")), id="orm-unbound"),
    pytest.param(_orm(M.payment_id == bindparam("pid", callable_=lambda: "pay_1")), id="orm-callable"),
    pytest.param(_text("payment_id = :pid", {}), id="text-missing-param"),
    pytest.param(_text("source_system NOT IN :excluded", {"excluded": None}), id="text-not-in-without-list"),
])
def test_evaluator_rejects_parameters_without_a_bound_value(executed):
    with pytest.raises(AssertionError, match="Unbound parameter|IN requires a bound list"):
        _matches(executed, _ROW)


@pytest.mark.parametrize("executed", [
    pytest.param(_orm(or_(M.payment_id == "pay_1", M.event_type == "x")), id="orm-or"),
    pytest.param(_orm(M.order_id.is_(None)), id="orm-is-null"),
    pytest.param(_orm(M.payment_id.like("pay_%")), id="orm-like"),
    pytest.param(_orm(M.payment_id == M.order_id), id="orm-column-to-column"),
    pytest.param(_orm(M.payment_id.in_(["pay_1", None])), id="orm-in-with-literal-null"),
    pytest.param(_text("payment_id = :p OR event_type = :e", {"p": "pay_1", "e": "x"}), id="text-or"),
    pytest.param(_text("ingested_at BETWEEN :a AND :b", {"a": T, "b": T}), id="text-between"),
    pytest.param(_text("event_type IN (:a, :b)", {"a": "x", "b": "y"}), id="text-in-non-literals"),
    pytest.param(_text("(payment_id = :p)", {"p": "pay_1"}), id="text-grouping"),
    pytest.param((text("SELECT 1 FROM payment_events"), {}), id="text-no-where"),
])
def test_evaluator_rejects_unsupported_predicate_shapes(executed):
    with pytest.raises(AssertionError, match="Unsupported"):
        _matches(executed, _ROW)


# --- queries under test -----------------------------------------------------------------------

async def _stage5_eligibility(session):
    await FailurePredictionService(session, MagicMock(), MagicMock(), MagicMock()).is_eligible_for_prediction(
        RAZORPAY_ATTEMPT_ID, T
    )


async def _stage5_label(session):
    await FailurePredictionService(session, MagicMock(), MagicMock(), MagicMock()).lookup_terminal_label(
        RAZORPAY_ATTEMPT_ID, T, observation_window_minutes=30
    )


async def _stage6_sizing(session):
    await FeatureReconstructionRepository(session).get_payment_event_by_id(RAZORPAY_ATTEMPT_ID, T)


async def _stage8_amount(session):
    await AttributionRepository(session).get_canonical_payment_amount(RAZORPAY_ATTEMPT_ID, T)


async def _stage6_terminal_check(session):
    await InterventionRepository(session).check_terminal_event_exists(RAZORPAY_ATTEMPT_ID, T)


async def _stage7_terminal_check(session):
    await ExecutionRepository(session).check_terminal_event_exists_before(RAZORPAY_ATTEMPT_ID, T)


async def _stage7_outcome_events(session):
    await ExecutionRepository(session).get_canonical_terminal_events(RAZORPAY_ATTEMPT_ID, T)


async def _executed(query):
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    await query(session)
    [call] = session.execute.call_args_list
    return call.args[0], (call.args[1] if len(call.args) > 1 else {})


SIZING_QUERIES = {"stage6.get_payment_event_by_id": _stage6_sizing,
                  "stage8.get_canonical_payment_amount": _stage8_amount}
TERMINAL_QUERIES = {
    "stage6.check_terminal_event_exists": _stage6_terminal_check,
    "stage7.check_terminal_event_exists_before": _stage7_terminal_check,
    "stage7.get_canonical_terminal_events": _stage7_outcome_events,
}


# --- guarantees: Stage 5 ----------------------------------------------------------------------

@pytest.mark.parametrize("builder, razorpay_event, razorpay_status", TERMINAL_PAIRS)
async def test_stage5_eligibility_ignores_paypal_terminal_events_with_overlapping_payment_id(
    builder, razorpay_event, razorpay_status,
):
    executed = await _executed(_stage5_eligibility)
    paypal = _paypal_row(builder)
    assert not _matches(executed, paypal)
    assert _matches(executed, _razorpay_row(razorpay_event, razorpay_status))
    assert _matches(executed, _synthetic_source_swap(paypal))


@pytest.mark.parametrize("builder, razorpay_event, razorpay_status", TERMINAL_PAIRS)
async def test_stage5_label_ignores_paypal_terminal_events_with_overlapping_payment_id(
    builder, razorpay_event, razorpay_status,
):
    executed = await _executed(_stage5_label)
    paypal = _paypal_row(builder, ingested_at=AFTER_T)
    assert not _matches(executed, paypal)
    assert _matches(executed, _razorpay_row(razorpay_event, razorpay_status, ingested_at=AFTER_T))
    assert _matches(executed, _synthetic_source_swap(paypal))


def _stub_live_pipeline_services(monkeypatch):
    """Replaces the services run_live_pipeline constructs; its own event routing stays real."""
    prediction_service = AsyncMock()
    prediction_service.orchestrate_prediction.return_value = MagicMock(failure_probability=0.1)
    for name in ("PaymentHealthAnalyticsService", "DegradationService", "RCAService", "FeatureReconstructionService",
                 "InterventionDecisionService", "InterventionExecutionService", "PaymentOutcomeObservationService",
                 "CounterfactualAttributionService"):
        monkeypatch.setattr(live_pipeline, name, MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr(live_pipeline, "FailurePredictionService", MagicMock(return_value=prediction_service))
    monkeypatch.setattr(live_pipeline, "get_development_model", MagicMock())
    monkeypatch.setattr(live_pipeline, "get_default_policy", MagicMock())
    monkeypatch.setattr(live_pipeline, "SimulatorInterventionExecutor", MagicMock())

    session = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = []
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    return session, prediction_service


@pytest.mark.parametrize("event_type", list(samples.ALL_SUPPORTED))
async def test_live_prediction_trigger_is_not_entered_by_real_paypal_events(event_type, monkeypatch):
    session, prediction_service = _stub_live_pipeline_services(monkeypatch)
    event = _paypal_event(samples.ALL_SUPPORTED[event_type])

    outcome = await live_pipeline.run_live_pipeline(event, session=session)

    assert outcome["status"] == "success"
    assert outcome["prediction"] is None
    prediction_service.orchestrate_prediction.assert_not_awaited()


async def test_live_prediction_trigger_is_entered_by_a_real_razorpay_authorization(monkeypatch):
    session, prediction_service = _stub_live_pipeline_services(monkeypatch)
    event = _razorpay_event("payment.authorized", "authorized")

    outcome = await live_pipeline.run_live_pipeline(event, session=session)

    assert outcome["status"] == "success"
    prediction_service.orchestrate_prediction.assert_awaited_once()
    assert prediction_service.orchestrate_prediction.await_args.args == (RAZORPAY_ATTEMPT_ID,)


async def test_live_prediction_trigger_checks_event_type_not_provider(monkeypatch):
    # Characterization: the trigger is `event_type == "payment.authorized"` with no provider check.
    # PayPal stays out only because its normalizer never emits payment.authorized and the PayPal
    # webhook route never calls run_live_pipeline. This synthetic row is not a real PayPal event.
    session, prediction_service = _stub_live_pipeline_services(monkeypatch)
    synthetic = _paypal_event(samples.order_approved).model_copy(update={"event_type": "payment.authorized"})
    assert synthetic.source_system == "paypal"

    outcome = await live_pipeline.run_live_pipeline(synthetic, session=session)

    assert outcome["status"] == "success"
    prediction_service.orchestrate_prediction.assert_awaited_once()


# --- guarantees: Stage 6 sizing and Stage 8 amount lookup ---------------------------------------

@pytest.mark.parametrize("query", list(SIZING_QUERIES))
@pytest.mark.parametrize("event_type", list(samples.ALL_SUPPORTED))
async def test_sizing_lookups_never_select_real_paypal_events(query, event_type):
    executed = await _executed(SIZING_QUERIES[query])
    assert not _matches(executed, _paypal_row(samples.ALL_SUPPORTED[event_type]))
    assert _matches(executed, _razorpay_row("payment.authorized", "authorized"))


@pytest.mark.parametrize("query", list(SIZING_QUERIES))
async def test_sizing_lookups_exclude_paypal_even_for_an_authorized_event_type(query):
    # PayPal never emits payment.authorized. This synthetic PayPal row and its synthetic
    # source-swapped twin differ only in source_system, so the provider filter alone decides.
    executed = await _executed(SIZING_QUERIES[query])
    synthetic_paypal = {**_paypal_row(samples.order_approved), "event_type": "payment.authorized"}
    assert not _matches(executed, synthetic_paypal)
    assert _matches(executed, _synthetic_source_swap(synthetic_paypal))


# --- known gap: Stage 6/7 terminal-event lookups ------------------------------------------------

@pytest.mark.parametrize("query", list(TERMINAL_QUERIES))
async def test_terminal_lookups_have_no_provider_predicate_known_gap(query):
    executed = await _executed(TERMINAL_QUERIES[query])
    assert _predicate_columns(executed) == {"payment_id", "event_type", "ingested_at"}


@pytest.mark.parametrize("query", list(TERMINAL_QUERIES))
@pytest.mark.parametrize("builder, razorpay_event, razorpay_status", TERMINAL_PAIRS)
async def test_terminal_lookups_currently_match_paypal_terminal_events_with_overlapping_payment_id(
    query, builder, razorpay_event, razorpay_status,
):
    # KNOWN GAP (characterization): a real PayPal capture or decline whose payment_id equals the
    # Razorpay attempt's matches exactly like the realistic Razorpay terminal event does.
    # Code-review inference, not exercised here: Stage 6 would then treat the attempt as already
    # terminal, Stage 7 would observe the PayPal outcome, and Stage 8 would attribute from it.
    executed = await _executed(TERMINAL_QUERIES[query])
    assert _matches(executed, _razorpay_row(razorpay_event, razorpay_status))
    assert _matches(executed, _paypal_row(builder))


@pytest.mark.parametrize("query", list(TERMINAL_QUERIES))
@pytest.mark.parametrize("builder", TERMINAL_BUILDERS)
async def test_terminal_lookups_rely_on_identifier_disjointness_for_paypal_captures(query, builder):
    # Current protection is only that PayPal capture ids and Razorpay payment ids differ;
    # no schema constraint enforces this.
    executed = await _executed(TERMINAL_QUERIES[query])
    paypal = _paypal_row(builder, payment_id=None)
    assert paypal["payment_id"] == samples.CAPTURE_ID != RAZORPAY_ATTEMPT_ID
    assert not _matches(executed, paypal)


@pytest.mark.parametrize("query", list(TERMINAL_QUERIES))
@pytest.mark.parametrize("builder", NON_TERMINAL_BUILDERS)
async def test_terminal_lookups_ignore_non_terminal_paypal_events_with_overlapping_payment_id(query, builder):
    executed = await _executed(TERMINAL_QUERIES[query])
    assert not _matches(executed, _paypal_row(builder))


# --- known gap: dashboard summary --------------------------------------------------------------

class _EmulatedSummarySession:
    """
    Python emulation of the database for get_executive_summary; NOT PostgreSQL.
    payment_events statements are answered from in-memory rows: each statement's own WHERE
    clause is applied by the evaluator, then COUNT ... GROUP BY event_type or SUM(amount) is
    emulated in Python. Statements on any other table return 0 (empty tables).
    """

    def __init__(self, rows):
        self.rows = rows
        self.payment_event_statements = []

    async def execute(self, statement):
        result = MagicMock()
        if PaymentEventModel.__table__ not in statement.get_final_froms():
            result.scalar_one_or_none.return_value = 0
            return result
        self.payment_event_statements.append(statement)
        matched = [row for row in self.rows if _orm_evaluate(statement.whereclause, row) is True]
        if "event_type" in [column.key for column in statement.selected_columns]:
            result.all.return_value = list(Counter(row["event_type"] for row in matched).items())
        else:
            result.scalar_one_or_none.return_value = sum(row["amount_minor_units"] for row in matched)
        return result


async def test_dashboard_summary_counts_paypal_captures_in_the_success_rate_known_gap():
    # KNOWN GAP (characterization): the summary's event-count statement has no WHERE clause, so a
    # PayPal capture is counted with Razorpay captures. The counts and rate come from the real
    # summary code over the emulated aggregate; this is not PostgreSQL validation.
    razorpay_auth = _razorpay_row("payment.authorized", "authorized")
    razorpay_capture = _razorpay_row("payment.captured", "captured")
    paypal_capture = _paypal_row(samples.capture_completed, payment_id=None)

    razorpay_session = _EmulatedSummarySession([razorpay_auth, razorpay_capture])
    mixed_session = _EmulatedSummarySession([razorpay_auth, razorpay_capture, paypal_capture])
    razorpay_only = await get_executive_summary(razorpay_session)
    mixed = await get_executive_summary(mixed_session)

    # The event-count statement (selecting event_type) is unfiltered: observed directly, not emulated.
    [count_statement] = [s for s in mixed_session.payment_event_statements
                         if "event_type" in [c.key for c in s.selected_columns]]
    assert count_statement.whereclause is None

    # Manually calculated from the fixtures: Razorpay contributes 1 authorization and 1 capture,
    # PayPal contributes 1 capture and no authorization, so captures/authorizations is 1/1 then 2/1.
    assert (razorpay_only["total_auths"], razorpay_only["total_captures"], razorpay_only["overall_success_rate"]) == (1, 1, 1.0)
    assert (mixed["total_auths"], mixed["total_captures"], mixed["overall_success_rate"]) == (1, 2, 2.0)
    # total_gmv sums payment.authorized amounts only; PayPal never emits payment.authorized.
    assert mixed["total_gmv_minor_units"] == razorpay_only["total_gmv_minor_units"] == razorpay_auth["amount_minor_units"]
