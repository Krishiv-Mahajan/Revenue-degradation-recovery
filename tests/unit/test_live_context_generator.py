"""
tests/unit/test_live_context_generator.py

Unit tests for scripts/generate_live_context.py.
Verifies window calculations, event structure, baseline rules, AST safety,
and non-destructive guarantees without touching the active demo database.
"""

import ast
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.generate_live_context import (
    compute_tumbling_windows,
    seed_historical_baselines,
    seed_upstream_synthetic_traffic,
)


def test_compute_tumbling_windows_boundaries():
    """Verifies that tumbling windows are computed strictly in 5-minute boundaries."""
    # Test arbitrary time: 14:23:45 UTC
    test_dt = datetime(2026, 9, 4, 14, 23, 45, tzinfo=timezone.utc)
    w0_start, w0_end, past_windows = compute_tumbling_windows(test_dt)

    assert w0_start == datetime(2026, 9, 4, 14, 20, 0, tzinfo=timezone.utc)
    assert w0_end == datetime(2026, 9, 4, 14, 25, 0, tzinfo=timezone.utc)

    # Exactly three completed windows
    assert len(past_windows) == 3

    # W-1
    assert past_windows[2] == (
        datetime(2026, 9, 4, 14, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 14, 20, 0, tzinfo=timezone.utc),
    )
    # W-2
    assert past_windows[1] == (
        datetime(2026, 9, 4, 14, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 14, 15, 0, tzinfo=timezone.utc),
    )
    # W-3
    assert past_windows[0] == (
        datetime(2026, 9, 4, 14, 5, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 14, 10, 0, tzinfo=timezone.utc),
    )

    # Each window is strictly 5 minutes (300 seconds)
    for start, end in past_windows:
        assert (end - start).total_seconds() == 300


def test_compute_tumbling_windows_at_boundary():
    """Verifies window boundary calculation when time is exactly on a 5-minute mark."""
    test_dt = datetime(2026, 9, 4, 14, 0, 0, tzinfo=timezone.utc)
    w0_start, w0_end, past_windows = compute_tumbling_windows(test_dt)

    assert w0_start == datetime(2026, 9, 4, 14, 0, 0, tzinfo=timezone.utc)
    assert w0_end == datetime(2026, 9, 4, 14, 5, 0, tzinfo=timezone.utc)
    assert past_windows[2] == (
        datetime(2026, 9, 4, 13, 55, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 14, 0, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_seed_historical_baselines_dimensions():
    """Verifies baseline snapshots are created for GLOBAL and PAYMENT_METHOD only (NO CURRENCY)."""
    mock_session = AsyncMock()

    windows = [
        (datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc), datetime(2026, 9, 4, 10, 5, tzinfo=timezone.utc)),
        (datetime(2026, 9, 4, 10, 5, tzinfo=timezone.utc), datetime(2026, 9, 4, 10, 10, tzinfo=timezone.utc)),
        (datetime(2026, 9, 4, 10, 10, tzinfo=timezone.utc), datetime(2026, 9, 4, 10, 15, tzinfo=timezone.utc)),
    ]

    now = datetime(2026, 9, 4, 10, 20, tzinfo=timezone.utc)
    count = await seed_historical_baselines(mock_session, windows, pm="card", now=now)

    # 3 windows * 2 dimensions = 6 snapshots
    assert count == 6
    assert mock_session.execute.call_count == 6
    mock_session.commit.assert_awaited_once()

    # Inspect the insert calls
    inserted_dims = []
    for call in mock_session.execute.call_args_list:
        stmt = call[0][0]
        # Check parameters compiled into the statement
        params = stmt.compile().params
        # Find the segment_dimension parameter
        for k, v in params.items():
            if "segment_dimension" in k:
                inserted_dims.append(v)

    # Confirm GLOBAL and payment_method are present, and currency is NOT
    assert "GLOBAL" in inserted_dims
    assert "payment_method" in inserted_dims
    assert "currency" not in inserted_dims
    assert "CURRENCY" not in inserted_dims


@pytest.mark.asyncio
async def test_seed_upstream_synthetic_traffic_contract():
    """Verifies synthetic traffic structure: 60 tx/window, razorpay source, card pm, INR currency."""
    mock_session = AsyncMock()
    mock_session.add_all = MagicMock()

    windows = [
        (datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc), datetime(2026, 9, 4, 10, 5, tzinfo=timezone.utc)),
        (datetime(2026, 9, 4, 10, 5, tzinfo=timezone.utc), datetime(2026, 9, 4, 10, 10, tzinfo=timezone.utc)),
        (datetime(2026, 9, 4, 10, 10, tzinfo=timezone.utc), datetime(2026, 9, 4, 10, 15, tzinfo=timezone.utc)),
    ]

    total_tx = await seed_upstream_synthetic_traffic(mock_session, windows, pm="card", curr="INR", seed=42)

    # 3 windows * 60 tx = 180 transactions
    assert total_tx == 180

    # Inspect added objects
    raw_records = mock_session.add_all.call_args_list[0][0][0]
    payment_events = mock_session.add_all.call_args_list[1][0][0]

    # Each tx has 2 raw records and 2 payment events (auth + terminal)
    assert len(raw_records) == 360
    assert len(payment_events) == 360

    # Verify all payment events have correct attributes
    term_events = [e for e in payment_events if e.event_type in ("payment.captured", "payment.failed")]
    assert len(term_events) == 180

    # Minimum transaction threshold check (>= 50 per window)
    for w_start, w_end in windows:
        window_terms = [e for e in term_events if w_start <= e.timestamp < w_end]
        assert len(window_terms) == 60
        assert len(window_terms) >= 50

        # Check failure rate (~80%)
        fails = [e for e in window_terms if e.event_type == "payment.failed"]
        assert len(fails) == 48
        assert len(fails) / len(window_terms) == 0.80

    # Source system must be 'razorpay'
    assert all(e.source_system == "razorpay" for e in payment_events)
    assert all(r.source_system == "razorpay" for r in raw_records)

    # Payment method must be 'card'
    assert all(e.payment_method == "card" for e in payment_events)

    # Currency must be 'INR'
    assert all(e.currency == "INR" for e in payment_events)


def test_no_destructive_database_operations_in_helper():
    """Static AST inspection ensuring generate_live_context.py contains no destructive calls."""
    script_path = os.path.join(os.path.dirname(__file__), "../../scripts/generate_live_context.py")
    with open(script_path, "r") as f:
        tree = ast.parse(f.read(), filename="generate_live_context.py")

    # Inspect code calls and SQL statements for destructive operations
    module_docstring = ast.get_docstring(tree) or ""
    forbidden_tokens = ["drop table", "truncate table", "delete from"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value.strip().lower()
            if val == module_docstring.strip().lower():
                continue
            for token in forbidden_tokens:
                assert token not in val, f"Forbidden destructive SQL token found in code string: {val}"
        elif isinstance(node, ast.Attribute):
            assert node.attr not in ("drop_all", "create_all"), f"Forbidden schema modification call: {node.attr}"

