"""Trading-day calendar, sourced from the broker rather than encoded here.

value-steward hand-wrote ~241 lines of holiday and early-close rules in
src/valuesteward/market_holidays.py, and an audit still found a missed early
close. Alpaca publishes the authoritative calendar, including each session's
actual open and close times, so VS2 asks instead of encoding -- see
docs/API_MENU.md section 10.

Named market_calendar rather than calendar to avoid shadowing the stdlib module.

**Alpaca returns naive datetimes on its calendar entries, and they carry US
Eastern wall-clock time** -- verified against alpaca-py 0.43.5:
`Calendar(date="2026-08-07", open="09:30", close="16:00")` yields
`datetime(2026, 8, 7, 16, 0)` with `tzinfo=None`. Every comparison against
"now" therefore has to say which zone it means, or it silently reads a UTC
clock as an Eastern one and is four or five hours wrong depending on the time
of year. `_to_eastern` is that statement, applied at each comparison rather
than by rewriting what `get_sessions` returns -- the sessions themselves stay
a faithful passthrough of what the broker said.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from alpaca.trading.requests import GetCalendarRequest

from vs2.data.retry import retry_alpaca

logger = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("America/New_York")


def _to_eastern(moment: datetime) -> datetime:
    """Read `moment` as US Eastern.

    A naive datetime is *interpreted* as Eastern (that is what the broker's
    calendar hands back, and what a machine clock set to Eastern reports); an
    aware one is converted. Either way the result is comparable to a session's
    open and close without depending on the host's timezone being right.
    """

    if moment.tzinfo is None:
        return moment.replace(tzinfo=MARKET_TZ)
    return moment.astimezone(MARKET_TZ)


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

    def latest_completed_session(
        self, now: datetime, lookback_days: int = 10
    ) -> TradingSession | None:
        """The most recent session whose close has already passed at `now`.

        This is the day the crossover rule is allowed to evaluate, and it is a
        stricter question than `most_recent_session`: today counts only once
        today's bell has actually rung. Asked at 11am, it returns *yesterday*,
        which is what stops an in-progress daily bar -- whose "close" is merely
        the last trade so far -- from being read as a finished one.

        Returns None when nothing in the window has closed yet, which the
        caller must treat as "do nothing", never as "no cross".
        """

        eastern_now = _to_eastern(now)
        start = date.fromordinal(eastern_now.date().toordinal() - lookback_days)
        sessions = self.get_sessions(start, eastern_now.date())
        completed = [s for s in sessions if _to_eastern(s.close_at) <= eastern_now]
        if not completed:
            logger.warning(
                "no completed trading session in the %d days to %s",
                lookback_days,
                eastern_now,
            )
            return None
        return completed[-1]

    def is_open(self, now: datetime) -> bool:
        """True if `now` falls inside a session's real open/close.

        Uses the session's actual times, so an early close is respected rather
        than assumed to be 4pm -- the case value-steward's hand-rolled table
        got wrong.
        """

        eastern_now = _to_eastern(now)
        for session in self.get_sessions(eastern_now.date(), eastern_now.date()):
            if _to_eastern(session.open_at) <= eastern_now <= _to_eastern(
                session.close_at
            ):
                return True
        return False
