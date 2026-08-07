"""Tests for BarsClient. No network calls -- a fake stands in for Alpaca's
StockHistoricalDataClient, matching the project's no-real-API-calls-in-tests rule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from vs2.data.bars import BarsClient, retry_alpaca


@dataclass
class FakeBar:
    timestamp: datetime
    close: float


class _Response:
    def __init__(self, data: dict[str, list[FakeBar]]) -> None:
        self.data = data


class RecordingFakeSource:
    """Captures the request it was called with and returns canned bars."""

    def __init__(self, data: dict[str, list[FakeBar]]) -> None:
        self._data = data
        self.last_request: Any = None
        self.call_count = 0

    def get_stock_bars(self, request: Any) -> _Response:
        self.last_request = request
        self.call_count += 1
        return _Response(self._data)


def _bar(day: int, close: float) -> FakeBar:
    return FakeBar(timestamp=datetime(2026, 8, day, tzinfo=timezone.utc), close=close)


def test_requests_all_adjustment_and_sip_feed_explicitly() -> None:
    source = RecordingFakeSource({"AAPL": [_bar(5, 200.0)]})
    BarsClient(source).get_daily_bars(["AAPL"], lookback_days=75)

    request = source.last_request
    assert str(request.adjustment) == "all" or request.adjustment.value == "all"
    assert str(request.feed) == "sip" or request.feed.value == "sip"


def test_sorts_bars_oldest_first_even_if_returned_out_of_order() -> None:
    out_of_order = [_bar(7, 3.0), _bar(5, 1.0), _bar(6, 2.0)]
    source = RecordingFakeSource({"AAPL": out_of_order})
    result = BarsClient(source).get_daily_bars(["AAPL"], lookback_days=75)

    closes = [b.close for b in result["AAPL"]]
    assert closes == [1.0, 2.0, 3.0]


def test_returns_empty_dict_when_response_has_no_data_mapping() -> None:
    class WeirdSource:
        def get_stock_bars(self, request: Any) -> object:
            return object()  # no .data attribute at all

    result = BarsClient(WeirdSource()).get_daily_bars(["AAPL"], lookback_days=75)
    assert result == {}


def test_multiple_symbols_each_sorted_independently() -> None:
    source = RecordingFakeSource(
        {
            "AAPL": [_bar(6, 2.0), _bar(5, 1.0)],
            "MSFT": [_bar(6, 20.0), _bar(5, 10.0)],
        }
    )
    result = BarsClient(source).get_daily_bars(["AAPL", "MSFT"], lookback_days=75)

    assert [b.close for b in result["AAPL"]] == [1.0, 2.0]
    assert [b.close for b in result["MSFT"]] == [10.0, 20.0]


class _FlakySource:
    """Fails with a retryable error a fixed number of times, then succeeds."""

    def __init__(self, fail_times: int, message: str = "429 too many requests") -> None:
        self.fail_times = fail_times
        self.message = message
        self.calls = 0

    def get_stock_bars(self, request: Any) -> _Response:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(self.message)
        return _Response({"AAPL": [_bar(5, 42.0)]})


def test_retries_on_rate_limit_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vs2.data.bars.time.sleep", lambda _seconds: None)
    source = _FlakySource(fail_times=2)
    result = BarsClient(source).get_daily_bars(["AAPL"], lookback_days=75)

    assert source.calls == 3
    assert result["AAPL"][0].close == 42.0


def test_gives_up_after_exhausting_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vs2.data.bars.time.sleep", lambda _seconds: None)
    source = _FlakySource(fail_times=10)  # never succeeds within 3 default retries

    with pytest.raises(RuntimeError, match="429"):
        BarsClient(source).get_daily_bars(["AAPL"], lookback_days=75)
    assert source.calls == 3


def test_does_not_retry_non_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vs2.data.bars.time.sleep", lambda _seconds: None)
    source = _FlakySource(fail_times=10, message="invalid api key")

    with pytest.raises(RuntimeError, match="invalid api key"):
        BarsClient(source).get_daily_bars(["AAPL"], lookback_days=75)
    assert source.calls == 1  # no retry attempted


def test_retry_decorator_exhausted_without_any_attempt_is_unreachable_in_practice() -> (
    None
):
    # retries=0 is the only way to hit the "exhausted without an attempt" branch.
    @retry_alpaca(retries=0)
    def never_called() -> None:
        raise AssertionError("should not run")

    with pytest.raises(RuntimeError, match="exhausted without making an attempt"):
        never_called()
