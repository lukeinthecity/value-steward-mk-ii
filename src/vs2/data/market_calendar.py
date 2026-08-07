"""Trading-day calendar, sourced from the broker rather than encoded here.

value-steward hand-wrote ~241 lines of holiday and early-close rules in
src/valuesteward/market_holidays.py, and an audit still found a missed early
close. Alpaca publishes the authoritative calendar, including each session's
actual open and close times, so VS2 asks instead of encoding -- see
docs/API_MENU.md section 10.

Named market_calendar rather than calendar to avoid shadowing the stdlib module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from alpaca.trading.requests import GetCalendarRequest

from vs2.data.retry import retry_alpaca

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradingSession:
    """One trading day with its real open/close, so early closes are explicit."""

    day: date
    open_at: datetime
    close_at: datetime


class _CalendarSource(Protocol):
    def get_calendar(self, filters: GetCalendarRequest) -> Any: ...


class MarketCalendar:
    def __init__(self, trading_client: _CalendarSource) -> None:
        self._trading_client = trading_client

    @retry_alpaca()
    def get_sessions(self, start: date, end: date) -> list[TradingSession]:
        """Return every trading session in [start, end], oldest first.

        Non-trading days are simply absent from the result -- that is how the
        broker reports weekends and holidays.
        """

        raw = self._trading_client.get_calendar(
            GetCalendarRequest(start=start, end=end)
        )
        if not isinstance(raw, list):
            return []
        sessions = [
            TradingSession(day=entry.date, open_at=entry.open, close_at=entry.close)
            for entry in raw
        ]
        return sorted(sessions, key=lambda s: s.day)

    def is_trading_day(self, day: date) -> bool:
        return any(session.day == day for session in self.get_sessions(day, day))

    def most_recent_session(self, on_or_before: date, lookback_days: int = 10) -> (
        TradingSession | None
    ):
        """The latest session at or before `on_or_before`.

        Answers "which day's close is the newest complete one" without assuming
        anything about weekends or holidays. `lookback_days` only has to exceed
        the longest possible market closure; 10 covers any holiday weekend.
        """

        start = date.fromordinal(on_or_before.toordinal() - lookback_days)
        sessions = self.get_sessions(start, on_or_before)
        if not sessions:
            logger.warning(
                "no trading sessions found in the %d days to %s",
                lookback_days,
                on_or_before,
            )
            return None
        return sessions[-1]
