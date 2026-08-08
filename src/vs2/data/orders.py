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

`submit` deliberately does NOT use retry_alpaca, unlike every read in this
codebase. A GET is safe to retry blindly -- asking twice is harmless. A submit
is not: a transient-looking error (429/500/502) does not mean the order was
rejected, only that the response was lost, and retrying an order whose first
attempt actually landed places a second, duplicate order. One failed attempt
stops and is reported rather than guessed at -- see `submit_all`, which
carries this same principle into a batch: one order failing does not stop the
others from being tried, and both outcomes are recorded rather than only the
successes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from vs2.core.decision import Decision

logger = logging.getLogger(__name__)


class _OrderSource(Protocol):
    def submit_order(self, order_data: MarketOrderRequest) -> Any: ...


@dataclass(frozen=True)
class SubmissionResult:
    """The outcome of attempting one order -- never both order and error."""

    decision: Decision
    order: Any | None
    error: str | None

    @property
    def succeeded(self) -> bool:
        return self.error is None


def client_order_id(decision: Decision) -> str | None:
    """A stable id derived from the decision itself, not from when it is sent.

    Two properties follow from it being deterministic. The broker enforces
    uniqueness on this field, so a resubmission of an order that actually
    landed is rejected *by Alpaca* rather than silently duplicated -- which is
    what makes `--force` safe to use after a partial failure: the orders that
    already went through cannot go through twice, while the ones that genuinely
    failed never registered an id and retry cleanly. And it gives
    reconciliation a key to look a fill up by, without which matching a fill
    back to the decision that caused it means guessing from symbol and
    timestamp.

    Returns None for a decision with no day, which cannot be keyed; the caller
    submits without one rather than inventing a date.
    """

    if decision.day is None:
        return None
    return f"vs2-{decision.day.isoformat()}-{decision.symbol}-{decision.action}"


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
            client_order_id=client_order_id(decision),
        )
    if decision.action == "SELL":
        if decision.qty is None or decision.qty <= 0:
            raise ValueError(f"{decision.symbol}: SELL decision has no positive qty")
        return MarketOrderRequest(
            symbol=decision.symbol,
            qty=decision.qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id(decision),
        )
    raise ValueError(
        f"{decision.symbol}: cannot submit a {decision.action} decision -- "
        "only BUY and SELL are orders"
    )


class OrderClient:
    def __init__(self, trading_client: _OrderSource) -> None:
        self._trading_client = trading_client

    def submit(self, decision: Decision) -> Any:
        """Submit one order. Not retried -- see the module docstring. Raises
        on failure; callers that need to keep going after one failure should
        use `submit_all`, not call this in a loop themselves."""

        request = build_order_request(decision)
        logger.info(
            "submitting %s %s notional=%s qty=%s",
            decision.action,
            decision.symbol,
            decision.notional,
            decision.qty,
        )
        return self._trading_client.submit_order(request)

    def submit_all(self, decisions: list[Decision]) -> list[SubmissionResult]:
        """Attempt every order-bearing decision, sells first, and keep going
        even if one fails.

        Selling before buying frees paper buying power sooner rather than
        later; it is a sensible default, not a settlement guarantee, since
        fills are not instantaneous even for market orders.

        One order's failure does not stop the rest from being attempted --
        an exception here would otherwise abort every order still queued
        behind it, with no record of which ones were never tried. Every
        decision gets a SubmissionResult, success or failure, so the caller
        (and the log) can tell "placed" apart from "never attempted" apart
        from "attempted and rejected."
        """

        orders = [d for d in decisions if d.is_order]
        orders.sort(key=lambda d: 0 if d.action == "SELL" else 1)

        results: list[SubmissionResult] = []
        for decision in orders:
            try:
                order = self.submit(decision)
                results.append(SubmissionResult(decision, order, None))
            except Exception as exc:
                # Deliberately broad: every failure mode must produce a
                # recorded result, not a crash that aborts the remaining
                # orders in this batch.
                logger.error(
                    "failed to submit %s %s: %s", decision.action, decision.symbol, exc
                )
                results.append(SubmissionResult(decision, None, str(exc)))
        return results
