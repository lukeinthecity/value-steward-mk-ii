"""50-day moving-average crossover detection -- the entire decision logic.

Pure computation. No network, no filesystem, no clock-dependent behavior except
the audit timestamp. Everything here can be checked by hand against a price
chart, which is the point: see DESIGN.md's "The rule".

Conventions, stated explicitly because each one is a choice:

* The moving average at day *t* includes day *t*'s own close --
  `sma[t] = mean(close[t-49..t])` for a 50-day window. This is the standard
  convention and is what "the price crossed its 50-day average" means to a
  reader looking at a chart.

* A cross-up requires `close[t-1] <= sma[t-1]` and `close[t] > sma[t]`. The
  `<=` on the prior day is deliberate: it makes the two prior-day states
  exhaustive, so a symbol sitting exactly on its average cannot fall into a
  gap where it never signals either way. Cross-down is the mirror
  (`>=` then `<`). The two can never both fire, since they disagree about
  `close[t]` versus `sma[t]`.

* Evaluating a cross needs the average on *both* day t and day t-1, so a
  50-day window needs 51 bars. Fewer than that is reported as
  INSUFFICIENT_HISTORY, never as "no cross" -- a skipped symbol and a symbol
  that genuinely did not cross are different facts and must not share a code.

Malformed input returns its own reason code rather than a computed answer.
VS1's defining failure was machinery that produced plausible numbers from a
population it had misread; here, anything that would make the arithmetic
meaningless is reported as such instead.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = 50

Signal = Literal[
    "CROSS_UP",
    "CROSS_DOWN",
    "NO_CROSS",
    "INSUFFICIENT_HISTORY",
    "UNSORTED_BARS",
    "INVALID_CLOSE",
]

ACTIONABLE: frozenset[str] = frozenset({"CROSS_UP", "CROSS_DOWN"})


class BarLike(Protocol):
    """The only two bar fields this module reads."""

    timestamp: Any
    close: Any


@dataclass(frozen=True)
class CrossSignal:
    """One symbol's verdict for one decision day.

    Carries the prior day's values as well as the current day's, so a reader
    can reconstruct exactly why the verdict was reached -- the row shape
    DESIGN.md's "Measurement" section specifies.
    """

    symbol: str
    day: date | None
    signal: Signal
    reason_code: str
    close: float | None
    sma: float | None
    prior_close: float | None
    prior_sma: float | None
    bars_available: int
    window: int
    evaluated_at: str

    @property
    def is_actionable(self) -> bool:
        """True only for a real cross. Never true for a skip or a bad input."""

        return self.signal in ACTIONABLE


def _utc_now_z() -> str:
    """UTC timestamp with an explicit Z suffix, per the auditability rule."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def simple_moving_average(closes: Sequence[float], window: int) -> float:
    """Mean of the last `window` closes. Raises if there are not enough."""

    if window <= 0:
        raise ValueError(f"window must be positive, got {window}")
    if len(closes) < window:
        raise ValueError(
            f"need at least {window} closes for a {window}-period average, "
            f"got {len(closes)}"
        )
    return sum(closes[-window:]) / window


def _extract_closes(bars: Sequence[BarLike]) -> list[float] | None:
    """Return closes as floats, or None if any bar is unusable.

    Rejects None, non-numeric, NaN, infinity, and non-positive prices. A single
    bad bar poisons the whole average, so this refuses rather than skipping the
    offending bar and silently computing over a shorter window.
    """

    closes: list[float] = []
    for bar in bars:
        raw = getattr(bar, "close", None)
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        closes.append(value)
    return closes


def _is_ascending(bars: Sequence[BarLike]) -> bool:
    """True if timestamps are strictly increasing.

    BarsClient already sorts, but an unsorted list would compute the average
    over the wrong window and return a confident wrong answer -- exactly the
    silent-wrongness this project exists to avoid.
    """

    stamps: list[Any] = []
    for bar in bars:
        stamp = getattr(bar, "timestamp", None)
        if stamp is None:
            return False
        stamps.append(stamp)
    return all(earlier < later for earlier, later in zip(stamps, stamps[1:]))


def bar_day(bar: BarLike) -> date | None:
    """The calendar day a bar belongs to, or None if its stamp is unusable.

    Public because the daily cycle needs exactly this notion of "which day is
    this bar" when it trims in-progress bars, and a second implementation of
    it elsewhere would be free to drift from the one the rule uses.
    """

    stamp = getattr(bar, "timestamp", None)
    if isinstance(stamp, datetime):
        return stamp.date()
    if isinstance(stamp, date):
        return stamp
    return None


def _skip(
    symbol: str,
    signal: Signal,
    bars: Sequence[BarLike],
    window: int,
    day: date | None = None,
) -> CrossSignal:
    return CrossSignal(
        symbol=symbol,
        day=day,
        signal=signal,
        reason_code=signal,
        close=None,
        sma=None,
        prior_close=None,
        prior_sma=None,
        bars_available=len(bars),
        window=window,
        evaluated_at=_utc_now_z(),
    )


def detect_cross(
    symbol: str, bars: Sequence[BarLike], window: int = DEFAULT_WINDOW
) -> CrossSignal:
    """Classify the most recent bar as a cross up, cross down, or neither.

    `bars` must be ascending by timestamp, oldest first -- the order
    `BarsClient.get_daily_bars` returns.
    """

    if window <= 0:
        raise ValueError(f"window must be positive, got {window}")

    required = window + 1
    if len(bars) < required:
        logger.info(
            "%s: %d bars available, %d required for a %d-day cross",
            symbol,
            len(bars),
            required,
            window,
        )
        return _skip(symbol, "INSUFFICIENT_HISTORY", bars, window)

    if not _is_ascending(bars):
        logger.warning("%s: bars are not strictly ascending by timestamp", symbol)
        return _skip(symbol, "UNSORTED_BARS", bars, window)

    closes = _extract_closes(bars)
    if closes is None:
        logger.warning("%s: at least one bar has an unusable close price", symbol)
        return _skip(symbol, "INVALID_CLOSE", bars, window, day=bar_day(bars[-1]))

    close = closes[-1]
    prior_close = closes[-2]
    sma = simple_moving_average(closes, window)
    prior_sma = simple_moving_average(closes[:-1], window)

    if prior_close <= prior_sma and close > sma:
        signal: Signal = "CROSS_UP"
    elif prior_close >= prior_sma and close < sma:
        signal = "CROSS_DOWN"
    else:
        signal = "NO_CROSS"

    return CrossSignal(
        symbol=symbol,
        day=bar_day(bars[-1]),
        signal=signal,
        reason_code=signal,
        close=close,
        sma=sma,
        prior_close=prior_close,
        prior_sma=prior_sma,
        bars_available=len(bars),
        window=window,
        evaluated_at=_utc_now_z(),
    )


def detect_crosses(
    bars_by_symbol: Mapping[str, Sequence[BarLike]], window: int = DEFAULT_WINDOW
) -> list[CrossSignal]:
    """Classify every symbol, sorted by symbol. Every input gets exactly one row.

    Symbols that could not be evaluated are present with their skip reason
    rather than dropped, so the output row count always matches the input.
    """

    return [
        detect_cross(symbol, bars_by_symbol[symbol], window)
        for symbol in sorted(bars_by_symbol)
    ]
