"""Tests for the decision log: append-only JSONL, and the once-per-day guard
it doubles as the source of truth for."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from vs2.core.decision import Decision
from vs2.data.decision_log import append_decisions, last_logged_day, load_decisions

DAY = date(2026, 8, 10)


def make(symbol: str, day: date | None, action: str = "HOLD") -> Decision:
    return Decision(
        symbol=symbol,
        day=day,
        action=action,  # type: ignore[arg-type]
        reason_code=action,
        close=100.0,
        sma=90.0,
        prior_close=89.0,
        prior_sma=90.0,
        notional=None,
        qty=None,
    )


def test_append_writes_one_json_line_per_decision(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    append_decisions([make("AAA", date(2026, 8, 10)), make("BBB", date(2026, 8, 10))], path)

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


def test_append_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data" / "decisions.jsonl"
    append_decisions([make("AAA", date(2026, 8, 10))], path)
    assert path.exists()


def test_append_does_not_overwrite_prior_runs(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    append_decisions([make("AAA", date(2026, 8, 10))], path)
    append_decisions([make("BBB", date(2026, 8, 11))], path)

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


def test_decision_with_null_day_serializes_without_crashing(tmp_path: Path) -> None:
    # The NO_SIGNAL_DATA case: a held symbol absent from the day's signals.
    path = tmp_path / "decisions.jsonl"
    append_decisions([make("GONE", day=None, action="HOLD")], path)

    import json

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["day"] is None
    assert row["symbol"] == "GONE"


def test_last_logged_day_is_none_for_a_missing_file(tmp_path: Path) -> None:
    assert last_logged_day(tmp_path / "does-not-exist.jsonl") is None


def test_last_logged_day_is_none_for_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.touch()
    assert last_logged_day(path) is None


def test_last_logged_day_returns_the_most_recent_day(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    append_decisions([make("A", date(2026, 8, 5))], path)
    append_decisions([make("B", date(2026, 8, 10))], path)
    append_decisions([make("C", date(2026, 8, 7))], path)  # out of order on purpose

    # last_logged_day reads the last row written, not the max across all rows --
    # a daily runner only ever appends in increasing day order, so this is a
    # deliberate simplification, pinned here so a future change notices.
    assert last_logged_day(path) == date(2026, 8, 7)


def test_last_logged_day_skips_null_days_at_the_end(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    append_decisions([make("A", date(2026, 8, 10))], path)
    append_decisions([make("GONE", day=None)], path)

    assert last_logged_day(path) == date(2026, 8, 10)


# --- replay: execution reads back what was decided ---------------------------
#
# A decision is made after one session's close and executed during the next.
# Recomputing at execution time would read a portfolio that morning's own sells
# had already changed, so the logged row is the contract.


def _decision(symbol: str, action: str = "BUY", day: date | None = DAY) -> Decision:
    return Decision(
        symbol=symbol,
        day=day,
        action=action,  # type: ignore[arg-type]
        reason_code="CROSS_UP",
        close=100.0,
        sma=90.0,
        prior_close=89.0,
        prior_sma=90.0,
        notional=5000.0 if action == "BUY" else None,
        qty=None if action == "BUY" else 10.0,
        dollar_volume=1e9,
        tiebreak_rank=1,
        available_cash=50_000.0,
        slots_free=20,
    )


def test_load_decisions_round_trips_every_field(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    original = _decision("AAPL")
    append_decisions([original], path)

    loaded = load_decisions(path, DAY)

    assert loaded == [original]


def test_load_decisions_returns_only_the_requested_day(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    append_decisions([_decision("AAPL")], path)
    append_decisions([_decision("MSFT", day=date(2026, 8, 11))], path)

    assert [d.symbol for d in load_decisions(path, DAY)] == ["AAPL"]


def test_load_decisions_is_sorted_by_symbol(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    append_decisions([_decision("MSFT"), _decision("AAPL")], path)

    assert [d.symbol for d in load_decisions(path, DAY)] == ["AAPL", "MSFT"]


def test_a_forced_rerun_replays_the_later_decision_not_the_superseded_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "decisions.jsonl"
    append_decisions([_decision("AAPL", action="BUY")], path)
    append_decisions([_decision("AAPL", action="HOLD")], path)

    loaded = load_decisions(path, DAY)

    assert len(loaded) == 1
    assert loaded[0].action == "HOLD"


def test_load_decisions_is_empty_for_a_missing_file(tmp_path: Path) -> None:
    assert load_decisions(tmp_path / "absent.jsonl", DAY) == []


def test_a_row_written_before_the_schema_grew_still_loads(tmp_path: Path) -> None:
    """A run in progress must not break because new columns were added."""

    path = tmp_path / "decisions.jsonl"
    path.write_text(
        json.dumps(
            {
                "symbol": "AAPL",
                "day": "2026-08-10",
                "action": "BUY",
                "reason_code": "CROSS_UP",
                "close": 100.0,
                "sma": 90.0,
                "prior_close": 89.0,
                "prior_sma": 90.0,
                "notional": 5000.0,
                "qty": None,
                "logged_at": "2026-08-10T20:15:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = load_decisions(path, DAY)

    assert loaded[0].action == "BUY"
    assert loaded[0].tiebreak_rank is None
