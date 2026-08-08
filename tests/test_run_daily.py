"""Tests for the composed daily run. Fakes stand in for every Alpaca-facing
client -- calendar, broker, bars, and orders all wrap fakes, matching how
run_daily.main() wires the real ones together. Nothing here can reach a real
account: FakeTradingClient.submit_order is the only thing that could place an
order, and it just records what it was asked to do.

**Deciding and executing happen at different times of day**, so these tests
drive `now` explicitly rather than a bare date. Two moments recur:

* `AFTER_CLOSE` -- Monday 16:15, the market shut. The latest *completed*
  session is Monday, so Monday is decided. Nothing can be submitted.
* `IN_SESSION` -- Tuesday 10:00, the market open. The latest completed session
  is still Monday (Tuesday has not closed), so Monday's decisions execute.

Calendar entries are built with **naive** datetimes on purpose: that is what
Alpaca actually returns, and it carries Eastern wall-clock time. Using
UTC-aware fixtures here would test a shape the broker never produces.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from vs2.data.bars import BarsClient
from vs2.data.broker import BrokerClient
from vs2.data.decision_log import last_logged_day, load_decisions
from vs2.data.market_calendar import MarketCalendar
from vs2.data.orders import OrderClient
from vs2.data.run_lock import single_instance
from vs2.data.session_log import load_sessions
from vs2.run_daily import LogPaths, run


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
    cash: str


class FakeTradingClient:
    """Backs MarketCalendar, BrokerClient and OrderClient at once, exactly as
    one real TradingClient does in run_daily.main()."""

    def __init__(
        self,
        trading_days: list[date],
        equity: str,
        positions: list[FakePosition],
        fail_symbols: set[str] | None = None,
        cash: str | None = None,
    ) -> None:
        self._trading_days = set(trading_days)
        self._equity = equity
        self._cash = cash if cash is not None else equity
        self.positions = positions
        self._fail_symbols = fail_symbols or set()
        self.submitted: list[Any] = []

    def get_calendar(self, filters: Any) -> list[FakeCalendarEntry]:
        return [
            FakeCalendarEntry(
                date=d,
                open=datetime(d.year, d.month, d.day, 9, 30),
                close=datetime(d.year, d.month, d.day, 16, 0),
            )
            for d in sorted(self._trading_days)
            if filters.start <= d <= filters.end
        ]

    def get_account(self) -> FakeAccount:
        return FakeAccount(equity=self._equity, cash=self._cash)

    def get_all_positions(self) -> list[FakePosition]:
        return self.positions

    def submit_order(self, order_data: Any) -> dict:
        self.submitted.append(order_data)
        if order_data.symbol in self._fail_symbols:
            raise RuntimeError(f"rejected: {order_data.symbol}")
        return {"id": f"fake-{len(self.submitted)}", "symbol": order_data.symbol}


class FakeBarsSource:
    def __init__(self, data: dict[str, list[FakeBar]]) -> None:
        self._data = data

    def get_stock_bars(self, request: Any) -> Any:
        class _Response:
            def __init__(self, data: dict) -> None:
                self.data = data

        return _Response(self._data)


DECISION_DAY = date(2026, 8, 10)  # Monday
NEXT_DAY = date(2026, 8, 11)  # Tuesday
TRADING_DAYS = [DECISION_DAY, NEXT_DAY]

AFTER_CLOSE = datetime(2026, 8, 10, 16, 15)  # Monday, market shut -> decide
IN_SESSION = datetime(2026, 8, 11, 10, 0)  # Tuesday, market open -> execute


def bars_51(day: date, last_close: float) -> list[FakeBar]:
    """50 flat bars at 10.0 on the 50 days before `day`, then one bar on `day`
    at last_close -- a clean cross when last_close is far enough from 10.0,
    matching test_crossover.py's convention."""

    day_dt = datetime(day.year, day.month, day.day)
    out = [FakeBar(timestamp=day_dt - timedelta(days=50 - i), close=10.0) for i in range(50)]
    out.append(FakeBar(timestamp=day_dt, close=last_close))
    return out


def _harness(
    trading_days: list[date],
    equity: str,
    positions: list[FakePosition],
    bars: dict[str, list[FakeBar]],
    fail_symbols: set[str] | None = None,
    cash: str | None = None,
) -> tuple[FakeTradingClient, MarketCalendar, BrokerClient, BarsClient, OrderClient]:
    trading = FakeTradingClient(trading_days, equity, positions, fail_symbols, cash)
    calendar = MarketCalendar(trading)
    broker = BrokerClient(trading)
    bars_client = BarsClient(FakeBarsSource(bars))
    orders = OrderClient(trading)
    return trading, calendar, broker, bars_client, orders


def _standard(
    tmp_path: Path,
    *,
    last_close: float = 50.0,
    positions: list[FakePosition] | None = None,
    trading_days: list[date] | None = None,
    fail_symbols: set[str] | None = None,
    symbols: tuple[str, ...] = ("AAPL",),
    cash: str | None = None,
) -> tuple[FakeTradingClient, dict[str, Any], LogPaths]:
    """The common setup: one universe, bars ending on DECISION_DAY."""

    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=TRADING_DAYS if trading_days is None else trading_days,
        equity="100000",
        positions=positions or [],
        bars={s: bars_51(DECISION_DAY, last_close) for s in symbols},
        fail_symbols=fail_symbols,
        cash=cash,
    )
    kwargs = {
        "calendar": calendar,
        "broker": broker,
        "bars_client": bars_client,
        "order_client": orders,
        "universe": list(symbols),
    }
    return trading, kwargs, LogPaths.under(tmp_path)


# --- deciding ----------------------------------------------------------------


def test_no_completed_session_does_nothing(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(tmp_path, trading_days=[])

    result = run(**kwargs, paths=paths, now=AFTER_CLOSE)

    assert result.trading_day is False
    assert result.decisions == []
    assert trading.submitted == []
    assert not paths.decisions.exists()


def test_completed_session_computes_and_logs_decisions(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(tmp_path)

    result = run(**kwargs, paths=paths, now=AFTER_CLOSE)

    assert result.trading_day is True
    assert result.day == DECISION_DAY
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "BUY"
    assert last_logged_day(paths.decisions) == DECISION_DAY


def test_the_decision_day_is_the_last_closed_session_not_the_current_one(
    tmp_path: Path,
) -> None:
    """Asked mid-session on Tuesday, the rule still evaluates Monday. Tuesday's
    close has not happened, so there is nothing yet to compare an average to."""

    trading, kwargs, paths = _standard(tmp_path)

    result = run(**kwargs, paths=paths, now=IN_SESSION)

    assert result.day == DECISION_DAY


def test_dry_run_never_calls_submit_order(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(tmp_path)

    run(**kwargs, paths=paths, now=AFTER_CLOSE)

    assert trading.submitted == []


def test_second_call_same_decision_day_is_a_noop_without_force(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(tmp_path)

    first = run(**kwargs, paths=paths, now=AFTER_CLOSE)
    second = run(**kwargs, paths=paths, now=AFTER_CLOSE)

    assert first.decided is True
    assert second.decided is False
    assert second.already_ran is True
    # The log must not have grown on the second call.
    assert len(paths.decisions.read_text(encoding="utf-8").strip().split("\n")) == 1


def test_force_reruns_even_when_already_logged(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(tmp_path)

    run(**kwargs, paths=paths, now=AFTER_CLOSE)
    second = run(**kwargs, paths=paths, force=True, now=AFTER_CLOSE)

    assert second.decided is True
    assert len(second.decisions) == 1
    # Forcing a rerun appends again rather than replacing -- the log is a
    # record of what happened, including that it ran twice.
    assert len(paths.decisions.read_text(encoding="utf-8").strip().split("\n")) == 2


# --- stale and in-progress bars ----------------------------------------------
#
# The regression these exist for: with today a trading day and the newest bar
# three days old, the previous implementation submitted a real order and
# stamped the execution log with the stale date, silently. The decision day now
# comes from the calendar, and bars have to reach it.


def test_stale_bars_decide_nothing_and_are_recorded(tmp_path: Path) -> None:
    stale_day = date(2026, 8, 7)  # the Friday before
    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=[stale_day] + TRADING_DAYS,
        equity="100000",
        positions=[],
        bars={"AAPL": bars_51(stale_day, 50.0)},  # never reaches Monday
    )
    paths = LogPaths.under(tmp_path)

    result = run(
        calendar, broker, bars_client, orders, ["AAPL"], paths,
        execute=True, now=IN_SESSION,
    )

    assert result.stale_bars is True
    assert result.bars_as_of == stale_day
    assert result.decisions == []
    assert trading.submitted == []
    assert not paths.decisions.exists()

    events = [row["event"] for row in load_sessions(paths.sessions)]
    assert "STALE_BARS" in events


def test_an_in_progress_bar_is_trimmed_rather_than_treated_as_a_close(
    tmp_path: Path,
) -> None:
    """During Tuesday's session Alpaca serves a Tuesday bar whose close is only
    the last trade so far. It must not be evaluated, but its presence must also
    not make Monday look stale."""

    bars = bars_51(DECISION_DAY, 50.0)
    bars.append(FakeBar(timestamp=datetime(2026, 8, 11, 10, 0), close=999.0))
    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=TRADING_DAYS, equity="100000", positions=[], bars={"AAPL": bars}
    )
    paths = LogPaths.under(tmp_path)

    result = run(calendar, broker, bars_client, orders, ["AAPL"], paths, now=IN_SESSION)

    assert result.stale_bars is False
    assert result.day == DECISION_DAY
    # The 999.0 in-progress print was not the close the rule saw.
    assert result.decisions[0].close == 50.0


# --- executing ---------------------------------------------------------------


def test_execute_submits_order_bearing_decisions(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(tmp_path)

    result = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    assert len(trading.submitted) == 1
    assert trading.submitted[0].symbol == "AAPL"
    assert result.submitted[0].succeeded is True
    assert result.executed is True


def test_nothing_is_submitted_while_the_market_is_shut(tmp_path: Path) -> None:
    """The decision is made after the close; the order waits for a real session
    rather than queueing overnight into an unpredictable opening auction."""

    trading, kwargs, paths = _standard(tmp_path)

    result = run(**kwargs, paths=paths, execute=True, now=AFTER_CLOSE)

    assert result.decided is True
    assert result.executed is False
    assert result.market_open is False
    assert trading.submitted == []


def test_decide_after_close_then_execute_next_session(tmp_path: Path) -> None:
    """The whole two-phase cycle, in the order cron runs it."""

    trading, kwargs, paths = _standard(tmp_path)

    decided = run(**kwargs, paths=paths, execute=True, now=AFTER_CLOSE)
    executed = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    assert decided.decided is True and decided.executed is False
    assert executed.decided is False and executed.executed is True
    assert len(trading.submitted) == 1
    # One decision row, written once, executed once.
    assert len(paths.decisions.read_text(encoding="utf-8").strip().split("\n")) == 1


def test_execution_replays_logged_decisions_rather_than_recomputing(
    tmp_path: Path,
) -> None:
    """The morning's own sells change the portfolio. Recomputing then would
    produce different orders from the ones on record -- so the logged row is
    the contract, and this pins that."""

    trading, kwargs, paths = _standard(tmp_path)

    run(**kwargs, paths=paths, execute=True, now=AFTER_CLOSE)
    logged = load_decisions(paths.decisions, DECISION_DAY)
    assert logged[0].action == "BUY"

    # Between deciding and executing, the position appears. A recompute would
    # now see AAPL as held and produce HOLD instead of BUY.
    trading.positions = [
        FakePosition("AAPL", qty="100", market_value="5000", avg_entry_price="50")
    ]

    result = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    assert result.executed is True
    assert len(trading.submitted) == 1
    assert trading.submitted[0].side.value == "buy"


def test_execute_with_no_order_bearing_decisions_submits_nothing(
    tmp_path: Path,
) -> None:
    trading, kwargs, paths = _standard(tmp_path, last_close=5.0)

    run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    assert trading.submitted == []


def test_held_position_sells_on_cross_down(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(
        tmp_path,
        last_close=5.0,
        positions=[
            FakePosition("AAPL", qty="10", market_value="500", avg_entry_price="50")
        ],
    )

    result = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    assert result.decisions[0].action == "SELL"
    assert trading.submitted[0].side.value == "sell"


def test_submitted_orders_carry_a_deterministic_client_order_id(
    tmp_path: Path,
) -> None:
    """The key reconciliation looks a fill up by, and what makes a resubmission
    of an order that landed get rejected by the broker rather than duplicated."""

    trading, kwargs, paths = _standard(tmp_path)

    run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    assert trading.submitted[0].client_order_id == "vs2-2026-08-10-AAPL-BUY"


# --- the execute-mode guard is independent of the dry-run guard -------------


def test_dry_run_then_execute_same_day_is_not_blocked_by_the_dry_run(
    tmp_path: Path,
) -> None:
    # Dry-run logs decisions; that must not make --execute think it already ran.
    trading, kwargs, paths = _standard(tmp_path)

    run(**kwargs, paths=paths, now=AFTER_CLOSE)
    executed = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    assert executed.executed is True
    assert len(trading.submitted) == 1


def test_second_execute_call_same_day_is_a_noop_without_force(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(tmp_path)

    first = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)
    second = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    assert first.executed is True
    assert second.executed is False
    assert len(trading.submitted) == 1  # not resubmitted


def test_many_intraday_ticks_converge_on_one_execution(tmp_path: Path) -> None:
    """Cron fires repeatedly through the session on purpose. Every tick after
    the first has to be a genuine no-op, not merely usually one."""

    trading, kwargs, paths = _standard(tmp_path)

    for hour in (10, 11, 13, 15):
        run(**kwargs, paths=paths, execute=True, now=datetime(2026, 8, 11, hour, 0))

    assert len(trading.submitted) == 1
    assert len(paths.decisions.read_text(encoding="utf-8").strip().split("\n")) == 1


# --- partial failure: recorded, not silently marked done --------------------


def test_partial_execution_failure_does_not_stop_remaining_orders(
    tmp_path: Path,
) -> None:
    trading, kwargs, paths = _standard(
        tmp_path, symbols=("GOOD", "BAD"), fail_symbols={"BAD"}
    )

    result = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    # Both were attempted -- BAD failing did not stop GOOD from being tried.
    assert {o.symbol for o in trading.submitted} == {"GOOD", "BAD"}
    by_symbol = {r.decision.symbol: r for r in result.submitted}
    assert by_symbol["GOOD"].succeeded is True
    assert by_symbol["BAD"].succeeded is False


def test_partial_execution_failure_still_counts_as_attempted_for_the_guard(
    tmp_path: Path,
) -> None:
    # A run where some orders failed must still block an automatic retry --
    # see execution_log.py's docstring for why recomputing and resubmitting is
    # not a safe automatic response to a partial failure.
    trading, kwargs, paths = _standard(
        tmp_path, symbols=("GOOD", "BAD"), fail_symbols={"BAD"}
    )

    first = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)
    second = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    assert first.executed is True
    assert second.executed is False
    assert len(trading.submitted) == 2  # neither symbol submitted a second time


def test_force_retries_after_a_partial_failure(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(
        tmp_path, symbols=("GOOD", "BAD"), fail_symbols={"BAD"}
    )

    run(**kwargs, paths=paths, execute=True, now=IN_SESSION)
    second = run(**kwargs, paths=paths, execute=True, force=True, now=IN_SESSION)

    assert second.executed is True
    # force=True resubmits -- GOOD is attempted again too, which is exactly why
    # it requires a human to opt in rather than happening automatically. The
    # broker's uniqueness check on client_order_id is the backstop against the
    # resubmission actually duplicating a filled order.
    assert len(trading.submitted) == 4


# --- the cash cap, end to end ------------------------------------------------


def test_buys_beyond_available_cash_are_declined_not_submitted(
    tmp_path: Path,
) -> None:
    """Equity sizes the slot; cash pays for it. When they disagree the excess
    is recorded as a capacity decision, not fired at the broker to bounce."""

    trading, kwargs, paths = _standard(
        tmp_path, symbols=("AAA", "BBB", "CCC"), cash="11000"
    )

    result = run(**kwargs, paths=paths, execute=True, now=IN_SESSION)

    actions = {d.symbol: d.action for d in result.decisions}
    assert sum(1 for a in actions.values() if a == "BUY") == 2
    assert sum(1 for a in actions.values() if a == "BUY_DECLINED_CASH") == 1
    assert len(trading.submitted) == 2
    assert len(result.declined_for_cash) == 1


# --- the session log ---------------------------------------------------------


def test_a_decided_session_records_its_exposure(tmp_path: Path) -> None:
    """DESIGN.md requires average invested exposure at the Day-60 review, and
    it cannot be reconstructed later if it was never written down."""

    trading, kwargs, paths = _standard(
        tmp_path,
        positions=[
            FakePosition("AAPL", qty="100", market_value="40000", avg_entry_price="400")
        ],
        last_close=5.0,
    )

    run(**kwargs, paths=paths, now=AFTER_CLOSE)

    rows = [r for r in load_sessions(paths.sessions) if r["event"] == "DECIDED"]
    assert len(rows) == 1
    assert rows[0]["equity"] == 100_000.0
    assert rows[0]["long_market_value"] == 40_000.0
    assert rows[0]["position_count"] == 1
    assert rows[0]["invested_fraction"] == 0.4


def test_a_missed_session_is_counted_rather_than_averaged_over(
    tmp_path: Path,
) -> None:
    """A day with no decision rows is otherwise ambiguous between 'the rule
    found nothing' and 'the run never happened'."""

    monday = date(2026, 8, 10)
    wednesday = date(2026, 8, 12)
    days = [monday, date(2026, 8, 11), wednesday]

    trading, calendar, broker, bars_client, orders = _harness(
        trading_days=days, equity="100000", positions=[],
        bars={"AAPL": bars_51(monday, 50.0)},
    )
    paths = LogPaths.under(tmp_path)
    run(calendar, broker, bars_client, orders, ["AAPL"], paths,
        now=datetime(2026, 8, 10, 16, 15))

    # Tuesday never ran. Wednesday's bars arrive and it decides again.
    _, calendar2, broker2, bars2, orders2 = _harness(
        trading_days=days, equity="100000", positions=[],
        bars={"AAPL": bars_51(wednesday, 60.0)},
    )
    result = run(calendar2, broker2, bars2, orders2, ["AAPL"], paths,
                 now=datetime(2026, 8, 12, 16, 15))

    assert result.missed_sessions == 1


# --- concurrent invocations: the lock, not the guard, prevents the race -----


def test_run_returns_already_running_when_the_lock_is_already_held(
    tmp_path: Path,
) -> None:
    # Simulates the actual failure mode this closes: a second cron tick
    # firing while a first invocation is still in flight. Pre-acquiring the
    # lock here stands in for "another process already has it".
    trading, kwargs, paths = _standard(tmp_path)

    with single_instance(paths.lock):
        result = run(**kwargs, paths=paths, now=AFTER_CLOSE)

    assert result.already_running is True
    assert result.trading_day is False
    assert result.decisions == []
    # Nothing was fetched or submitted -- the lock check happens before any
    # of that, not after.
    assert trading.submitted == []
    assert not paths.decisions.exists()


def test_run_succeeds_normally_once_the_lock_is_free_again(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(tmp_path)

    with single_instance(paths.lock):
        pass  # acquired and released -- simulates a prior run finishing

    result = run(**kwargs, paths=paths, now=AFTER_CLOSE)

    assert result.already_running is False
    assert result.trading_day is True
    assert len(result.decisions) == 1


def test_already_running_is_false_on_the_ordinary_happy_path(tmp_path: Path) -> None:
    trading, kwargs, paths = _standard(tmp_path)

    result = run(**kwargs, paths=paths, now=AFTER_CLOSE)

    assert result.already_running is False


def test_lock_is_released_after_a_normal_run_so_the_next_one_can_proceed(
    tmp_path: Path,
) -> None:
    # If run() ever failed to release the lock on its own successful exit,
    # this second call would incorrectly see already_running=True.
    trading, kwargs, paths = _standard(tmp_path, last_close=5.0)

    run(**kwargs, paths=paths, now=AFTER_CLOSE)
    second = run(**kwargs, paths=paths, force=True, now=AFTER_CLOSE)

    assert second.already_running is False
