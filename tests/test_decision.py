"""Tests for build_decisions. Pure function -- no network, no fakes needed
beyond plain data, since CrossSignal and Holding are already dataclasses."""

from __future__ import annotations

from datetime import date

import pytest

from vs2.core.crossover import CrossSignal
from vs2.core.decision import build_decisions
from vs2.data.broker import Holding

DAY = date(2026, 8, 10)


def sig(
    symbol: str,
    signal: str,
    close: float = 100.0,
    sma: float = 90.0,
    prior_close: float = 89.0,
    prior_sma: float = 90.0,
) -> CrossSignal:
    return CrossSignal(
        symbol=symbol,
        day=DAY,
        signal=signal,  # type: ignore[arg-type]
        reason_code=signal,
        close=close,
        sma=sma,
        prior_close=prior_close,
        prior_sma=prior_sma,
        bars_available=51,
        window=50,
        evaluated_at="2026-08-10T20:15:00Z",
    )


def holding(symbol: str, qty: float = 10.0) -> Holding:
    return Holding(symbol=symbol, qty=qty, market_value=1000.0, avg_entry_price=95.0)


# --- basic classification ---------------------------------------------------


def test_buy_when_cross_up_and_not_held_and_room() -> None:
    decisions = build_decisions(
        [sig("AAPL", "CROSS_UP")], {}, equity=100_000, slots=20, dollar_volume={}
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "BUY"
    assert d.reason_code == "CROSS_UP"
    assert d.notional == pytest.approx(5000.0)
    assert d.qty is None
    assert d.is_order is True


def test_sell_when_held_and_cross_down() -> None:
    decisions = build_decisions(
        [sig("AAPL", "CROSS_DOWN")],
        {"AAPL": holding("AAPL", qty=12.5)},
        equity=100_000,
        slots=20,
        dollar_volume={},
    )
    d = decisions[0]
    assert d.action == "SELL"
    assert d.qty == pytest.approx(12.5)
    assert d.notional is None
    assert d.is_order is True


def test_hold_when_held_and_no_cross() -> None:
    decisions = build_decisions(
        [sig("AAPL", "NO_CROSS")],
        {"AAPL": holding("AAPL")},
        equity=100_000,
        slots=20,
        dollar_volume={},
    )
    d = decisions[0]
    assert d.action == "HOLD"
    assert d.is_order is False


def test_no_action_when_not_held_and_no_cross() -> None:
    decisions = build_decisions(
        [sig("AAPL", "NO_CROSS")], {}, equity=100_000, slots=20, dollar_volume={}
    )
    assert decisions[0].action == "NO_ACTION"


def test_every_input_signal_produces_exactly_one_decision() -> None:
    signals = [sig("AAA", "CROSS_UP"), sig("BBB", "NO_CROSS"), sig("CCC", "CROSS_DOWN")]
    decisions = build_decisions(
        signals, {"CCC": holding("CCC")}, equity=100_000, slots=20, dollar_volume={}
    )
    assert len(decisions) == 3
    assert {d.symbol for d in decisions} == {"AAA", "BBB", "CCC"}


# --- capacity and the tiebreak ----------------------------------------------


def test_buy_declined_when_no_free_slots() -> None:
    held = {f"H{i}": holding(f"H{i}") for i in range(20)}
    signals = [sig(f"H{i}", "NO_CROSS") for i in range(20)] + [sig("NEW", "CROSS_UP")]
    decisions = build_decisions(signals, held, equity=100_000, slots=20, dollar_volume={})
    new_decision = next(d for d in decisions if d.symbol == "NEW")
    assert new_decision.action == "BUY_DECLINED_FULL"
    assert new_decision.is_order is False


def test_tiebreak_prefers_highest_dollar_volume() -> None:
    signals = [
        sig("LOW", "CROSS_UP"),
        sig("HIGH", "CROSS_UP"),
        sig("MID", "CROSS_UP"),
    ]
    dv = {"LOW": 1_000.0, "HIGH": 3_000.0, "MID": 2_000.0}
    decisions = build_decisions(signals, {}, equity=100_000, slots=2, dollar_volume=dv)

    by_symbol = {d.symbol: d.action for d in decisions}
    assert by_symbol["HIGH"] == "BUY"
    assert by_symbol["MID"] == "BUY"
    assert by_symbol["LOW"] == "BUY_DECLINED_FULL"


def test_missing_dollar_volume_tiebreaks_last_not_first() -> None:
    # A symbol this function has no volume data for must never win a slot
    # over one it does -- defaulting a KeyError to 0.0 achieves that, but the
    # behavior itself is worth pinning with a test.
    signals = [sig("KNOWN", "CROSS_UP"), sig("UNKNOWN", "CROSS_UP")]
    dv = {"KNOWN": 500.0}  # UNKNOWN absent entirely
    decisions = build_decisions(signals, {}, equity=100_000, slots=1, dollar_volume=dv)

    by_symbol = {d.symbol: d.action for d in decisions}
    assert by_symbol["KNOWN"] == "BUY"
    assert by_symbol["UNKNOWN"] == "BUY_DECLINED_FULL"


def test_sells_free_capacity_for_same_day_buys() -> None:
    held = {f"H{i}": holding(f"H{i}") for i in range(20)}
    signals = (
        [sig(f"H{i}", "NO_CROSS") for i in range(19)]
        + [sig("H19", "CROSS_DOWN")]
        + [sig("NEW", "CROSS_UP")]
    )
    decisions = build_decisions(signals, held, equity=100_000, slots=20, dollar_volume={})

    by_symbol = {d.symbol: d.action for d in decisions}
    assert by_symbol["H19"] == "SELL"
    assert by_symbol["NEW"] == "BUY"


def test_zero_free_slots_declines_every_candidate() -> None:
    held = {f"H{i}": holding(f"H{i}") for i in range(5)}
    signals = [sig(f"H{i}", "NO_CROSS") for i in range(5)] + [
        sig("A", "CROSS_UP"),
        sig("B", "CROSS_UP"),
    ]
    decisions = build_decisions(signals, held, equity=100_000, slots=5, dollar_volume={})
    assert all(
        d.action == "BUY_DECLINED_FULL" for d in decisions if d.symbol in ("A", "B")
    )


def test_more_held_than_slots_defensively_floors_free_at_zero() -> None:
    # Should not happen if this function always governed buys, but a manual
    # position or a lowered slot count must not produce negative capacity.
    held = {f"H{i}": holding(f"H{i}") for i in range(25)}
    signals = [sig(f"H{i}", "NO_CROSS") for i in range(25)] + [sig("NEW", "CROSS_UP")]
    decisions = build_decisions(signals, held, equity=100_000, slots=20, dollar_volume={})
    assert next(d for d in decisions if d.symbol == "NEW").action == "BUY_DECLINED_FULL"


# --- sizing ------------------------------------------------------------------


def test_equal_weight_sizing_divides_equity_by_slots() -> None:
    decisions = build_decisions(
        [sig("AAPL", "CROSS_UP")], {}, equity=50_000, slots=25, dollar_volume={}
    )
    assert decisions[0].notional == pytest.approx(2000.0)


def test_sell_uses_the_exact_held_quantity_not_a_computed_amount() -> None:
    decisions = build_decisions(
        [sig("AAPL", "CROSS_DOWN")],
        {"AAPL": holding("AAPL", qty=0.017099767)},
        equity=100_000,
        slots=20,
        dollar_volume={},
    )
    assert decisions[0].qty == pytest.approx(0.017099767)


# --- missing / unevaluable data ----------------------------------------------


@pytest.mark.parametrize(
    "signal", ["INSUFFICIENT_HISTORY", "UNSORTED_BARS", "INVALID_CLOSE"]
)
def test_held_symbol_with_unevaluable_signal_holds_rather_than_force_sells(
    signal: str,
) -> None:
    decisions = build_decisions(
        [sig("AAPL", signal)],
        {"AAPL": holding("AAPL")},
        equity=100_000,
        slots=20,
        dollar_volume={},
    )
    d = decisions[0]
    assert d.action == "HOLD"
    assert d.reason_code == signal  # the skip reason survives onto the row


def test_held_symbol_missing_from_signals_entirely_is_not_silently_dropped() -> None:
    # e.g. dropped from the universe, or its bar fetch failed outright.
    decisions = build_decisions(
        [sig("OTHER", "NO_CROSS")],
        {"GONE": holding("GONE")},
        equity=100_000,
        slots=20,
        dollar_volume={},
    )
    assert len(decisions) == 2
    gone = next(d for d in decisions if d.symbol == "GONE")
    assert gone.action == "HOLD"
    assert gone.reason_code == "NO_SIGNAL_DATA"
    assert gone.day is None
    assert gone.close is None


# --- validation ---------------------------------------------------------------


def test_raises_on_nonpositive_equity() -> None:
    with pytest.raises(ValueError, match="equity must be positive"):
        build_decisions([], {}, equity=0, slots=20, dollar_volume={})


def test_raises_on_nonpositive_slots() -> None:
    with pytest.raises(ValueError, match="slots must be positive"):
        build_decisions([], {}, equity=100_000, slots=0, dollar_volume={})


def test_decision_is_immutable() -> None:
    d = build_decisions([sig("AAPL", "CROSS_UP")], {}, 100_000, 20, {})[0]
    with pytest.raises(AttributeError):
        d.action = "SELL"  # type: ignore[misc]


def test_empty_signals_and_holdings_gives_empty_decisions() -> None:
    assert build_decisions([], {}, equity=100_000, slots=20, dollar_volume={}) == []
