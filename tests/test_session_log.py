"""Tests for the session log -- the record that makes a Day-60 verdict
possible, and the one that says whether a session happened at all."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from vs2.data.broker import AccountState, Holding
from vs2.data.session_log import (
    append_session,
    build_record,
    last_decided_day,
    load_sessions,
)

DAY = date(2026, 8, 10)


def account(equity: float = 100_000.0, cash: float = 30_000.0) -> AccountState:
    return AccountState(equity=equity, cash=cash)


def holding(symbol: str, market_value: float) -> Holding:
    return Holding(
        symbol=symbol, qty=10.0, market_value=market_value, avg_entry_price=1.0
    )


# --- exposure, the number DESIGN.md requires ---------------------------------


def test_invested_fraction_is_positions_over_equity() -> None:
    record = build_record(
        DAY,
        "DECIDED",
        account=account(equity=100_000.0),
        holdings=[holding("AAA", 40_000.0), holding("BBB", 20_000.0)],
    )

    assert record.long_market_value == pytest.approx(60_000.0)
    assert record.invested_fraction == pytest.approx(0.6)
    assert record.position_count == 2


def test_a_flat_account_is_zero_invested_not_unknown() -> None:
    record = build_record(DAY, "DECIDED", account=account(), holdings=[])

    assert record.invested_fraction == 0.0
    assert record.position_count == 0


def test_invested_fraction_is_none_when_equity_is_unknown() -> None:
    """None and 0.0 are different facts. Averaging 'we never looked' into a
    series of real exposures would understate it."""

    record = build_record(DAY, "NO_COMPLETED_SESSION")

    assert record.invested_fraction is None
    assert record.equity is None


def test_zero_equity_does_not_divide_by_zero() -> None:
    record = build_record(DAY, "DECIDED", account=account(equity=0.0), holdings=[])
    assert record.invested_fraction is None


# --- persistence --------------------------------------------------------------


def test_append_writes_one_row_per_call(tmp_path: Path) -> None:
    path = tmp_path / "sessions.jsonl"
    append_session(build_record(DAY, "DECIDED", account=account(), holdings=[]), path)
    append_session(build_record(DAY, "EXECUTED", account=account(), holdings=[]), path)

    rows = load_sessions(path)
    assert [r["event"] for r in rows] == ["DECIDED", "EXECUTED"]


def test_append_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "sessions.jsonl"
    append_session(build_record(DAY, "DECIDED", account=account(), holdings=[]), path)

    assert path.exists()


def test_rows_are_json_serialisable_with_dates_as_strings(tmp_path: Path) -> None:
    path = tmp_path / "sessions.jsonl"
    append_session(
        build_record(
            DAY, "DECIDED", account=account(), holdings=[], bars_as_of=DAY
        ),
        path,
    )

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["day"] == "2026-08-10"
    assert row["bars_as_of"] == "2026-08-10"
    assert row["logged_at"].endswith("Z")


def test_a_write_failure_is_logged_but_never_raises(tmp_path: Path) -> None:
    """This log is instrumentation. Failing to write an exposure row must not
    abort a cycle that is about to place real orders -- unlike the execution
    log, whose loss is worse than stopping."""

    unwritable = tmp_path / "sessions.jsonl"
    unwritable.mkdir()  # a directory where a file is expected

    append_session(build_record(DAY, "DECIDED", account=account(), holdings=[]), unwritable)


def test_load_sessions_is_empty_for_a_missing_file(tmp_path: Path) -> None:
    assert load_sessions(tmp_path / "absent.jsonl") == []


# --- noticing a gap -----------------------------------------------------------


def test_last_decided_day_ignores_non_decided_events(tmp_path: Path) -> None:
    """An EXECUTED row on a later day does not mean that day was decided."""

    path = tmp_path / "sessions.jsonl"
    append_session(build_record(DAY, "DECIDED", account=account(), holdings=[]), path)
    append_session(
        build_record(date(2026, 8, 11), "EXECUTED", account=account(), holdings=[]),
        path,
    )
    append_session(
        build_record(date(2026, 8, 12), "STALE_BARS", account=account(), holdings=[]),
        path,
    )

    assert last_decided_day(path) == DAY


def test_last_decided_day_takes_the_maximum_not_the_last_written(
    tmp_path: Path,
) -> None:
    """A --force re-run of an earlier day appends out of order."""

    path = tmp_path / "sessions.jsonl"
    append_session(
        build_record(date(2026, 8, 12), "DECIDED", account=account(), holdings=[]), path
    )
    append_session(build_record(DAY, "DECIDED", account=account(), holdings=[]), path)

    assert last_decided_day(path) == date(2026, 8, 12)


def test_last_decided_day_is_none_for_a_missing_file(tmp_path: Path) -> None:
    assert last_decided_day(tmp_path / "absent.jsonl") is None
