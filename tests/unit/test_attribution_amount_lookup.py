"""
M2.1: Stage 8 must attribute against the same payment.authorized amount Stage 6 used to size
the decision (latest authorization at or before the decision, never beyond as_of), and never
against refund, capture, order or other lifecycle rows.

The statement is captured from a mocked session and compiled with the PostgreSQL dialect;
predicates are checked structurally and through bound parameters. No database is used.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression

from src.core.attribution.attribution_service import CounterfactualAttributionService
from src.core.domain.source_systems import NON_RAZORPAY_PIPELINE_SOURCES
from src.infrastructure.attribution_repository import AttributionRepository
from src.infrastructure.models import PaymentEventModel

PG = postgresql.dialect()
AS_OF = datetime(2026, 9, 1, 10, 30, tzinfo=timezone.utc)
AUTHORIZED = "payment.authorized"


def _session(amount=None):
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = amount
    session.execute.return_value = result
    return session


async def _lookup(amount=None):
    session = _session(amount)
    returned = await AttributionRepository(session).get_canonical_payment_amount("pay_1", AS_OF)
    [call] = session.execute.call_args_list
    return returned, call.args[0]


def _predicates_on(statement, column):
    return [
        clause for clause in statement.whereclause.clauses
        if isinstance(clause, BinaryExpression)
        and getattr(clause.left, "table", None) is PaymentEventModel.__table__
        and clause.left.key == column.key
    ]


async def test_selects_only_the_amount_column():
    _, statement = await _lookup()
    assert [c.key for c in statement.selected_columns] == ["amount_minor_units"]


async def test_event_type_is_restricted_to_payment_authorized():
    _, statement = await _lookup()
    [predicate] = _predicates_on(statement, PaymentEventModel.event_type)
    assert predicate.operator is operators.eq
    assert predicate.right.value == AUTHORIZED

    sql = str(statement.compile(dialect=PG))
    params = statement.compile(dialect=PG).params
    name = re.search(r"payment_events\.event_type = %\((\w+)\)s", sql).group(1)
    assert params[name] == AUTHORIZED


@pytest.mark.parametrize("event_type", [
    "payment.captured",
    "payment.failed",
    "payment.pending",
    "payment.refunded",
    "order.approved",
    "order.completed",
    "refund.processed",
])
async def test_other_lifecycle_event_types_cannot_be_selected(event_type):
    _, statement = await _lookup()
    event_type_predicates = _predicates_on(statement, PaymentEventModel.event_type)
    assert len(event_type_predicates) == 1
    assert event_type_predicates[0].right.value != event_type


async def test_existing_payment_and_as_of_predicates_are_preserved():
    _, statement = await _lookup()
    [payment] = _predicates_on(statement, PaymentEventModel.payment_id)
    assert payment.operator is operators.eq and payment.right.value == "pay_1"
    [ingested] = _predicates_on(statement, PaymentEventModel.ingested_at)
    assert ingested.operator is operators.le and ingested.right.value == AS_OF


async def test_provider_exclusion_is_bound_to_the_shared_constant():
    _, statement = await _lookup()
    [exclusion] = _predicates_on(statement, PaymentEventModel.source_system)
    assert exclusion.operator is operators.not_in_op

    compiled = statement.compile(dialect=PG)
    name = re.search(
        r"payment_events\.source_system NOT IN \(__\[POSTCOMPILE_(\w+)\]\)", str(compiled)
    ).group(1)
    assert list(compiled.params[name]) == list(NON_RAZORPAY_PIPELINE_SOURCES) == ["paypal"]


async def test_latest_authorized_event_at_or_before_cutoff_is_chosen():
    _, statement = await _lookup()
    compiled = statement.compile(dialect=PG)
    sql = str(compiled)
    assert "ORDER BY payment_events.ingested_at DESC" in sql
    assert compiled.params[re.search(r"LIMIT %\((\w+)\)s", sql).group(1)] == 1


async def test_authorized_amount_is_returned_unchanged():
    returned, _ = await _lookup(amount=50000)
    assert returned == 50000


async def test_missing_authorized_amount_returns_none():
    returned, _ = await _lookup(amount=None)
    assert returned is None


def _service_repo(decided_at, amount=None):
    command = MagicMock(
        command_id=uuid.uuid4(),
        decision_id=uuid.uuid4(),
        payment_attempt_id="pay_1",
        source_system="razorpay",
        route_key="RETRY_ALTERNATE_GATEWAY",
        command_status="NOT_NEEDED",
        created_at=decided_at,
    )
    decision = MagicMock(
        decision_id=command.decision_id,
        source_system="razorpay",
        decision_type="NO_ACTION",
        stage5_prediction_id=None,
        diagnosis_confidence=None,
        decided_at=decided_at,
    )
    repo = AsyncMock()
    repo.get_attribution_by_as_of.return_value = None
    repo.get_latest_attribution.return_value = None
    repo.get_command.return_value = command
    repo.get_latest_execution_attempt.return_value = None
    repo.get_latest_outcome_observation.return_value = None
    repo.get_decision.return_value = decision
    repo.get_canonical_payment_amount.return_value = amount
    repo.append_attribution.side_effect = lambda attribution: attribution
    return command, repo


async def _attribute(repo, command, as_of):
    return await CounterfactualAttributionService(repo, attempt_repository=AsyncMock()).attribute_payment_intervention(
        command.command_id, as_of_timestamp=as_of
    )


async def test_service_uses_decision_time_when_it_precedes_as_of():
    decided_at = AS_OF - timedelta(minutes=10)
    command, repo = _service_repo(decided_at, amount=50000)

    attribution = await _attribute(repo, command, AS_OF)

    repo.get_canonical_payment_amount.assert_awaited_once_with("pay_1", decided_at)
    assert attribution.payment_amount_minor_units == 50000
    assert attribution.as_of_timestamp == AS_OF


async def test_service_uses_as_of_when_it_precedes_decision_time():
    command, repo = _service_repo(AS_OF + timedelta(minutes=10), amount=50000)

    await _attribute(repo, command, AS_OF)

    repo.get_canonical_payment_amount.assert_awaited_once_with("pay_1", AS_OF)


async def test_service_attributes_zero_when_no_authorized_amount_exists():
    command, repo = _service_repo(AS_OF, amount=None)

    attribution = await _attribute(repo, command, AS_OF)

    repo.get_canonical_payment_amount.assert_awaited_once_with("pay_1", AS_OF)
    assert attribution.payment_amount_minor_units == 0
    assert attribution.attributed_protected_gmv_minor_units == 0


async def test_authorization_ingested_after_the_decision_cannot_be_selected():
    decided_at = AS_OF - timedelta(minutes=10)
    authorized_before_decision = decided_at - timedelta(minutes=1)
    authorized_after_decision = decided_at + timedelta(minutes=1)
    assert authorized_after_decision <= AS_OF

    command, repo = _service_repo(decided_at)
    session = _session(amount=None)
    repo.get_canonical_payment_amount.side_effect = (
        AttributionRepository(session).get_canonical_payment_amount
    )

    await _attribute(repo, command, AS_OF)

    [call] = session.execute.call_args_list
    [cutoff] = _predicates_on(call.args[0], PaymentEventModel.ingested_at)
    assert cutoff.operator is operators.le
    assert cutoff.right.value == decided_at
    assert authorized_before_decision <= cutoff.right.value
    assert not authorized_after_decision <= cutoff.right.value
