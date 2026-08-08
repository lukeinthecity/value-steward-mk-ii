"""Tests for the execution log: the record of what was actually attempted,
independent of decision_log.py's record of what was decided."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from vs2.core.decision import Decision
from vs2.data.execution_log import append_execution_results, already_executed_today
from vs2.data.orders import SubmissionResult


def make_decision(symbol: str, action: str = "BUY") -> Decision:
    return Decision(
        symbol=symbol,
        day=date(2026, 8, 10),
        action=action,  # type: ignore[arg-type]
        reason_code=action,
        close=100.0,
        sma=90.0,
        prior_close=89.0,
        prior_sma=90.0,
        notional=1000.0 if action == "BUY" else None,
        qty=None if action == "BUY" else 5.0,
    )


class _FakeOrder:
    def __init__(self, order_id: str) -> None:
        self.id = order_id


def test_append_writes_one_row_per_result(tmp_path: Path) -> None:
    path = tmp_path / "executions.jsonl"
    results = [
        SubmissionResult(make_decision("AAA"), _FakeOrder("id-1"), None),
        SubmissionResult(make_decision("BBB"), None, "rejected"),
    ]
    append_execution_results(results, date(2026, 8, 10), path)

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


def test_success_row_records_order_id_and_no_error(tmp_path: Path) -> None:
    path = tmp_path / "executions.jsonl"
    append_execution_results(
        [SubmissionResult(make_decision("AAA"), _FakeOrder("abc-123"), None)],
        date(2026, 8, 10),
        path,
    )

    import json

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["succeeded"] is True
    assert row["order_id"] == "abc-123"
    assert row["error"] is None


def test_failure_row_records_error_and_no_order_id(tmp_path: Path) -> None:
    path = tmp_path / "executions.jsonl"
    append_execution_results(
        [SubmissionResult(make_decision("BBB"), None, "insufficient buying power")],
        date(2026, 8, 10),
        path,
    )

    import json

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["succeeded"] is False
    assert row["order_id"] is None
    assert row["error"] == "insufficient buying power"


def test_order_id_extraction_handles_a_dict_shaped_order(tmp_path: Path) -> None:
    # Test fakes elsewhere in this suite return a plain dict rather than an
    # object with an .id attribute -- both shapes must work.
    path = tmp_path / "executions.jsonl"
    append_execution_results(
        [SubmissionResult(make_decision("AAA"), {"id": "dict-id-1"}, None)],
        date(2026, 8, 10),
        path,
    )

    import json

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["order_id"] == "dict-id-1"


def test_append_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data" / "executions.jsonl"
    append_execution_results(
        [SubmissionResult(make_decision("AAA"), _FakeOrder("x"), None)],
        date(2026, 8, 10),
        path,
    )
    assert path.exists()


def test_already_executed_today_is_false_for_a_missing_file(tmp_path: Path) -> None:
    assert already_executed_today(tmp_path / "none.jsonl", date(2026, 8, 10)) is False


def test_already_executed_today_is_false_when_no_row_matches_the_day(tmp_path: Path) -> None:
    path = tmp_path / "executions.jsonl"
    append_execution_results(
        [SubmissionResult(make_decision("AAA"), _FakeOrder("x"), None)],
        date(2026, 8, 5),
        path,
    )
    assert already_executed_today(path, date(2026, 8, 10)) is False


def test_already_executed_today_is_true_after_a_successful_attempt(tmp_path: Path) -> None:
    path = tmp_path / "executions.jsonl"
    append_execution_results(
        [SubmissionResult(make_decision("AAA"), _FakeOrder("x"), None)],
        date(2026, 8, 10),
        path,
    )
    assert already_executed_today(path, date(2026, 8, 10)) is True


def test_already_executed_today_is_true_even_if_every_order_failed(tmp_path: Path) -> None:
    # This is the entire point: an attempted-but-failed day still counts as
    # "attempted", so the guard does not silently retry it automatically.
    path = tmp_path / "executions.jsonl"
    append_execution_results(
        [SubmissionResult(make_decision("AAA"), None, "rejected")],
        date(2026, 8, 10),
        path,
    )
    assert already_executed_today(path, date(2026, 8, 10)) is True


def test_already_executed_today_finds_a_match_anywhere_in_the_file(tmp_path: Path) -> None:
    # Unlike last_logged_day's deliberate last-row-only simplification, this
    # must find a match anywhere -- a force=True retry can add rows for a day
    # that is no longer the last one written.
    path = tmp_path / "executions.jsonl"
    append_execution_results(
        [SubmissionResult(make_decision("AAA"), _FakeOrder("x"), None)],
        date(2026, 8, 10),
        path,
    )
    append_execution_results(
        [SubmissionResult(make_decision("BBB"), _FakeOrder("y"), None)],
        date(2026, 8, 11),
        path,
    )
    assert already_executed_today(path, date(2026, 8, 10)) is True
