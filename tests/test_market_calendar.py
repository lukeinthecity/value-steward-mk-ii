"""Tests for the trading-day calendar. No network -- a fake stands in for Alpaca."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from vs2.data.market_calendar import MarketCalendar, TradingSession


@dataclass
class FakeCalendarEntry:
    date: date
    open: datetime
    close: datetime


class FakeCalendarSource:
    def __init__(self, entries: list[FakeCalendarEntry]) -> None:
        self._entries = entries
        self.last_request: Any = None

    def get_calendar(self, filters: Any) -> list[FakeCalendarEntry]:
        self.last_request = filters
        return [e for e in self._entries if filters.start <= e.date <= filters.end]


def _entry(day: int, close_hour: int = 16) -> FakeCalendarEntry:
    return FakeCalendarEntry(
        date=date(2026, 8, day),
        open=datetime(2026, 8, day, 9, 30, tzinfo=timezone.utc),
        close=datetime(2026, 8, day, close_hour, 0, tzinfo=timezone.utc),
    )


def test_returns_sessions_oldest_first() -> None:
    source = FakeCalendarSource([_entry(7), _entry(5), _entry(6)])
    sessions = MarketCalendar(source).get_sessions(date(2026, 8, 1), date(2026, 8, 31))

    assert [s.day.day for s in sessions] == [5, 6, 7]


def test_early_close_is_preserved_not_assumed() -> None:
    # A 1pm close (e.g. day before Thanksgiving) must survive intact -- this is
    # the case value-steward's hand-rolled holiday table got wrong.
    source = FakeCalendarSource([_entry(5, close_hour=13)])
    sessions = MarketCalendar(source).get_sessions(date(2026, 8, 1), date(2026, 8, 31))

    assert sessions[0].close_at.hour == 13


def test_non_trading_days_are_simply_absent() -> None:
    source = FakeCalendarSource([_entry(7)])  # only the 7th is a session
    calendar = MarketCalendar(source)

    assert calendar.is_trading_day(date(2026, 8, 7)) is True
    assert calendar.is_trading_day(date(2026, 8, 8)) is False


def test_most_recent_session_skips_back_over_a_closure() -> None:
    # Nothing on the 8th or 9th (weekend); the 7th is the newest complete close.
    source = FakeCalendarSource([_entry(5), _entry(6), _entry(7)])
    session = MarketCalendar(source).most_recent_session(date(2026, 8, 9))

    assert session is not None
    assert session.day == date(2026, 8, 7)


def test_most_recent_session_returns_none_when_nothing_in_window() -> None:
    source = FakeCalendarSource([])
    assert MarketCalendar(source).most_recent_session(date(2026, 8, 9)) is None


def test_most_recent_session_queries_a_bounded_lookback() -> None:
    source = FakeCalendarSource([_entry(7)])
    MarketCalendar(source).most_recent_session(date(2026, 8, 9), lookback_days=10)

    assert source.last_request.start == date(2026, 7, 30)
    assert source.last_request.end == date(2026, 8, 9)


def test_unexpected_response_shape_yields_no_sessions() -> None:
    class WeirdSource:
        def get_calendar(self, filters: Any) -> object:
            return object()

    assert MarketCalendar(WeirdSource()).get_sessions(
        date(2026, 8, 1), date(2026, 8, 2)
    ) == []


def test_trading_session_is_immutable() -> None:
    session = TradingSession(
        day=date(2026, 8, 7),
        open_at=datetime(2026, 8, 7, 9, 30, tzinfo=timezone.utc),
        close_at=datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc),
    )
    try:
        session.day = date(2026, 8, 8)  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("TradingSession should be frozen")


# --- which session may the rule evaluate? -------------------------------------
#
# Alpaca returns NAIVE datetimes carrying Eastern wall-clock time -- verified
# against alpaca-py 0.43.5. These cases use naive fixtures for that reason: a
# UTC-aware fixture would test a shape the broker never produces.


def _naive_entry(day: int, close_hour: int = 16) -> FakeCalendarEntry:
    return FakeCalendarEntry(
        date=date(2026, 8, day),
        open=datetime(2026, 8, day, 9, 30),
        close=datetime(2026, 8, day, close_hour, 0),
    )


def test_latest_completed_session_is_today_once_the_bell_has_rung() -> None:
    source = FakeCalendarSource([_naive_entry(10)])
    session = MarketCalendar(source).latest_completed_session(
        datetime(2026, 8, 10, 16, 15)
    )

    assert session is not None
    assert session.day == date(2026, 8, 10)


def test_latest_completed_session_is_yesterday_during_todays_session() -> None:
    """The safety property the whole daily cycle rests on: asked at 10am, the
    answer is yesterday, so an in-progress bar can never be read as a close."""

    source = FakeCalendarSource([_naive_entry(10), _naive_entry(11)])
    session = MarketCalendar(source).latest_completed_session(
        datetime(2026, 8, 11, 10, 0)
    )

    assert session is not None
    assert session.day == date(2026, 8, 10)


def test_latest_completed_session_respects_an_early_close() -> None:
    """At 14:00 on a 13:00-close day the session IS complete, where a hardcoded
    16:00 assumption would say it is not."""

    source = FakeCalendarSource([_naive_entry(10, close_hour=13)])
    session = MarketCalendar(source).latest_completed_session(
        datetime(2026, 8, 10, 14, 0)
    )

    assert session is not None
    assert session.day == date(2026, 8, 10)


def test_latest_completed_session_is_none_before_anything_has_closed() -> None:
    source = FakeCalendarSource([_naive_entry(10)])
    assert (
        MarketCalendar(source).latest_completed_session(datetime(2026, 8, 10, 9, 45))
        is None
    )


def test_an_aware_now_is_converted_rather_than_compared_raw() -> None:
    """20:15 UTC is 16:15 Eastern -- after the close. Comparing the raw clock
    numbers would call it complete for the wrong reason, or not at all."""

    source = FakeCalendarSource([_naive_entry(10)])
    session = MarketCalendar(source).latest_completed_session(
        datetime(2026, 8, 10, 20, 15, tzinfo=timezone.utc)
    )

    assert session is not None
    assert session.day == date(2026, 8, 10)


def test_a_utc_morning_is_not_mistaken_for_a_closed_session() -> None:
    # 13:00 UTC is 09:00 Eastern: the market has not even opened.
    source = FakeCalendarSource([_naive_entry(10)])
    assert (
        MarketCalendar(source).latest_completed_session(
            datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)
        )
        is None
    )


# --- is the market open right now? --------------------------------------------


def test_is_open_inside_the_session() -> None:
    source = FakeCalendarSource([_naive_entry(10)])
    assert MarketCalendar(source).is_open(datetime(2026, 8, 10, 10, 0)) is True


def test_is_open_is_false_before_the_open_and_after_the_close() -> None:
    calendar = MarketCalendar(FakeCalendarSource([_naive_entry(10)]))

    assert calendar.is_open(datetime(2026, 8, 10, 9, 0)) is False
    assert calendar.is_open(datetime(2026, 8, 10, 16, 30)) is False


def test_is_open_respects_an_early_close() -> None:
    calendar = MarketCalendar(FakeCalendarSource([_naive_entry(10, close_hour=13)]))

    assert calendar.is_open(datetime(2026, 8, 10, 12, 30)) is True
    assert calendar.is_open(datetime(2026, 8, 10, 14, 0)) is False


def test_is_open_is_false_on_a_non_session_day() -> None:
    calendar = MarketCalendar(FakeCalendarSource([_naive_entry(10)]))
    assert calendar.is_open(datetime(2026, 8, 11, 10, 0)) is False
