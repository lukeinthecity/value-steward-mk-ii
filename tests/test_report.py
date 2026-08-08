"""Tests for the run review.

The end-to-end case at the bottom does not hand-write its input. It drives the
real `run_daily.run()` across several simulated sessions and reports on the
logs that actually came out, because a fixture written to look plausible is
exactly what let this project's predecessor ship four population bugs that
every one of its own tests passed -- see the code-check playbook, section 4.
Hand-built rows appear only in the unit cases below, where the point is to pin
one function's behaviour on an input chosen to be awkward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from vs2.analysis.report import (
    benchmark_return,
    build_report,
    format_report,
    load_report,
    report_as_dict,
    strategy_return,
    summarise_capture,
    summarise_exposure,
)


@dataclass
class Bar:
    timestamp: datetime
    open: float
    close: float


def bars(days: list[tuple[date, float, float]]) -> list[Bar]:
    return [
        Bar(timestamp=datetime(d.year, d.month, d.day), open=o, close=c)
        for d, o, c in days
    ]


def decision_row(
    day: str, symbol: str, action: str, reason: str = "CROSS_UP"
) -> dict[str, Any]:
    return {"day": day, "symbol": symbol, "action": action, "reason_code": reason}


def fill_row(
    day: str,
    symbol: str,
    action: str,
    price: float,
    qty: float = 10.0,
    status: str = "filled",
    slippage: float | None = None,
) -> dict[str, Any]:
    return {
        "day": day,
        "symbol": symbol,
        "action": action,
        "status": status,
        "filled_avg_price": price,
        "filled_qty": qty,
        "slippage_bp": slippage,
    }


def session_row(day: str, invested: float | None, event: str = "DECIDED") -> dict:
    return {"day": day, "event": event, "invested_fraction": invested}


# --- exposure -----------------------------------------------------------------


def test_exposure_averages_only_decided_sessions() -> None:
    rows = [
        session_row("2026-08-10", 0.5),
        session_row("2026-08-11", 0.7),
        session_row("2026-08-11", 0.9, event="EXECUTED"),  # not an end-of-day mark
    ]

    exposure = summarise_exposure(rows)

    assert exposure.sessions == 2
    assert exposure.mean_invested == pytest.approx(0.6)
    assert exposure.max_invested == pytest.approx(0.7)


def test_exposure_is_none_rather_than_zero_when_nothing_was_recorded() -> None:
    exposure = summarise_exposure([])

    assert exposure.sessions == 0
    assert exposure.mean_invested is None


# --- signal capture -----------------------------------------------------------


def test_every_cross_up_lands_in_exactly_one_bucket() -> None:
    decisions = [
        decision_row("2026-08-10", "AAA", "BUY"),
        decision_row("2026-08-10", "BBB", "BUY_DECLINED_FULL"),
        decision_row("2026-08-10", "CCC", "BUY_DECLINED_CASH"),
        decision_row("2026-08-10", "DDD", "NO_ACTION", reason="NO_CROSS"),
    ]

    capture = summarise_capture(decisions, [], [])

    assert capture.cross_ups == 3
    assert capture.accounted_for is True
    assert capture.decision_capture == pytest.approx(1 / 3)


def test_fill_capture_shows_intended_buys_that_never_landed() -> None:
    decisions = [
        decision_row("2026-08-10", "AAA", "BUY"),
        decision_row("2026-08-10", "BBB", "BUY"),
    ]
    executions = [
        {"day": "2026-08-10", "symbol": "AAA", "action": "BUY", "succeeded": True},
        {"day": "2026-08-10", "symbol": "BBB", "action": "BUY", "succeeded": False},
    ]
    fills = [fill_row("2026-08-10", "AAA", "BUY", 100.0)]

    capture = summarise_capture(decisions, executions, fills)

    assert capture.submitted == 1
    assert capture.filled == 1
    assert capture.fill_capture == pytest.approx(0.5)


# --- returns ------------------------------------------------------------------


def test_strategy_return_measures_completed_round_trips() -> None:
    fills = [
        fill_row("2026-08-10", "AAA", "BUY", 100.0, qty=10.0),
        fill_row("2026-08-20", "AAA", "SELL", 110.0, qty=10.0),
    ]

    assert strategy_return(fills) == pytest.approx(0.10)


def test_an_open_position_is_not_counted_as_a_result() -> None:
    """Marking it to market here would blend a realized figure with an
    unrealized one in a single number."""

    fills = [fill_row("2026-08-10", "AAA", "BUY", 100.0)]

    assert strategy_return(fills) is None


def test_round_trips_are_weighted_by_capital_not_averaged_naively() -> None:
    fills = [
        fill_row("2026-08-10", "AAA", "BUY", 100.0, qty=1.0),  # $100 at +10%
        fill_row("2026-08-20", "AAA", "SELL", 110.0, qty=1.0),
        fill_row("2026-08-10", "BBB", "BUY", 100.0, qty=9.0),  # $900 at -10%
        fill_row("2026-08-20", "BBB", "SELL", 90.0, qty=9.0),
    ]

    # Naive mean would be 0.0; capital-weighted is -8%.
    assert strategy_return(fills) == pytest.approx(-0.08)


def test_unfilled_rows_are_ignored_by_the_return() -> None:
    fills = [
        fill_row("2026-08-10", "AAA", "BUY", 100.0),
        fill_row("2026-08-20", "AAA", "SELL", 110.0, status="rejected"),
    ]

    assert strategy_return(fills) is None


def test_benchmark_enters_at_the_next_sessions_open_not_the_decision_close() -> None:
    """The strategy decides on a close and fills the next session. A benchmark
    entered at the decision close would be handed an overnight gap the strategy
    never got -- the measurement fault that ended VS1's runs 2 and 3."""

    series = bars(
        [
            (date(2026, 8, 10), 100.0, 100.0),  # decision close
            (date(2026, 8, 11), 105.0, 106.0),  # gap up; entry is this OPEN
            (date(2026, 8, 12), 106.0, 110.0),  # exit close
        ]
    )

    result = benchmark_return({"AAA": series}, date(2026, 8, 10), date(2026, 8, 12))

    # 110 / 105 - 1, not 110 / 100 - 1.
    assert result == pytest.approx((110.0 - 105.0) / 105.0)


def test_benchmark_is_equal_weight_across_the_universe() -> None:
    up = bars(
        [
            (date(2026, 8, 10), 100.0, 100.0),
            (date(2026, 8, 11), 100.0, 100.0),
            (date(2026, 8, 12), 100.0, 120.0),
        ]
    )
    down = bars(
        [
            (date(2026, 8, 10), 100.0, 100.0),
            (date(2026, 8, 11), 100.0, 100.0),
            (date(2026, 8, 12), 100.0, 80.0),
        ]
    )

    result = benchmark_return(
        {"UP": up, "DOWN": down}, date(2026, 8, 10), date(2026, 8, 12)
    )

    assert result == pytest.approx(0.0)


def test_a_symbol_without_bars_at_both_ends_is_skipped() -> None:
    complete = bars(
        [
            (date(2026, 8, 10), 100.0, 100.0),
            (date(2026, 8, 11), 100.0, 100.0),
            (date(2026, 8, 12), 100.0, 110.0),
        ]
    )
    listed_late = bars([(date(2026, 8, 12), 50.0, 55.0)])

    result = benchmark_return(
        {"OK": complete, "LATE": listed_late}, date(2026, 8, 10), date(2026, 8, 12)
    )

    assert result == pytest.approx(0.10)


# --- readability --------------------------------------------------------------


def test_a_run_with_missed_sessions_is_not_readable() -> None:
    report = build_report(
        [decision_row("2026-08-10", "AAA", "BUY")],
        [],
        [{"day": "2026-08-12", "event": "DECIDED", "invested_fraction": 0.5,
          "missed_sessions": 2}],
        [],
    )

    assert report.sessions_missed == 2
    assert report.readable is False
    assert any("no decisions on record" in w for w in report.warnings)


def test_a_run_with_no_exposure_rows_is_not_readable() -> None:
    """The precondition DESIGN.md states outright."""

    report = build_report([decision_row("2026-08-10", "AAA", "BUY")], [], [], [])

    assert any("invested-exposure" in w for w in report.warnings)
    assert report.readable is False


def test_duplicate_rows_from_a_forced_rerun_are_not_double_counted() -> None:
    """VS1's scorecard inflated 104 real decisions to 214 rows this way."""

    rows = [
        decision_row("2026-08-10", "AAA", "BUY"),
        decision_row("2026-08-10", "AAA", "BUY"),
        decision_row("2026-08-10", "AAA", "HOLD", reason="NO_CROSS"),
    ]

    report = build_report(rows, [], [], [])

    assert report.capture.cross_ups == 0  # the last row in force is a HOLD
    assert report.sessions_decided == 1


def test_slippage_is_summarised_over_the_fills_that_have_one() -> None:
    fills = [
        fill_row("2026-08-10", "AAA", "BUY", 100.0, slippage=2.0),
        fill_row("2026-08-10", "BBB", "BUY", 100.0, slippage=4.0),
        fill_row("2026-08-10", "CCC", "BUY", 100.0, slippage=None),
    ]

    report = build_report([], [], [], fills)

    assert report.fills_measured == 2
    assert report.median_slippage_bp == pytest.approx(3.0)


def test_format_report_states_what_is_missing(tmp_path: Path) -> None:
    report = build_report([], [], [], [])
    text = format_report(report)

    assert "NOT READABLE AS A VERDICT" in text
    assert "Invested exposure" in text


def test_report_serialises_to_json_safe_types() -> None:
    payload = report_as_dict(build_report([decision_row("2026-08-10", "A", "BUY")], [], [], []))

    assert payload["start"] == "2026-08-10"
    assert payload["readable"] is False
    assert payload["capture"]["accounted_for"] is True


def test_load_report_on_an_empty_directory_does_not_crash(tmp_path: Path) -> None:
    report = load_report(tmp_path)

    assert report.sessions_decided == 0
    assert report.readable is False


# --- end to end, over rows the production code actually wrote -----------------


def test_report_reconciles_against_logs_the_real_pipeline_produced(
    tmp_path: Path,
) -> None:
    """Drives run_daily.run() across three sessions, then reports on whatever
    it wrote. If the log schema and the report's reader ever drift apart, this
    fails where a hand-written fixture would keep passing."""

    from tests.test_run_daily import FakeBarsSource, FakeTradingClient, bars_51
    from vs2.data.bars import BarsClient
    from vs2.data.broker import BrokerClient
    from vs2.data.market_calendar import MarketCalendar
    from vs2.data.orders import OrderClient
    from vs2.run_daily import LogPaths, run

    days = [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)]
    paths = LogPaths.under(tmp_path)

    # Session one: a cross up on Monday, decided after the close.
    trading = FakeTradingClient(days, "100000", [], cash="100000")
    run(
        MarketCalendar(trading),
        BrokerClient(trading),
        BarsClient(FakeBarsSource({"AAPL": bars_51(days[0], 50.0)})),
        OrderClient(trading),
        ["AAPL"],
        paths,
        execute=True,
        now=datetime(2026, 8, 10, 16, 15),
    )
    # Session two: Tuesday morning, the order goes in.
    run(
        MarketCalendar(trading),
        BrokerClient(trading),
        BarsClient(FakeBarsSource({"AAPL": bars_51(days[0], 50.0)})),
        OrderClient(trading),
        ["AAPL"],
        paths,
        execute=True,
        now=datetime(2026, 8, 11, 10, 0),
    )

    report = load_report(tmp_path)

    assert report.start == date(2026, 8, 10)
    assert report.sessions_decided == 1
    assert report.capture.cross_ups == 1
    assert report.capture.bought == 1
    assert report.capture.submitted == 1
    assert report.capture.accounted_for is True
    # Exposure was recorded, so that particular caveat is absent.
    assert report.exposure.sessions == 1
    assert not any("invested-exposure" in w for w in report.warnings)
    # No fills were reconciled (no FillReader wired), so the run correctly
    # reports itself as not yet answerable rather than inventing a return.
    assert report.strategy_return is None
    assert report.readable is False
