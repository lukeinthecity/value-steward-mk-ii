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
