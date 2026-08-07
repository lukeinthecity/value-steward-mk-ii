"""Tests for crossover detection.

Many tests use a deliberately small window (3) so the moving average is
arithmetic a reader can verify in their head -- the same standard DESIGN.md
sets for the parameters themselves. The 50-day boundary cases are tested
separately, since off-by-one on the history requirement is the likeliest real
bug and would silently skip every symbol or read one bar too few.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from vs2.core.crossover import (
    DEFAULT_WINDOW,
    CrossSignal,
    detect_cross,
    detect_crosses,
    simple_moving_average,
)


@dataclass
class FakeBar:
    timestamp: datetime
    close: float | None


def bars(closes: list[float | None], start_day: int = 1) -> list[FakeBar]:
    """Ascending daily bars from the given closes, oldest first."""

    base = datetime(2026, 1, start_day, tzinfo=timezone.utc)
    return [
        FakeBar(timestamp=base + timedelta(days=i), close=c)
        for i, c in enumerate(closes)
    ]


# --- simple_moving_average -------------------------------------------------


def test_average_is_the_mean_of_the_last_window_closes() -> None:
    # last 3 of [1,2,3,10,20,30] are 10,20,30 -> 20
    assert simple_moving_average([1, 2, 3, 10, 20, 30], 3) == pytest.approx(20.0)


def test_average_uses_exactly_the_window_not_the_whole_series() -> None:
    assert simple_moving_average([100, 1, 1, 1], 3) == pytest.approx(1.0)


def test_average_rejects_too_few_closes() -> None:
    with pytest.raises(ValueError, match="need at least 3"):
        simple_moving_average([1, 2], 3)


def test_average_rejects_nonpositive_window() -> None:
    with pytest.raises(ValueError, match="window must be positive"):
        simple_moving_average([1, 2, 3], 0)


# --- the cross itself, hand-checkable --------------------------------------


def test_cross_up_when_close_moves_from_below_to_above() -> None:
    # window=3. prior sma over [10,10,10]=10, prior close 10 -> 10 <= 10.
    # current sma over [10,10,16]=12, current close 16 -> 16 > 12. Cross up.
    signal = detect_cross("TEST", bars([10, 10, 10, 16]), window=3)

    assert signal.signal == "CROSS_UP"
    assert signal.reason_code == "CROSS_UP"
    assert signal.is_actionable is True
    assert signal.close == pytest.approx(16.0)
    assert signal.sma == pytest.approx(12.0)
    assert signal.prior_close == pytest.approx(10.0)
    assert signal.prior_sma == pytest.approx(10.0)


def test_cross_down_when_close_moves_from_above_to_below() -> None:
    # prior sma over [10,10,10]=10, prior close 10 -> 10 >= 10.
    # current sma over [10,10,4]=8, current close 4 -> 4 < 8. Cross down.
    signal = detect_cross("TEST", bars([10, 10, 10, 4]), window=3)

    assert signal.signal == "CROSS_DOWN"
    assert signal.is_actionable is True
    assert signal.close == pytest.approx(4.0)
    assert signal.sma == pytest.approx(8.0)


def test_no_cross_when_price_stays_above_the_average() -> None:
    signal = detect_cross("TEST", bars([10, 11, 12, 13]), window=3)

    assert signal.signal == "NO_CROSS"
    assert signal.is_actionable is False
    # Values are still recorded, so a no-cross row is auditable too.
    assert signal.close == pytest.approx(13.0)
    assert signal.sma is not None


def test_no_cross_when_price_stays_below_the_average() -> None:
    signal = detect_cross("TEST", bars([13, 12, 11, 10]), window=3)
    assert signal.signal == "NO_CROSS"


def test_rising_but_still_below_the_average_is_not_a_cross() -> None:
    # Rising sharply, but 11 < sma over [30,20,11]=20.33. Not a cross.
    signal = detect_cross("TEST", bars([40, 30, 20, 11]), window=3)
    assert signal.signal == "NO_CROSS"


def test_prior_close_exactly_on_the_average_counts_as_below() -> None:
    # The documented `<=` choice: sitting exactly on the line then rising is a
    # cross up, so a symbol on its average is never stuck without a signal.
    signal = detect_cross("TEST", bars([10, 10, 10, 16]), window=3)
    assert signal.prior_close == signal.prior_sma
    assert signal.signal == "CROSS_UP"


def test_up_and_down_are_mutually_exclusive_across_many_series() -> None:
    # Both require opposite relations between close and sma, so no input
    # should ever satisfy both. Guards against a future refactor breaking it.
    series = [
        [10, 10, 10, 16],
        [10, 10, 10, 4],
        [1, 5, 3, 9],
        [9, 3, 5, 1],
        [7, 7, 7, 7],
        [2, 8, 2, 8],
    ]
    for closes in series:
        signal = detect_cross("TEST", bars(closes), window=3)
        assert signal.signal in {"CROSS_UP", "CROSS_DOWN", "NO_CROSS"}


def test_flat_series_never_crosses() -> None:
    signal = detect_cross("TEST", bars([5, 5, 5, 5]), window=3)
    assert signal.signal == "NO_CROSS"


# --- history requirement ---------------------------------------------------


def test_window_plus_one_bars_is_enough() -> None:
    signal = detect_cross("TEST", bars([10] * 3 + [16]), window=3)
    assert signal.signal != "INSUFFICIENT_HISTORY"
    assert signal.bars_available == 4


def test_exactly_window_bars_is_not_enough() -> None:
    # The average is computable for today but not for yesterday, so there is
    # nothing to compare against and no cross can be established.
    signal = detect_cross("TEST", bars([10, 10, 16]), window=3)

    assert signal.signal == "INSUFFICIENT_HISTORY"
    assert signal.reason_code == "INSUFFICIENT_HISTORY"
    assert signal.is_actionable is False
    assert signal.bars_available == 3


def test_fifty_day_window_needs_fifty_one_bars() -> None:
    # The real configured window: 50 bars is a skip, 51 evaluates.
    assert (
        detect_cross("TEST", bars([10] * 50)).signal == "INSUFFICIENT_HISTORY"
    )
    assert detect_cross("TEST", bars([10] * 51)).signal != "INSUFFICIENT_HISTORY"


def test_default_window_is_fifty() -> None:
    assert DEFAULT_WINDOW == 50


def test_real_fifty_day_cross_up() -> None:
    # 50 bars at 10.0, then a jump. sma over the last 50 rises only slightly,
    # so a large jump clears it.
    signal = detect_cross("TEST", bars([10.0] * 50 + [50.0]))
    assert signal.signal == "CROSS_UP"


def test_insufficient_history_is_not_reported_as_no_cross() -> None:
    # These are different facts and must not share a reason code -- a skipped
    # symbol looks identical to a genuine non-event otherwise.
    skip = detect_cross("TEST", bars([10, 10]), window=3)
    genuine = detect_cross("TEST", bars([5, 5, 5, 5]), window=3)

    assert skip.reason_code != genuine.reason_code


def test_empty_bars_is_a_skip_not_a_crash() -> None:
    signal = detect_cross("TEST", [], window=3)
    assert signal.signal == "INSUFFICIENT_HISTORY"
    assert signal.bars_available == 0


def test_nonpositive_window_raises() -> None:
    with pytest.raises(ValueError, match="window must be positive"):
        detect_cross("TEST", bars([1, 2, 3, 4]), window=0)


# --- malformed input -------------------------------------------------------


def test_unsorted_bars_are_refused_rather_than_computed() -> None:
    out_of_order = bars([10, 10, 10, 16])
    out_of_order[1], out_of_order[2] = out_of_order[2], out_of_order[1]

    signal = detect_cross("TEST", out_of_order, window=3)

    assert signal.signal == "UNSORTED_BARS"
    assert signal.is_actionable is False
    assert signal.sma is None


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), 0.0, -5.0])
def test_unusable_close_is_refused(bad: float | None) -> None:
    signal = detect_cross("TEST", bars([10, 10, 10, bad]), window=3)

    assert signal.signal == "INVALID_CLOSE"
    assert signal.is_actionable is False
    assert signal.sma is None


def test_one_bad_bar_anywhere_invalidates_the_window() -> None:
    # A bad bar in the middle would otherwise shorten the averaging window
    # silently and produce a confident wrong answer.
    signal = detect_cross("TEST", bars([10, None, 10, 16]), window=3)
    assert signal.signal == "INVALID_CLOSE"


# --- batch behavior --------------------------------------------------------


def test_every_input_symbol_gets_exactly_one_row() -> None:
    signals = detect_crosses(
        {
            "AAA": bars([10, 10, 10, 16]),
            "BBB": bars([10, 10]),  # too short
            "CCC": bars([5, 5, 5, 5]),
        },
        window=3,
    )

    assert len(signals) == 3
    assert [s.symbol for s in signals] == ["AAA", "BBB", "CCC"]
    assert {s.symbol: s.signal for s in signals} == {
        "AAA": "CROSS_UP",
        "BBB": "INSUFFICIENT_HISTORY",
        "CCC": "NO_CROSS",
    }


def test_skipped_symbols_are_not_dropped_from_the_output() -> None:
    signals = detect_crosses({"BAD": bars([1, 2])}, window=3)
    assert len(signals) == 1
    assert signals[0].signal == "INSUFFICIENT_HISTORY"


def test_empty_input_gives_empty_output() -> None:
    assert detect_crosses({}, window=3) == []


# --- audit fields ----------------------------------------------------------


def test_records_the_day_of_the_evaluated_bar() -> None:
    signal = detect_cross("TEST", bars([10, 10, 10, 16], start_day=1), window=3)
    assert signal.day == date(2026, 1, 4)


def test_timestamp_is_utc_with_explicit_z() -> None:
    signal = detect_cross("TEST", bars([10, 10, 10, 16]), window=3)
    assert signal.evaluated_at.endswith("Z")
    assert "+00:00" not in signal.evaluated_at


def test_window_is_recorded_on_the_row() -> None:
    assert detect_cross("TEST", bars([10, 10, 10, 16]), window=3).window == 3


def test_signal_is_immutable() -> None:
    signal = detect_cross("TEST", bars([10, 10, 10, 16]), window=3)
    with pytest.raises(AttributeError):
        signal.signal = "CROSS_DOWN"  # type: ignore[misc]


def test_is_actionable_only_for_real_crosses() -> None:
    assert detect_cross("T", bars([10, 10, 10, 16]), window=3).is_actionable
    assert detect_cross("T", bars([10, 10, 10, 4]), window=3).is_actionable
    assert not detect_cross("T", bars([5, 5, 5, 5]), window=3).is_actionable
    assert not detect_cross("T", bars([1, 2]), window=3).is_actionable


def test_cross_signal_carries_the_full_audit_row() -> None:
    # DESIGN.md's measurement spec: symbol, date, action, reason code, price,
    # sma, and the prior day's values that established the cross.
    signal = detect_cross("TEST", bars([10, 10, 10, 16]), window=3)
    for field in (
        "symbol",
        "day",
        "signal",
        "reason_code",
        "close",
        "sma",
        "prior_close",
        "prior_sma",
        "bars_available",
        "window",
        "evaluated_at",
    ):
        assert getattr(signal, field) is not None, field
    assert isinstance(signal, CrossSignal)
