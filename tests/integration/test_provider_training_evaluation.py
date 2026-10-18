"""
Database-backed tests for Stage 5 provider-evaluation mode (PROVIDER_EVALUATION).

They run only against the validated TEST_DATABASE_URL through the shared fixtures in
tests/conftest.py, which truncate every application table before each test.

Provider extraction requires a session with no transaction in progress, so each test opens its own
sessions from the fixture engine. `async with` closes them, rolling back any open transaction, even
when an assertion fails. Callers own that cleanup: the validator never commits or rolls back.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.ml.training import (
    CONTAMINATION_LOOKBACK,
    LABEL_HORIZON,
    LEGACY_LIVE_CONTEXT_PAYMENT_PREFIX,
    EvaluationDataContaminationError,
    EvaluationIsolationError,
    Stage5PipelineValidator,
    TrainingPopulation,
    compute_dataset_fingerprint,
)
from src.infrastructure.models import PaymentEventModel

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
TOKEN = "0123456789ab"
ONE_MICROSECOND = timedelta(microseconds=1)


def _event(payment_id, event_type, at, *, source_system="razorpay", ingested_at=None, amount=50000,
           event_id=None, order_id=None, source_event_id=None):
    return PaymentEventModel(
        event_id=event_id or uuid.uuid4(),
        source_system=source_system,
        source_event_id=source_event_id or f"evt_provider_it_{uuid.uuid4().hex}",
        payment_id=payment_id,
        order_id=order_id,
        timestamp=at,
        event_type=event_type,
        currency="INR",
        amount_minor_units=amount,
        payment_status=event_type.split(".", 1)[1],
        payment_method="card",
        ingested_at=ingested_at or at,
    )


def _payment(payment_id, at, outcome="payment.captured", source_system="razorpay", **authorization):
    """An authorization at `at` and its terminal event five minutes later, inside the label horizon."""
    return [
        _event(payment_id, "payment.authorized", at, source_system=source_system, **authorization),
        _event(payment_id, outcome, at + timedelta(minutes=5), source_system=source_system),
    ]


def _sessions(db_engine):
    return async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(db_engine, events):
    async with _sessions(db_engine)() as session:
        session.add_all(events)
        await session.commit()


def _provider(session):
    return Stage5PipelineValidator(session, population=TrainingPopulation.PROVIDER_EVALUATION)


async def _isolation(session):
    return (await session.execute(text("SHOW transaction_isolation"))).scalar()


async def _default_isolation(session):
    return (await session.execute(text("SHOW default_transaction_isolation"))).scalar()


def _record_calls(validator):
    """Wraps the label and feature lookups so tests can see whether extraction reached them."""
    calls = []
    label, features = validator.get_terminal_label, validator.reconstructor.reconstruct_features

    async def recorded_label(payment_id, t):
        calls.append(("label", payment_id))
        return await label(payment_id, t)

    async def recorded_features(payment_id, t):
        calls.append(("features", payment_id))
        return await features(payment_id, t)

    validator.get_terminal_label = recorded_label
    validator.reconstructor.reconstruct_features = recorded_features
    return calls


# --- isolation -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_extraction_runs_at_repeatable_read_and_leaves_the_transaction_to_the_caller(db_engine):
    await _seed(db_engine, _payment("pay_iso_a", T0))

    async with _sessions(db_engine)() as session:
        X_raw, y_raw, times = await _provider(session).extract_dataset()

        assert (len(X_raw), y_raw, times) == (1, [0], [T0])
        assert session.in_transaction()
        assert await _isolation(session) == "repeatable read"

        await session.rollback()
        default_level = await _default_isolation(session)
        assert default_level != "repeatable read", "the reset check needs a server default other than REPEATABLE READ"
        assert await _isolation(session) == default_level


@pytest.mark.asyncio
async def test_provider_extraction_refuses_a_session_whose_transaction_already_began(db_engine):
    await _seed(db_engine, _payment("pay_iso_b", T0))

    async with _sessions(db_engine)() as session:
        await session.execute(text("SELECT 1"))
        default_level = await _default_isolation(session)
        validator = _provider(session)

        with pytest.raises(EvaluationIsolationError, match="no transaction in progress"):
            await validator.extract_dataset()

        assert await _isolation(session) == default_level
        assert validator.checked_contamination_window is None


# --- snapshot consistency ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rows_committed_after_the_snapshot_began_are_invisible_to_the_extraction(db_engine):
    await _seed(db_engine, [
        *_payment("pay_snap_a", T0),
        _event("pay_snap_b", "payment.authorized", T0 + timedelta(minutes=1)),
    ])
    late_terminal = _event("pay_snap_b", "payment.failed", T0 + timedelta(minutes=6))
    Session = _sessions(db_engine)

    async with Session() as session:
        validator = _provider(session)
        label = validator.get_terminal_label
        committed = []

        async def label_after_concurrent_commit(payment_id, t):
            if not committed:
                await _seed(db_engine, [late_terminal])
                committed.append(payment_id)
            return await label(payment_id, t)

        validator.get_terminal_label = label_after_concurrent_commit
        snapshot_data = await validator.extract_dataset()

        assert committed == ["pay_snap_a"]
        assert snapshot_data[1:] == ([0], [T0])
        assert validator._checked_extraction.dataset_fingerprint == compute_dataset_fingerprint(*snapshot_data)

    async with Session() as session:
        fresh_data = await _provider(session).extract_dataset()

    assert fresh_data[1:] == ([0, 1], [T0, T0 + timedelta(minutes=1)])
    assert compute_dataset_fingerprint(*fresh_data) != compute_dataset_fingerprint(*snapshot_data)


@pytest.mark.asyncio
async def test_features_are_reconstructed_from_the_extraction_snapshot(db_engine):
    # The Razorpay global failure rate needs at least 50 terminal events in the 30 minutes before T.
    prior_failures = [
        _event(f"pay_rate_fail_{i}", "payment.failed", T0 - timedelta(minutes=10),
               ingested_at=T0 - timedelta(minutes=9))
        for i in range(50)
    ]
    late_captures = [
        _event(f"pay_rate_capture_{i}", "payment.captured", T0 - timedelta(minutes=8),
               ingested_at=T0 - timedelta(minutes=7))
        for i in range(50)
    ]
    await _seed(db_engine, [*_payment("pay_rate_a", T0), *prior_failures])
    Session = _sessions(db_engine)

    async with Session() as session:
        validator = _provider(session)
        label = validator.get_terminal_label

        async def label_after_concurrent_commit(payment_id, t):
            await _seed(db_engine, late_captures)
            return await label(payment_id, t)

        validator.get_terminal_label = label_after_concurrent_commit
        (snapshot_features,), _, _ = await validator.extract_dataset()

    async with Session() as session:
        (fresh_features,), _, _ = await _provider(session).extract_dataset()

    assert snapshot_features["global_30m_failure_rate"] == 1.0
    assert fresh_features["global_30m_failure_rate"] == 0.5


# --- failure and transaction lifecycle -----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_commit_during_extraction_fails_closed_and_leaves_no_checked_state(db_engine):
    await _seed(db_engine, _payment("pay_life_a", T0))

    async with _sessions(db_engine)() as session:
        validator = _provider(session)
        label = validator.get_terminal_label

        async def commit_then_label(payment_id, t):
            await session.commit()
            return await label(payment_id, t)

        validator.get_terminal_label = commit_then_label

        with pytest.raises(EvaluationIsolationError):
            await validator.extract_dataset()

        assert validator.checked_contamination_window is None
        with pytest.raises(RuntimeError, match="contamination check first"):
            validator.run_pipeline([{}], [0], [T0])


@pytest.mark.asyncio
async def test_callers_must_end_the_transaction_before_extracting_again(db_engine):
    await _seed(db_engine, _payment("pay_life_b", T0))

    async with _sessions(db_engine)() as session:
        validator = _provider(session)
        first = await validator.extract_dataset()

        with pytest.raises(EvaluationIsolationError, match="no transaction in progress"):
            await validator.extract_dataset()
        assert validator.checked_contamination_window is None

        await session.rollback()
        assert not session.in_transaction()
        assert await validator.extract_dataset() == first
        assert validator.checked_contamination_window == (T0 - CONTAMINATION_LOOKBACK, T0 + LABEL_HORIZON)


@pytest.mark.asyncio
async def test_a_failed_extraction_still_leaves_the_transaction_open_for_the_caller(db_engine):
    await _seed(db_engine, [
        *_payment("pay_life_c", T0),
        _event("pay_life_synthetic", "payment.failed", T0 - timedelta(days=1), source_system="synthetic"),
    ])

    async with _sessions(db_engine)() as session:
        with pytest.raises(EvaluationDataContaminationError):
            await _provider(session).extract_dataset()

        assert session.in_transaction()
        await session.rollback()
        assert not session.in_transaction()


# --- contamination guard -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_contaminating_source_inside_the_window_is_refused_before_labels_or_features(db_engine):
    await _seed(db_engine, [
        *_payment("pay_guard_a", T0),
        _event("pay_guard_synthetic", "payment.failed", T0 - timedelta(days=7), source_system="synthetic"),
    ])

    async with _sessions(db_engine)() as session:
        validator = _provider(session)
        calls = _record_calls(validator)

        with pytest.raises(EvaluationDataContaminationError, match="1 non-provider and 0 legacy"):
            await validator.extract_dataset()

        assert calls == []
        assert validator.checked_contamination_window is None


WINDOW_START, WINDOW_END = T0 - CONTAMINATION_LOOKBACK, T0 + LABEL_HORIZON


@pytest.mark.parametrize("at, refused", [
    pytest.param(WINDOW_START, True, id="at-window-start"),
    pytest.param(WINDOW_START - ONE_MICROSECOND, False, id="before-window-start"),
    pytest.param(WINDOW_END, True, id="at-window-end"),
    pytest.param(WINDOW_END + ONE_MICROSECOND, False, id="after-window-end"),
])
@pytest.mark.asyncio
async def test_the_contamination_window_bounds_are_inclusive(db_engine, at, refused):
    await _seed(db_engine, [
        *_payment("pay_bound_a", T0),
        _event("pay_bound_synthetic", "payment.failed", at, source_system="synthetic"),
    ])

    async with _sessions(db_engine)() as session:
        validator = _provider(session)
        if refused:
            with pytest.raises(EvaluationDataContaminationError, match="1 non-provider"):
                await validator.extract_dataset()
        else:
            _, y_raw, times = await validator.extract_dataset()
            assert (y_raw, times) == ([0], [T0])


def _legacy(kind, token=TOKEN, at=T0 - timedelta(days=1)):
    event_type = "payment.authorized" if kind == "auth" else "payment.captured"
    return _event(f"pay_live_ctx_{token}", event_type, at, order_id=f"order_live_ctx_{token}",
                  source_event_id=f"evt_live_{kind}_{token}")


@pytest.mark.asyncio
async def test_legacy_live_context_rows_inside_the_window_are_refused(db_engine):
    await _seed(db_engine, [*_payment("pay_legacy_a", T0), _legacy("auth"), _legacy("term")])

    async with _sessions(db_engine)() as session:
        validator = _provider(session)
        calls = _record_calls(validator)

        with pytest.raises(EvaluationDataContaminationError, match="0 non-provider and 2 legacy"):
            await validator.extract_dataset()
        assert calls == []


# (payment_id, order_id, source_event_id); each uses its own token so source_event_ids stay unique.
LOOKALIKES = [
    ("payXliveXctxX111111111111", "order_live_ctx_111111111111", "evt_live_term_111111111111"),
    ("pay_live_ctx_222222222222", "order_live_ctx_999999999999", "evt_live_term_222222222222"),
    ("pay_live_ctx_333333333333", "order_live_ctx_333333333333", "evt_live_term_999999999999"),
    ("pay_live_ctx_ABCDEFABCDEF", "order_live_ctx_ABCDEFABCDEF", "evt_live_term_ABCDEFABCDEF"),
    ("pay_live_ctx_555555555555", None, "evt_live_term_555555555555"),
]


def _lookalike_rows():
    return [
        _event(payment_id, "payment.captured", T0 - timedelta(days=1), order_id=order_id,
               source_event_id=source_event_id)
        for payment_id, order_id, source_event_id in LOOKALIKES
    ]


@pytest.mark.asyncio
async def test_legacy_lookalikes_inside_the_window_are_not_treated_as_contamination(db_engine):
    await _seed(db_engine, [*_payment("pay_lookalike_a", T0), *_lookalike_rows()])

    async with _sessions(db_engine)() as session:
        _, y_raw, times = await _provider(session).extract_dataset()

    assert (y_raw, times) == ([0], [T0])


@pytest.mark.asyncio
async def test_the_escaped_legacy_prefix_matches_only_the_literal_prefix_in_postgresql(db_engine, db_session):
    await _seed(db_engine, [_legacy("auth"), *_lookalike_rows()])

    rows = await db_session.execute(
        select(PaymentEventModel.payment_id).where(
            PaymentEventModel.payment_id.startswith(LEGACY_LIVE_CONTEXT_PAYMENT_PREFIX, autoescape=True)
        )
    )

    matched = {payment_id for (payment_id,) in rows.all()}
    assert "payXliveXctxX111111111111" not in matched
    assert matched == {f"pay_live_ctx_{TOKEN}"} | {
        payment_id for payment_id, _, _ in LOOKALIKES if payment_id.startswith("pay_live_ctx_")
    }


@pytest.mark.asyncio
async def test_legacy_samples_outside_the_window_are_dropped_without_refusal(db_engine):
    await _seed(db_engine, [
        *_payment("pay_dropped_a", T0),
        _legacy("auth", at=T0 - timedelta(days=60)),
    ])

    async with _sessions(db_engine)() as session:
        validator = _provider(session)
        calls = _record_calls(validator)
        _, y_raw, times = await validator.extract_dataset()

    assert (y_raw, times) == ([0], [T0])
    assert calls == [("label", "pay_dropped_a"), ("features", "pay_dropped_a")]


# --- deterministic ordering ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tied_authorizations_are_ordered_by_event_id(db_engine):
    await _seed(db_engine, [
        *_payment("pay_tie_high", T0, amount=1111, event_id=uuid.UUID(int=2)),
        *_payment("pay_tie_low", T0, amount=2222, event_id=uuid.UUID(int=1)),
    ])

    async with _sessions(db_engine)() as session:
        X_raw, _, times = await _provider(session).extract_dataset()

    assert times == [T0, T0]
    assert [row["amount_minor_units"] for row in X_raw] == [2222, 1111]


@pytest.mark.parametrize("lower_event, expected_label", [
    pytest.param("payment.captured", 0, id="capture-has-lower-event-id"),
    pytest.param("payment.failed", 1, id="decline-has-lower-event-id"),
])
@pytest.mark.asyncio
async def test_tied_terminal_events_resolve_the_label_by_event_id(db_engine, lower_event, expected_label):
    higher_event = "payment.failed" if lower_event == "payment.captured" else "payment.captured"
    tied_at = T0 + timedelta(minutes=5)
    await _seed(db_engine, [
        _event("pay_tie_label", "payment.authorized", T0),
        _event("pay_tie_label", higher_event, tied_at, event_id=uuid.UUID(int=0x20)),
        _event("pay_tie_label", lower_event, tied_at, event_id=uuid.UUID(int=0x10)),
    ])

    async with _sessions(db_engine)() as session:
        _, y_raw, _ = await _provider(session).extract_dataset()

    assert y_raw == [expected_label]


@pytest.mark.asyncio
async def test_repeated_extractions_over_unchanged_data_have_the_same_fingerprint(db_engine):
    await _seed(db_engine, [
        *_payment("pay_repeat_a", T0, event_id=uuid.UUID(int=3)),
        *_payment("pay_repeat_b", T0, outcome="payment.failed", event_id=uuid.UUID(int=4)),
        *_payment("pay_repeat_c", T0 + timedelta(minutes=2)),
    ])
    Session = _sessions(db_engine)

    async with Session() as session:
        first_validator = _provider(session)
        first = await first_validator.extract_dataset()
    async with Session() as session:
        second_validator = _provider(session)
        second = await second_validator.extract_dataset()

    assert first == second
    assert compute_dataset_fingerprint(*first) == compute_dataset_fingerprint(*second)
    assert first_validator.checked_contamination_window == second_validator.checked_contamination_window


# --- default synthetic-development compatibility ---------------------------------------------------

@pytest.mark.asyncio
async def test_default_extraction_keeps_synthetic_samples_and_the_default_isolation(db_session):
    db_session.add_all([
        *_payment("pay_default_razorpay", T0),
        *_payment("pay_default_synthetic", T0 + timedelta(minutes=1), source_system="synthetic"),
    ])
    await db_session.commit()

    X_raw, y_raw, times = await Stage5PipelineValidator(db_session).extract_dataset()

    assert times == [T0, T0 + timedelta(minutes=1)]
    assert len(X_raw) == len(y_raw) == 2
    assert await _isolation(db_session) == await _default_isolation(db_session)
