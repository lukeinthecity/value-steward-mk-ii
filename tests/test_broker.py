"""Tests for read-only account state. No network -- a fake stands in for Alpaca.

Several tests below feed *strings* rather than floats on purpose: that is what
Alpaca actually returns, per docs/API_MENU.md, and hand-writing floats here
would be exactly the fixture-vs-production mismatch that VS1_MECHANISM_NOTES.md
identifies as the root cause of four separate defects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from vs2.data.broker import BrokerClient, Holding, MissingAccountFieldError


@dataclass
class FakeAccount:
    equity: str | None
    # Defaulted so the equity-focused cases below stay about equity. The
    # cash-specific cases set it explicitly.
    cash: str | None = "0"


@dataclass
class FakePosition:
    symbol: str
    qty: str
    market_value: str | None
    avg_entry_price: str


class FakeBrokerSource:
    def __init__(
        self, account: FakeAccount, positions: Any = None
    ) -> None:
        self._account = account
        self._positions = positions if positions is not None else []

    def get_account(self) -> FakeAccount:
        return self._account

    def get_all_positions(self) -> Any:
        return self._positions


def test_equity_parses_alpacas_string_representation() -> None:
    client = BrokerClient(FakeBrokerSource(FakeAccount(equity="100023.49")))
    assert client.get_equity() == pytest.approx(100023.49)


def test_missing_equity_raises_rather_than_defaulting_to_zero() -> None:
    # Silently returning 0.0 would size every subsequent order to nothing.
    client = BrokerClient(FakeBrokerSource(FakeAccount(equity=None)))
    with pytest.raises(MissingAccountFieldError, match="equity"):
        client.get_equity()


def test_empty_string_equity_also_raises() -> None:
    client = BrokerClient(FakeBrokerSource(FakeAccount(equity="")))
    with pytest.raises(MissingAccountFieldError, match="equity"):
        client.get_equity()


def test_non_numeric_equity_raises_with_the_offending_value() -> None:
    client = BrokerClient(FakeBrokerSource(FakeAccount(equity="N/A")))
    with pytest.raises(MissingAccountFieldError, match="N/A"):
        client.get_equity()


def test_holdings_convert_strings_and_sort_by_symbol() -> None:
    source = FakeBrokerSource(
        FakeAccount(equity="1000"),
        [
            FakePosition("NVDA", "3", "665.99", "221.99"),
            FakePosition("AAPL", "10.5", "3279.57", "312.34"),
        ],
    )
    holdings = BrokerClient(source).get_holdings()

    assert [h.symbol for h in holdings] == ["AAPL", "NVDA"]
    assert holdings[0] == Holding(
        symbol="AAPL",
        qty=pytest.approx(10.5),
        market_value=pytest.approx(3279.57),
        avg_entry_price=pytest.approx(312.34),
    )


def test_flat_account_returns_empty_list_not_none() -> None:
    client = BrokerClient(FakeBrokerSource(FakeAccount(equity="100000"), []))
    assert client.get_holdings() == []
    assert client.held_symbols() == set()


def test_missing_market_value_names_the_symbol_in_the_error() -> None:
    source = FakeBrokerSource(
        FakeAccount(equity="1000"),
        [FakePosition("BRK.B", "1", None, "519.61")],
    )
    with pytest.raises(MissingAccountFieldError, match="BRK.B.market_value"):
        BrokerClient(source).get_holdings()


def test_held_symbols_returns_a_set_of_symbols() -> None:
    source = FakeBrokerSource(
        FakeAccount(equity="1000"),
        [
            FakePosition("AAPL", "1", "312.34", "312.34"),
            FakePosition("BRK.B", "1", "519.61", "519.61"),
        ],
    )
    assert BrokerClient(source).held_symbols() == {"AAPL", "BRK.B"}


def test_unexpected_positions_shape_yields_no_holdings() -> None:
    source = FakeBrokerSource(FakeAccount(equity="1000"), object())
    assert BrokerClient(source).get_holdings() == []


def test_fractional_quantities_survive() -> None:
    # value-steward traded fractional shares; qty arrives as e.g. "0.017099767".
    source = FakeBrokerSource(
        FakeAccount(equity="1000"),
        [FakePosition("KCCA", "0.017099767", "0.29", "17.18")],
    )
    holdings = BrokerClient(source).get_holdings()
    assert holdings[0].qty == pytest.approx(0.017099767)


# --- cash, the ceiling on what a day's buys can spend -------------------------


def test_account_state_reads_equity_and_cash_in_one_call() -> None:
    source = FakeBrokerSource(FakeAccount(equity="100023.49", cash="30000.00"))
    state = BrokerClient(source).get_account_state()

    assert state.equity == pytest.approx(100023.49)
    assert state.cash == pytest.approx(30000.00)


def test_missing_cash_raises_rather_than_defaulting_to_zero() -> None:
    """Silently reading absent cash as zero would decline every buy and look
    exactly like a quiet market -- the same reasoning as equity."""

    source = FakeBrokerSource(FakeAccount(equity="100000", cash=None))
    with pytest.raises(MissingAccountFieldError, match="'cash'"):
        BrokerClient(source).get_account_state()


def test_negative_cash_is_reported_faithfully_not_clamped() -> None:
    """A margin debit is a real state. Clamping here would hide it; the
    decision layer is where it stops being spending power."""

    source = FakeBrokerSource(FakeAccount(equity="100000", cash="-5000"))
    assert BrokerClient(source).get_account_state().cash == pytest.approx(-5000.0)
