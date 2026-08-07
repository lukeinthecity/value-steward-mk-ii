"""Read-only account state: equity and current holdings.

Order placement deliberately lives elsewhere. This module cannot move money,
which makes it safe to call from anywhere -- including tests and diagnostics.

Alpaca returns numeric account fields as *strings*, and several as Optional
(`equity`, `market_value`, `unrealized_pl` among them). Converting at this
boundary, with missing values raising rather than silently becoming 0.0, keeps
those two footguns out of every caller. Position sizing divides by equity, so
an equity of 0.0 would size every order to nothing and log nothing useful.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from vs2.data.retry import retry_alpaca

logger = logging.getLogger(__name__)


class MissingAccountFieldError(RuntimeError):
    """Raised when Alpaca omits a field the caller cannot sensibly default."""


@dataclass(frozen=True)
class Holding:
    """One open position, in our own types rather than the SDK's."""

    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float


def _required_float(value: Any, field: str) -> float:
    """Convert an Alpaca string field to float, refusing to invent a default."""

    if value is None or value == "":
        raise MissingAccountFieldError(
            f"Alpaca returned no value for {field!r}; refusing to substitute a "
            "default, because silently treating it as zero would corrupt sizing"
        )
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise MissingAccountFieldError(
            f"Alpaca returned a non-numeric {field!r}: {value!r}"
        ) from exc


class _BrokerSource(Protocol):
    def get_account(self) -> Any: ...
    def get_all_positions(self) -> Any: ...


class BrokerClient:
    def __init__(self, trading_client: _BrokerSource) -> None:
        self._trading_client = trading_client

    @retry_alpaca()
    def get_equity(self) -> float:
        """Total account value: cash plus the market value of every position."""

        account = self._trading_client.get_account()
        return _required_float(getattr(account, "equity", None), "equity")

    @retry_alpaca()
    def get_holdings(self) -> list[Holding]:
        """Every open position, sorted by symbol. Empty list when flat."""

        raw = self._trading_client.get_all_positions()
        if not isinstance(raw, list):
            return []
        holdings = [
            Holding(
                symbol=position.symbol,
                qty=_required_float(position.qty, f"{position.symbol}.qty"),
                market_value=_required_float(
                    position.market_value, f"{position.symbol}.market_value"
                ),
                avg_entry_price=_required_float(
                    position.avg_entry_price, f"{position.symbol}.avg_entry_price"
                ),
            )
            for position in raw
        ]
        return sorted(holdings, key=lambda h: h.symbol)

    def held_symbols(self) -> set[str]:
        """Convenience for the decision layer: what do we currently own?"""

        return {holding.symbol for holding in self.get_holdings()}
