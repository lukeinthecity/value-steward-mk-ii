"""Tests for the standing health checks -- the reader that runs outside a
cycle, so it can notice a cycle that never ran.

The load-bearing case is `test_healthy_week_produces_no_findings`: a checker
that cries wolf on a good week gets muted, and a muted checker is worse than
none. Every other test here pairs a defect with the finding it must produce.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from vs2.health import Finding, check, read_rows, summarise

TODAY = date(2026, 8, 14)
WEEK = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]


def decided(day: str, *, invested: float | None = 0.0, missed: int = 0) -> dict[str, object]:
    return {
        "day": day,
        "event": "DECIDED",
        "equity": 100_000.0,
        "invested_fraction": invested,
        "missed_sessions": missed,
    }


def decisions_for(days: list[str], *, count: int = 30) -> list[dict[str, object]]:
    return [{"day": day, "symbol": f"S{i}"} for day in days for i in range(count)]


def no_orders(day: str) -> dict[str, object]:
    return {"day": day, "symbol": None, "action": "NO_ORDERS", "succeeded": True}


def healthy() -> tuple[list, list, list]:
    return (
        [decided(day) for day in WEEK],
        decisions_for(WEEK),
        [no_orders(day) for day in WEEK],
    )


# --- the quiet case, which must stay quiet -----------------------------------


def test_healthy_week_produces_no_findings() -> None:
    sessions, decisions, executions = healthy()
    assert check(sessions, decisions, executions, today=TODAY) == []


def test_zero_orders_all_week_is_not_a_finding() -> None:
    """The most common shape of a real week: no crossings, so no orders. It
    must not look like a broken run."""

    sessions, decisions, _ = healthy()
    executions = [no_orders(day) for day in WEEK]
    assert check(sessions, decisions, executions, today=TODAY) == []


# --- liveness: the failure nothing else can report ---------------------------


def test_silence_is_reported_as_stalled() -> None:
    sessions, decisions, executions = healthy()
    later = date(2026, 8, 21)  # a week after the newest row
    codes = [f.code for f in check(sessions, decisions, executions, today=later)]
    assert "RUN_STALLED" in codes


def test_a_long_weekend_is_not_stalled() -> None:
    """Friday's decision read on the following Tuesday is four days old and
    entirely normal. The threshold has to clear a holiday Monday."""

    sessions, decisions, executions = healthy()
    tuesday = date(2026, 8, 18)
    assert check(sessions, decisions, executions, today=tuesday) == []


def test_empty_logs_report_stalled_rather_than_raising() -> None:
    findings = check([], [], [], today=TODAY)
    assert [f.code for f in findings] == ["RUN_STALLED"]


def test_missed_sessions_on_the_newest_row_is_reported() -> None:
    sessions, decisions, executions = healthy()
    sessions[-1] = decided(WEEK[-1], missed=3)
    findings = check(sessions, decisions, executions, today=TODAY)
    assert any(f.code == "MISSED_SESSIONS" and "3 session" in f.detail for f in findings)


# --- convergence: the once-per-day guards ------------------------------------


def test_repeated_decided_rows_are_reported() -> None:
    sessions, decisions, executions = healthy()
    sessions.append(decided(WEEK[-1]))
    findings = check(sessions, decisions, executions, today=TODAY)
    assert any(f.code == "DUPLICATE_DECIDED" for f in findings)


def test_repeated_no_orders_rows_are_reported() -> None:
    """The #12 bug: six intraday ticks each writing an execution row for one
    decision day."""

    sessions, decisions, _ = healthy()
    executions = [no_orders(WEEK[-1]) for _ in range(6)]
    findings = check(sessions, decisions, executions, today=TODAY)
    assert any(f.code == "EXECUTION_REPEATED" and "6 NO_ORDERS" in f.detail for f in findings)


def test_one_no_orders_row_per_day_is_fine() -> None:
    sessions, decisions, executions = healthy()
    assert not [f for f in check(sessions, decisions, executions, today=TODAY)]


# --- completeness and measurability ------------------------------------------


def test_short_decision_day_is_reported() -> None:
    """27 of 30 symbols is a broken day that reads like a quiet one."""

    sessions, _, executions = healthy()
    decisions = decisions_for(WEEK[:-1]) + decisions_for([WEEK[-1]], count=27)
    findings = check(sessions, decisions, executions, today=TODAY)
    assert any(f.code == "DECISION_COUNT" and "27 decision rows" in f.detail for f in findings)


def test_a_decided_day_with_no_decision_rows_is_reported() -> None:
    sessions, _, executions = healthy()
    findings = check(sessions, decisions_for(WEEK[:-1]), executions, today=TODAY)
    assert any(f.code == "DECISION_COUNT" and WEEK[-1] in f.detail for f in findings)


def test_null_exposure_is_reported() -> None:
    sessions, decisions, executions = healthy()
    sessions[2] = decided(WEEK[2], invested=None)
    findings = check(sessions, decisions, executions, today=TODAY)
    assert any(f.code == "NO_EXPOSURE" and WEEK[2] in f.detail for f in findings)


def test_zero_exposure_is_not_null_exposure() -> None:
    """0.0 is a measurement; None is its absence. Conflating them is the class
    of bug the report module exists to avoid."""

    sessions, decisions, executions = healthy()
    assert not [f for f in check(sessions, decisions, executions, today=TODAY)]


# --- things a push may have missed -------------------------------------------


def test_stale_bars_event_is_reported() -> None:
    sessions, decisions, executions = healthy()
    sessions.append({"day": WEEK[-1], "event": "STALE_BARS"})
    findings = check(sessions, decisions, executions, today=TODAY)
    assert any(f.code == "STALE_BARS" for f in findings)


def test_failed_order_is_reported_with_its_error() -> None:
    sessions, decisions, _ = healthy()
    executions = [
        {
            "day": WEEK[-1],
            "symbol": "AAPL",
            "action": "BUY",
            "succeeded": False,
            "error": "insufficient buying power",
        }
    ]
    findings = check(sessions, decisions, executions, today=TODAY)
    assert any(
        f.code == "ORDER_FAILED" and "AAPL" in f.detail and "buying power" in f.detail
        for f in findings
    )


def test_successful_orders_are_not_reported() -> None:
    sessions, decisions, _ = healthy()
    executions = [
        {"day": day, "symbol": "AAPL", "action": "BUY", "succeeded": True} for day in WEEK
    ]
    assert check(sessions, decisions, executions, today=TODAY) == []


# --- reading and reporting ----------------------------------------------------


def test_read_rows_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "sessions.jsonl"
    path.write_text('{"day": "2026-08-10"}\n\n{"day": "2026-08-11"}\n', encoding="utf-8")
    assert [row["day"] for row in read_rows(path)] == ["2026-08-10", "2026-08-11"]


def test_read_rows_treats_a_missing_file_as_empty(tmp_path: Path) -> None:
    assert read_rows(tmp_path / "nope.jsonl") == []


def test_summary_truncates_and_says_so() -> None:
    findings = [Finding("X", str(i)) for i in range(12)]
    title, body = summarise(findings, limit=3)
    assert "12 issues" in title
    assert body.count("\n") == 3
    assert "and 9 more" in body


def test_summary_pluralises_one_issue() -> None:
    title, _ = summarise([Finding("X", "y")])
    assert "1 issue" in title and "issues" not in title


def test_findings_stringify_as_code_and_detail() -> None:
    assert str(Finding("RUN_STALLED", "nothing since Friday")) == (
        "RUN_STALLED: nothing since Friday"
    )


# --- against the real week's rows ---------------------------------------------


def test_against_the_first_dry_run_week(tmp_path: Path) -> None:
    """The shape the live logs actually had on 2026-08-14: five decided
    sessions, 30 decisions each, zero exposure, one NO_ORDERS row per day."""

    sessions_path = tmp_path / "sessions.jsonl"
    sessions_path.write_text(
        "\n".join(json.dumps(decided(day)) for day in WEEK) + "\n", encoding="utf-8"
    )
    decisions_path = tmp_path / "decisions.jsonl"
    decisions_path.write_text(
        "\n".join(json.dumps(row) for row in decisions_for(WEEK)) + "\n", encoding="utf-8"
    )
    executions_path = tmp_path / "executions.jsonl"
    executions_path.write_text(
        "\n".join(json.dumps(no_orders(day)) for day in WEEK) + "\n", encoding="utf-8"
    )

    findings = check(
        read_rows(sessions_path),
        read_rows(decisions_path),
        read_rows(executions_path),
        today=TODAY,
    )
    assert findings == []
