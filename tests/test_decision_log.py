"""Tests for the decision log: append-only JSONL, and the once-per-day guard
it doubles as the source of truth for."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from vs2.core.decision import Decision
from vs2.data.decision_log import append_decisions, last_logged_day


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
