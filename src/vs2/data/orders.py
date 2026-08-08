"""Order submission -- the only module in this codebase that can move money.

Every other module in vs2.data (bars, broker, market_calendar) is read-only by
construction; this is deliberately the single file where that stops being
true, so anyone auditing "what code here can place an order" only has to read
this one file rather than trust a convention.

Market orders only, per DESIGN.md's "Why market orders" -- fill certainty over
price, because an unfilled order is a decision that never becomes data, and
VS1's execution layer silently discarding most of its own signals was a
recurring failure this design exists to avoid.

BUY orders are notional (dollar-denominated), matching the equal-weight sizing
`build_decisions` computes. SELL orders use the held quantity directly rather
than a notional amount, so a position is closed exactly rather than leaving a
fractional remainder from price drift since entry.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from vs2.core.decision import Decision
from vs2.data.retry import retry_alpaca

logger = logging.getLogger(__name__)


class _OrderSource(Protocol):
    def submit_order(self, order_data: MarketOrderRequest) -> Any: ...


def build_order_request(decision: Decision) -> MarketOrderRequest:
    """Translate one order Decision into an Alpaca request. Pure, no I/O --
    separated from submission so the mapping itself is directly testable."""

    if decision.action == "BUY":
        if decision.notional is None or decision.notional <= 0:
            raise ValueError(
                f"{decision.symbol}: BUY decision has no positive notional"
            )
        return MarketOrderRequest(
            symbol=decision.symbol,
            notional=round(decision.notional, 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
    if decision.action == "SELL":
        if decision.qty is None or decision.qty <= 0:
            raise ValueError(f"{decision.symbol}: SELL decision has no positive qty")
        return MarketOrderRequest(
            symbol=decision.symbol,
            qty=decision.qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
    raise ValueError(
        f"{decision.symbol}: cannot submit a {decision.action} decision -- "
        "only BUY and SELL are orders"
    )


class OrderClient:
    def __init__(self, trading_client: _OrderSource) -> None:
        self._trading_client = trading_client

    @retry_alpaca()
    def submit(self, decision: Decision) -> Any:
        request = build_order_request(decision)
        logger.info(
            "submitting %s %s notional=%s qty=%s",
            decision.action,
            decision.symbol,
            decision.notional,
            decision.qty,
        )
        return self._trading_client.submit_order(request)

    def submit_all(self, decisions: list[Decision]) -> list[Any]:
        """Submit every order-bearing decision, sells first.

        Selling before buying frees paper buying power sooner rather than
        later; it is a sensible default, not a settlement guarantee, since
        fills are not instantaneous even for market orders.
        """

        orders = [d for d in decisions if d.is_order]
        orders.sort(key=lambda d: 0 if d.action == "SELL" else 1)
        return [self.submit(d) for d in orders]
