"""Tests for the composed daily run. Fakes stand in for every Alpaca-facing
client -- calendar, broker, bars, and orders all wrap fakes, matching how
run_daily.main() wires the real ones together. Nothing here can reach a real
account: RecordingFakeTradingClient.submit_order is the only thing that could
place an order, and it just records what it was asked to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vs2.data.bars import BarsClient
from vs2.data.broker import BrokerClient
from vs2.data.decision_log import last_logged_day
from vs2.data.market_calendar import MarketCalendar
from vs2.data.orders import OrderClient
from vs2.run_daily import run


@dataclass
class FakeBar:
    timestamp: datetime
    close: float
    volume: float = 1_000_000.0


@dataclass
class FakeCalendarEntry:
    date: date
    open: datetime
    close: datetime


@dataclass
class FakePosition:
    symbol: str
    qty: str
    market_value: str
    avg_entry_price: str


@dataclass
class FakeAccount:
    equity: str


class FakeTradingClient:
    """Backs MarketCalendar, BrokerClient and OrderClient at once, exactly as
    one real TradingClient does in run_daily.main()."""

    def __init__(
        self,
        trading_days: list[date],
        equity: str,
        positions: list[FakePosition],
    ) -> None:
        self._trading_days = set(trading_days)
        self._equity = equity
        self._positions = positions
        self.submitted: list[Any] = []

    def get_calendar(self, filters: Any) -> list[FakeCalendarEntry]:
        return [
            FakeCalendarEntry(
                date=d,
                open=datetime(d.year, d.month, d.day, 9, 30, tzinfo=timezone.utc),
                close=datetime(d.year, d.month, d.day, 16, 0, tzinfo=timezone.utc),
            )
            for d in sorted(self._trading_days)
            if filters.start <= d <= filters.end
        ]

    def get_account(self) -> FakeAccount:
        return FakeAccount(equity=self._equity)

    def get_all_positions(self) -> list[FakePosition]:
        return self._positions

    def submit_order(self, order_data: Any) -> dict:
        self.submitted.append(order_data)
        return {"id": f"fake-{len(self.submitted)}", "symbol": order_data.symbol}


class FakeBarsSource:
    def __init__(self, data: dict[str, list[FakeBar]]) -> None:
        self._data = data

    def get_stock_bars(self, request: Any) -> Any:
        class _Response:
            def __init__(self, data: dict) -> None:
                self.data = data

        return _Response(self._data)


def bars_51(day: date, last_close: float) -> list[FakeBar]:
    """50 flat bars at 10.0 on the 50 days before `day`, then one bar on `day`
    at last_close -- a clean cross when last_close is far enough from 10.0,
    matching test_crossover.py's convention."""

    day_dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    out = [FakeBar(timestamp=day_dt - timedelta(days=50 - i), close=10.0) for i in range(50)]
    out.append(FakeBar(timestamp=day_dt, close=last_close))
    return out


def _harness(
    trading_days: list[date],
    equity: str,
    positions: list[FakePosition],
    bars: dict[str, list[FakeBar]],
) -> tuple[FakeTradingClient, MarketCalendar, BrokerClient, BarsClient, OrderClient]:
    trading = FakeTradingClient(trading_days, equity, positions)
    calendar = MarketCalendar(trading)
    broker = BrokerClient(trading)
    bars_client = BarsClient(FakeBarsSource(bars))
    orders = OrderClient(trading)
    return trading, calendar, broker, bars_client, orders


TODAY = date(2026, 8, 10)


def test_non_trading_day_does_nothing(tmp_path: Path) -> None:
    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=[],  # today is not in the calendar at all
        equity="100000",
        positions=[],
        bars={"AAPL": bars_51(TODAY, 50.0)},
    )
    log_path = tmp_path / "decisions.jsonl"

    result = run(
        calendar, broker, bars_client, orders, ["AAPL"], log_path, today=TODAY
    )

    assert result.trading_day is False
    assert result.decisions == []
    assert trading.submitted == []
    assert not log_path.exists()


def test_trading_day_computes_and_logs_decisions(tmp_path: Path) -> None:
    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=[TODAY],
        equity="100000",
        positions=[],
        bars={"AAPL": bars_51(TODAY, 50.0)},  # 10 -> 50 is a clean cross up
    )
    log_path = tmp_path / "decisions.jsonl"

    result = run(
        calendar, broker, bars_client, orders, ["AAPL"], log_path, today=TODAY
    )

    assert result.trading_day is True
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "BUY"
    assert log_path.exists()
    assert last_logged_day(log_path) == result.day


def test_dry_run_never_calls_submit_order(tmp_path: Path) -> None:
    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=[TODAY],
        equity="100000",
        positions=[],
        bars={"AAPL": bars_51(TODAY, 50.0)},
    )
    log_path = tmp_path / "decisions.jsonl"

    run(calendar, broker, bars_client, orders, ["AAPL"], log_path, today=TODAY)

    assert trading.submitted == []


def test_execute_submits_order_bearing_decisions(tmp_path: Path) -> None:
    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=[TODAY],
        equity="100000",
        positions=[],
        bars={"AAPL": bars_51(TODAY, 50.0)},
    )
    log_path = tmp_path / "decisions.jsonl"

    result = run(
        calendar,
        broker,
        bars_client,
        orders,
        ["AAPL"],
        log_path,
        execute=True,
        today=TODAY,
    )

    assert len(trading.submitted) == 1
    assert trading.submitted[0].symbol == "AAPL"
    assert len(result.submitted) == 1
    assert result.executed is True


def test_execute_with_no_order_bearing_decisions_submits_nothing(tmp_path: Path) -> None:
    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=[TODAY],
        equity="100000",
        positions=[],
        bars={"AAPL": bars_51(TODAY, 5.0)},  # 10 -> 5, no cross up, nothing held
    )
    log_path = tmp_path / "decisions.jsonl"

    run(calendar, broker, bars_client, orders, ["AAPL"], log_path, execute=True, today=TODAY)

    assert trading.submitted == []


def test_second_call_same_decision_day_is_a_noop_without_force(tmp_path: Path) -> None:
    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=[TODAY],
        equity="100000",
        positions=[],
        bars={"AAPL": bars_51(TODAY, 50.0)},
    )
    log_path = tmp_path / "decisions.jsonl"

    first = run(calendar, broker, bars_client, orders, ["AAPL"], log_path, today=TODAY)
    second = run(calendar, broker, bars_client, orders, ["AAPL"], log_path, today=TODAY)

    assert first.already_ran is False
    assert second.already_ran is True
    assert second.decisions == []
    # The log must not have grown on the second call.
    assert len(log_path.read_text(encoding="utf-8").strip().split("\n")) == 1


def test_force_reruns_even_when_already_logged(tmp_path: Path) -> None:
    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=[TODAY],
        equity="100000",
        positions=[],
        bars={"AAPL": bars_51(TODAY, 50.0)},
    )
    log_path = tmp_path / "decisions.jsonl"

    run(calendar, broker, bars_client, orders, ["AAPL"], log_path, today=TODAY)
    second = run(
        calendar, broker, bars_client, orders, ["AAPL"], log_path, force=True, today=TODAY
    )

    assert second.already_ran is False
    assert len(second.decisions) == 1
    # Forcing a rerun appends again rather than replacing -- the log is a
    # record of what happened, including that it ran twice.
    assert len(log_path.read_text(encoding="utf-8").strip().split("\n")) == 2


def test_held_position_sells_on_cross_down(tmp_path: Path) -> None:
    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=[TODAY],
        equity="100000",
        positions=[FakePosition("AAPL", qty="10", market_value="500", avg_entry_price="50")],
        bars={"AAPL": bars_51(TODAY, 5.0)},  # held, but this cross series is a cross DOWN
    )
    log_path = tmp_path / "decisions.jsonl"

    result = run(
        calendar, broker, bars_client, orders, ["AAPL"], log_path, execute=True, today=TODAY
    )

    assert result.decisions[0].action == "SELL"
    assert trading.submitted[0].side.value == "sell"
