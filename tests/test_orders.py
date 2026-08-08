"""Tests for order submission. A fake stands in for Alpaca's TradingClient --
these tests must never be able to place a real order, on principle as much as
for speed."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from alpaca.trading.enums import OrderSide

from vs2.core.decision import Decision
from vs2.data.orders import (
    OrderClient,
    SubmissionResult,
    build_order_request,
    client_order_id,
)


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


# --- submit does NOT retry: a write is not safe to repeat blindly -----------


def test_submit_does_not_retry_a_transient_looking_error() -> None:
    # A 429/500/502 on a write does not mean the order was rejected -- it may
    # mean it landed and the response was lost. Retrying could duplicate it,
    # so submit() must raise on the first failure rather than try again.
    class FlakyOnce:
        def __init__(self) -> None:
            self.calls = 0

        def submit_order(self, order_data: Any) -> dict:
            self.calls += 1
            raise RuntimeError("429 too many requests")

    flaky = FlakyOnce()
    with pytest.raises(RuntimeError, match="429"):
        OrderClient(flaky).submit(decision("AAPL", "BUY", notional=1000.0))
    assert flaky.calls == 1


# --- submit_all: one failure does not stop the rest -------------------------


class SelectivelyFailingTradingClient:
    """Fails submit_order for exactly the given symbols; succeeds for the rest."""

    def __init__(self, fail_symbols: set[str]) -> None:
        self._fail_symbols = fail_symbols
        self.submitted: list[Any] = []

    def submit_order(self, order_data: Any) -> dict:
        self.submitted.append(order_data)
        if order_data.symbol in self._fail_symbols:
            raise RuntimeError(f"rejected: {order_data.symbol}")
        return {"id": f"ok-{order_data.symbol}", "symbol": order_data.symbol}


def test_submit_all_attempts_every_order_even_if_one_fails() -> None:
    fake = SelectivelyFailingTradingClient(fail_symbols={"BAD"})
    decisions = [
        decision("GOOD1", "BUY", notional=1000.0),
        decision("BAD", "BUY", notional=2000.0),
        decision("GOOD2", "BUY", notional=3000.0),
    ]

    results = OrderClient(fake).submit_all(decisions)

    # All three were attempted -- BAD failing did not stop GOOD2 from being tried.
    assert [o.symbol for o in fake.submitted] == ["GOOD1", "BAD", "GOOD2"]
    assert len(results) == 3


def test_submit_all_marks_success_and_failure_correctly() -> None:
    fake = SelectivelyFailingTradingClient(fail_symbols={"BAD"})
    decisions = [decision("GOOD1", "BUY", notional=1000.0), decision("BAD", "BUY", notional=2000.0)]

    results = OrderClient(fake).submit_all(decisions)
    by_symbol = {r.decision.symbol: r for r in results}

    good = by_symbol["GOOD1"]
    assert good.succeeded is True
    assert good.error is None
    assert good.order is not None

    bad = by_symbol["BAD"]
    assert bad.succeeded is False
    assert bad.error is not None
    assert "BAD" in bad.error
    assert bad.order is None


def test_submit_all_does_not_raise_when_an_order_fails() -> None:
    # The whole point: a failure is a recorded result, not an exception that
    # propagates and takes down the caller (and every order still queued).
    fake = SelectivelyFailingTradingClient(fail_symbols={"BAD"})
    results = OrderClient(fake).submit_all([decision("BAD", "SELL", qty=1.0)])
    assert results[0].succeeded is False


def test_submission_result_succeeded_reflects_whether_error_is_set() -> None:
    ok = SubmissionResult(decision("A", "BUY", notional=1.0), order={"id": "x"}, error=None)
    bad = SubmissionResult(decision("A", "BUY", notional=1.0), order=None, error="boom")
    assert ok.succeeded is True
    assert bad.succeeded is False


# --- deterministic client order ids -------------------------------------------
#
# Two properties hang off this being derived from the decision rather than from
# the clock: the broker rejects a resubmission of an order that actually
# landed, and reconciliation has a key to look the fill up by.


def test_client_order_id_is_derived_from_the_decision() -> None:
    assert (
        client_order_id(decision("AAPL", "BUY", notional=5000.0))
        == "vs2-2026-08-10-AAPL-BUY"
    )


def test_buy_and_sell_of_one_symbol_on_one_day_do_not_collide() -> None:
    assert client_order_id(decision("AAPL", "BUY", notional=5000.0)) != client_order_id(
        decision("AAPL", "SELL", qty=10.0)
    )


def test_the_same_decision_always_yields_the_same_id() -> None:
    """What makes a duplicate submission the broker's problem to reject rather
    than ours to avoid perfectly."""

    assert client_order_id(decision("AAPL", "BUY", notional=5000.0)) == client_order_id(
        decision("AAPL", "BUY", notional=5000.0)
    )


def test_a_decision_with_no_day_cannot_be_keyed() -> None:
    dateless = Decision(
        symbol="AAPL",
        day=None,
        action="BUY",
        reason_code="CROSS_UP",
        close=100.0,
        sma=90.0,
        prior_close=89.0,
        prior_sma=90.0,
        notional=5000.0,
        qty=None,
    )

    assert client_order_id(dateless) is None


def test_the_id_reaches_the_built_request() -> None:
    request = build_order_request(decision("AAPL", "BUY", notional=5000.0))
    assert request.client_order_id == "vs2-2026-08-10-AAPL-BUY"
