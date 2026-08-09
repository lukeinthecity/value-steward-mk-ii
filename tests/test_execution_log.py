"""Tests for the execution log: the record of what was actually attempted,
independent of decision_log.py's record of what was decided."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from vs2.core.decision import Decision
from vs2.data.execution_log import (
    already_executed_today,
    append_execution_results,
    pending_submissions,
)
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


DAY = date(2026, 8, 10)
buy = make_decision


def ok(decision: Decision) -> SubmissionResult:
    return SubmissionResult(decision, _FakeOrder(f"id-{decision.symbol}"), None)


def failed(decision: Decision) -> SubmissionResult:
    return SubmissionResult(decision, None, "rejected")


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


# --- write retries: safe here in a way orders.submit's retry was not --------


class FlakyPath:
    """Duck-types the two Path operations append_execution_results uses.
    Fails .open() a configurable number of times, then delegates to a real
    path underneath -- so the underlying write behavior is genuinely real,
    only the failure injection is fake."""

    def __init__(self, real_path: Path, fail_times: int) -> None:
        self._real = real_path
        self._fail_times = fail_times
        self.open_calls = 0

    @property
    def parent(self) -> Path:
        return self._real.parent

    def open(self, mode: str, encoding: str | None = None):  # noqa: ANN201
        self.open_calls += 1
        if self.open_calls <= self._fail_times:
            raise OSError("simulated: no space left on device")
        return self._real.open(mode, encoding=encoding)


def test_write_retries_and_succeeds_after_a_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vs2.data.execution_log.time.sleep", lambda _s: None)
    real_path = tmp_path / "executions.jsonl"
    flaky = FlakyPath(real_path, fail_times=1)

    append_execution_results(
        [SubmissionResult(make_decision("AAA"), _FakeOrder("x"), None)],
        date(2026, 8, 10),
        flaky,  # type: ignore[arg-type]
    )

    assert flaky.open_calls == 2  # one failure, then a successful retry
    assert real_path.exists()
    assert already_executed_today(real_path, date(2026, 8, 10)) is True


def test_write_gives_up_after_exhausting_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vs2.data.execution_log.time.sleep", lambda _s: None)
    real_path = tmp_path / "executions.jsonl"
    flaky = FlakyPath(real_path, fail_times=10)  # never succeeds within 3 default retries

    with pytest.raises(OSError, match="simulated"):
        append_execution_results(
            [SubmissionResult(make_decision("AAA"), _FakeOrder("x"), None)],
            date(2026, 8, 10),
            flaky,  # type: ignore[arg-type]
        )

    assert flaky.open_calls == 3
    assert not real_path.exists()  # every attempt failed; nothing was written


def test_exhausted_retries_log_the_raw_results_at_critical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The entire point: if the durable record can't be written, the raw
    # outcome must still be visible somewhere a human will see it.
    monkeypatch.setattr("vs2.data.execution_log.time.sleep", lambda _s: None)
    flaky = FlakyPath(tmp_path / "executions.jsonl", fail_times=10)

    with caplog.at_level("CRITICAL", logger="vs2.data.execution_log"):
        with pytest.raises(OSError):
            append_execution_results(
                [SubmissionResult(make_decision("ORPHANED"), _FakeOrder("real-id-123"), None)],
                date(2026, 8, 10),
                flaky,  # type: ignore[arg-type]
            )

    critical_messages = [r.message for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(critical_messages) == 1
    assert "ORPHANED" in critical_messages[0]
    assert "real-id-123" in critical_messages[0]


def test_zero_retries_raises_a_distinct_error_rather_than_crashing_confusingly(
    tmp_path: Path,
) -> None:
    # retries<=0 means the loop body never runs and no exception was ever
    # caught -- a misuse of the function, not a write failure, and must not
    # be reported as one.
    flaky = FlakyPath(tmp_path / "executions.jsonl", fail_times=10)
    with pytest.raises(RuntimeError, match="no attempt was made"):
        append_execution_results(
            [SubmissionResult(make_decision("AAA"), _FakeOrder("x"), None)],
            date(2026, 8, 10),
            flaky,  # type: ignore[arg-type]
            retries=0,
        )


def test_retries_are_not_used_up_by_a_successful_first_attempt(tmp_path: Path) -> None:
    real_path = tmp_path / "executions.jsonl"
    flaky = FlakyPath(real_path, fail_times=0)  # succeeds immediately

    append_execution_results(
        [SubmissionResult(make_decision("AAA"), _FakeOrder("x"), None)],
        date(2026, 8, 10),
        flaky,  # type: ignore[arg-type]
    )

    assert flaky.open_calls == 1


# --- what reconciliation reads back -------------------------------------------


def test_rows_carry_the_reconciliation_key_and_the_close_to_measure_against(
    tmp_path: Path,
) -> None:
    """Neither can be recovered later: the decision row may be superseded by a
    --force re-run, and matching a fill by symbol and timestamp is guesswork."""

    path = tmp_path / "executions.jsonl"
    append_execution_results([ok(buy("AAPL"))], DAY, path)

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["client_order_id"] == "vs2-2026-08-10-AAPL-BUY"
    assert row["decision_close"] == 100.0


def test_pending_submissions_skips_failures(tmp_path: Path) -> None:
    """An order that was never accepted has no fill to find."""

    path = tmp_path / "executions.jsonl"
    append_execution_results([ok(buy("AAA")), failed(buy("BBB"))], DAY, path)

    pending = pending_submissions(path, set())

    assert [row["symbol"] for row in pending] == ["AAA"]


def test_pending_submissions_skips_what_is_already_reconciled(tmp_path: Path) -> None:
    path = tmp_path / "executions.jsonl"
    append_execution_results([ok(buy("AAA")), ok(buy("BBB"))], DAY, path)

    pending = pending_submissions(path, {"vs2-2026-08-10-AAA-BUY"})

    assert [row["symbol"] for row in pending] == ["BBB"]


def test_pending_submissions_is_empty_for_a_missing_file(tmp_path: Path) -> None:
    assert pending_submissions(tmp_path / "absent.jsonl", set()) == []


def test_a_day_with_no_orders_still_records_that_it_was_attempted(
    tmp_path: Path,
) -> None:
    """Most sessions produce no order at all. Without a row the guard keeps
    answering False and every intraday tick re-enters the execute branch --
    placing nothing, but breaking the once-per-day property on the commonest
    kind of day."""

    path = tmp_path / "executions.jsonl"
    append_execution_results([], DAY, path)

    assert already_executed_today(path, DAY) is True
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["action"] == "NO_ORDERS"
    assert row["symbol"] is None


def test_the_no_orders_marker_is_not_reconciled(tmp_path: Path) -> None:
    """It has no client_order_id, so there is no fill to go looking for."""

    path = tmp_path / "executions.jsonl"
    append_execution_results([], DAY, path)

    assert pending_submissions(path, set()) == []
