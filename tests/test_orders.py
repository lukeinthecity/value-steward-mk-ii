"""Tests for order submission. A fake stands in for Alpaca's TradingClient --
these tests must never be able to place a real order, on principle as much as
for speed."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from alpaca.trading.enums import OrderSide

from vs2.core.decision import Decision
from vs2.data.orders import OrderClient, build_order_request


def decision(
    symbol: str,
    action: str,
    notional: float | None = None,
    qty: float | None = None,
) -> Decision:
    return Decision(
        symbol=symbol,
        day=date(2026, 8, 10),
        action=action,  # type: ignore[arg-type]
        reason_code=action,
        close=100.0,
        sma=90.0,
        prior_close=89.0,
        prior_sma=90.0,
        notional=notional,
        qty=qty,
    )


class RecordingFakeTradingClient:
    def __init__(self) -> None:
        self.submitted: list[Any] = []

    def submit_order(self, order_data: Any) -> dict:
        self.submitted.append(order_data)
        return {"id": f"fake-{len(self.submitted)}", "symbol": order_data.symbol}


# --- build_order_request: pure mapping ---------------------------------------


def test_buy_becomes_a_notional_market_order() -> None:
    request = build_order_request(decision("AAPL", "BUY", notional=5000.0))
    assert request.symbol == "AAPL"
    assert request.notional == "5000.0" or float(request.notional) == 5000.0
    assert request.qty is None
    assert request.side == OrderSide.BUY


def test_sell_becomes_a_qty_market_order() -> None:
    request = build_order_request(decision("AAPL", "SELL", qty=12.5))
    assert request.symbol == "AAPL"
    assert float(request.qty) == pytest.approx(12.5)
    assert request.notional is None
    assert request.side == OrderSide.SELL


def test_notional_is_rounded_to_two_decimals() -> None:
    request = build_order_request(decision("AAPL", "BUY", notional=3333.33333))
    assert float(request.notional) == pytest.approx(3333.33)


@pytest.mark.parametrize("action", ["HOLD", "NO_ACTION", "BUY_DECLINED_FULL"])
def test_non_order_actions_are_refused(action: str) -> None:
    with pytest.raises(ValueError, match="cannot submit"):
        build_order_request(decision("AAPL", action))


def test_buy_without_notional_is_refused() -> None:
    with pytest.raises(ValueError, match="no positive notional"):
        build_order_request(decision("AAPL", "BUY", notional=None))


def test_buy_with_zero_notional_is_refused() -> None:
    with pytest.raises(ValueError, match="no positive notional"):
        build_order_request(decision("AAPL", "BUY", notional=0.0))


def test_sell_without_qty_is_refused() -> None:
    with pytest.raises(ValueError, match="no positive qty"):
        build_order_request(decision("AAPL", "SELL", qty=None))


def test_sell_with_zero_qty_is_refused() -> None:
    with pytest.raises(ValueError, match="no positive qty"):
        build_order_request(decision("AAPL", "SELL", qty=0.0))


# --- OrderClient: submission via a fake, never the real API -----------------


def test_submit_sends_the_built_request_to_the_trading_client() -> None:
    fake = RecordingFakeTradingClient()
    OrderClient(fake).submit(decision("AAPL", "BUY", notional=5000.0))

    assert len(fake.submitted) == 1
    assert fake.submitted[0].symbol == "AAPL"


def test_submit_refuses_a_hold_decision_before_touching_the_client() -> None:
    fake = RecordingFakeTradingClient()
    with pytest.raises(ValueError, match="cannot submit"):
        OrderClient(fake).submit(decision("AAPL", "HOLD"))
    assert fake.submitted == []


def test_submit_all_only_sends_order_bearing_decisions() -> None:
    fake = RecordingFakeTradingClient()
    decisions = [
        decision("HOLD1", "HOLD"),
        decision("BUY1", "BUY", notional=1000.0),
        decision("DECLINED1", "BUY_DECLINED_FULL"),
        decision("NOACTION1", "NO_ACTION"),
    ]
    OrderClient(fake).submit_all(decisions)

    assert [o.symbol for o in fake.submitted] == ["BUY1"]


def test_submit_all_places_sells_before_buys() -> None:
    fake = RecordingFakeTradingClient()
    decisions = [
        decision("BUY1", "BUY", notional=1000.0),
        decision("SELL1", "SELL", qty=5.0),
        decision("BUY2", "BUY", notional=2000.0),
        decision("SELL2", "SELL", qty=3.0),
    ]
    OrderClient(fake).submit_all(decisions)

    sides = [o.side for o in fake.submitted]
    assert sides == [OrderSide.SELL, OrderSide.SELL, OrderSide.BUY, OrderSide.BUY]


def test_submit_all_with_no_orders_touches_the_client_zero_times() -> None:
    fake = RecordingFakeTradingClient()
    OrderClient(fake).submit_all([decision("H", "HOLD"), decision("N", "NO_ACTION")])
    assert fake.submitted == []


def test_submit_all_returns_one_result_per_order() -> None:
    fake = RecordingFakeTradingClient()
    results = OrderClient(fake).submit_all(
        [decision("A", "BUY", notional=100.0), decision("B", "SELL", qty=1.0)]
    )
    assert len(results) == 2


def test_submit_is_wrapped_in_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vs2.data.retry.time.sleep", lambda _s: None)

    class FlakyOnce:
        def __init__(self) -> None:
            self.calls = 0

        def submit_order(self, order_data: Any) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("429 too many requests")
            return {"id": "ok"}

    flaky = FlakyOnce()
    OrderClient(flaky).submit(decision("AAPL", "BUY", notional=1000.0))
    assert flaky.calls == 2
