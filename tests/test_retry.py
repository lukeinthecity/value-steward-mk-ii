"""Tests for the shared Alpaca retry/backoff decorator."""

from __future__ import annotations

import pytest

from vs2.data.retry import is_transient, retry_alpaca


@pytest.mark.parametrize(
    "message",
    [
        "429 Client Error",
        "Too Many Requests",
        "500 Internal Server Error",
        "502 Bad Gateway",
    ],
)
def test_transient_messages_are_detected(message: str) -> None:
    assert is_transient(RuntimeError(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "invalid api key",
        "403 Forbidden: subscription does not permit querying recent SIP data",
        "invalid top: should not be larger than 100",
    ],
)
def test_non_transient_messages_are_not_retried(message: str) -> None:
    assert is_transient(RuntimeError(message)) is False


def test_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vs2.data.retry.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    @retry_alpaca()
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("429 too many requests")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_gives_up_after_exhausting_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vs2.data.retry.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    @retry_alpaca()
    def always_429() -> None:
        calls["n"] += 1
        raise RuntimeError("429 too many requests")

    with pytest.raises(RuntimeError, match="429"):
        always_429()
    assert calls["n"] == 3


def test_auth_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vs2.data.retry.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    @retry_alpaca()
    def bad_key() -> None:
        calls["n"] += 1
        raise RuntimeError("invalid api key")

    with pytest.raises(RuntimeError, match="invalid api key"):
        bad_key()
    assert calls["n"] == 1


def test_backoff_delays_grow_exponentially(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("vs2.data.retry.time.sleep", lambda seconds: slept.append(seconds))

    @retry_alpaca(retries=4, backoff=1.0)
    def always_429() -> None:
        raise RuntimeError("429")

    with pytest.raises(RuntimeError):
        always_429()
    assert slept == [1.0, 2.0, 4.0, 8.0]


def test_zero_retries_raises_rather_than_returning_none() -> None:
    @retry_alpaca(retries=0)
    def never_called() -> None:
        raise AssertionError("should not run")

    with pytest.raises(RuntimeError, match="exhausted without making an attempt"):
        never_called()
