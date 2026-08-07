"""Daily bar fetching -- the one recurring market-data call the crossover rule needs.

`adjustment` and `feed` are set explicitly rather than left to the API default,
per the finding in docs/API_MENU.md: the default `adjustment` is "raw"
(unadjusted), which reports a 10:1 split as an ~90% single-day drop -- fatal for
a rule that compares price to its own trailing average. `feed` is pinned to
`sip` (verified 2026-08-07 to already be this account's default) so that if the
account's entitlement is ever downgraded, a fetch fails loudly with an API
error instead of silently returning thinner iex-only data.

`sip` data cannot be queried too close to "now" on this account's
subscription -- confirmed 2026-08-07 via a live 403,
`"subscription does not permit querying recent SIP data"`, when `end` was left
at the current instant. value-steward's src/valuesteward/data/market_data.py
already worked around this with a 16-minute buffer; that turns out to be a real
constraint, not spare caution, so it is ported here rather than dropped.

The retry/backoff behavior is ported near-verbatim from value-steward's
src/valuesteward/data/alpaca_client.py -- see DESIGN.md's "Stack" section for
why it is reused rather than rewritten.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Protocol

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.models import Bar
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)


def retry_alpaca(retries: int = 3, backoff: float = 1.0) -> Callable:
    """Retry on rate limits and transient server errors; re-raise everything else."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    msg = str(exc).lower()
                    if (
                        "429" in msg
                        or "too many requests" in msg
                        or "500" in msg
                        or "502" in msg
                    ):
                        wait = backoff * (2**attempt)
                        logger.warning(
                            "[ALPACA] rate limit or server error, retrying in "
                            "%ss (%d/%d): %s",
                            wait,
                            attempt + 1,
                            retries,
                            exc,
                        )
                        time.sleep(wait)
                        continue
                    raise
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("retry_alpaca exhausted without making an attempt")

        return wrapper

    return decorator


class _BarsSource(Protocol):
    """The one Alpaca SDK method BarsClient depends on -- narrow on purpose so
    tests can supply a fake without constructing a real StockHistoricalDataClient."""

    def get_stock_bars(self, request: StockBarsRequest) -> Any: ...


SIP_RECENCY_BUFFER_MINUTES = 16


class BarsClient:
    def __init__(self, data_client: _BarsSource) -> None:
        self._data_client = data_client

    @retry_alpaca()
    def get_daily_bars(
        self, symbols: list[str], lookback_days: int
    ) -> dict[str, list[Bar]]:
        """Return daily bars per symbol, oldest first, split/dividend-adjusted.

        `lookback_days` counts calendar days back from now; the crossover rule
        needs 51 trading days of history, which callers should size generously
        (e.g. ~75 calendar days) to absorb weekends and holidays.
        """

        end = datetime.now(timezone.utc) - timedelta(minutes=SIP_RECENCY_BUFFER_MINUTES)
        start = end - timedelta(days=lookback_days)
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.ALL,
            feed=DataFeed.SIP,
        )
        response = self._data_client.get_stock_bars(request)
        data = getattr(response, "data", response)
        if not isinstance(data, dict):
            return {}
        return {
            symbol: sorted(bars, key=lambda b: b.timestamp)
            for symbol, bars in data.items()
        }


def create_bars_client(api_key_id: str, secret_key: str) -> BarsClient:
    """Compose a BarsClient against the real Alpaca API. Not used by tests."""

    return BarsClient(StockHistoricalDataClient(api_key_id, secret_key))
