"""Tests for ntfy push notifications.

No network: every case injects a fake transport. The one thing these must
prove above all is that a broken notification channel cannot break a trading
cycle -- a push is observability, and `send_push` swallowing everything is the
feature, not an oversight.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from vs2.data.push import (
    DEFAULT_SERVER,
    PushConfig,
    PushResult,
    already_pushed,
    close_message,
    load_push_config,
    maybe_push,
    open_message,
    push_priority,
    push_tags,
    record_push,
    send_push,
)

DAY = date(2026, 8, 10)
CONFIG = PushConfig(server="https://ntfy.example", topic="secret-topic", token=None)


class FakeTransport:
    def __init__(self, status: int = 200, raises: Exception | None = None) -> None:
        self.status = status
        self.raises = raises
        self.calls: list[tuple[str, bytes, dict]] = []

    def __call__(self, url: str, body: bytes, headers) -> int:
        self.calls.append((url, body, dict(headers)))
        if self.raises is not None:
            raise self.raises
        return self.status


# --- configuration ------------------------------------------------------------
#
# The env var names are deliberately identical to value-steward's, so copying
# its .env across points both systems at one topic and one phone subscription.


def test_topic_and_server_come_from_the_same_env_vars_vs1_uses() -> None:
    config = load_push_config(
        {"VS_NTFY_TOPIC": "t", "VS_NTFY_SERVER": "https://ntfy.example"}
    )

    assert config is not None
    assert config.url == "https://ntfy.example/t"


def test_server_defaults_to_ntfy_sh() -> None:
    config = load_push_config({"VS_NTFY_TOPIC": "t"})

    assert config is not None
    assert config.server == DEFAULT_SERVER


def test_a_trailing_slash_on_the_server_does_not_double_up() -> None:
    config = load_push_config(
        {"VS_NTFY_TOPIC": "t", "VS_NTFY_SERVER": "https://ntfy.example/"}
    )

    assert config is not None
    assert config.url == "https://ntfy.example/t"


def test_a_topic_needing_escaping_is_url_encoded() -> None:
    config = load_push_config({"VS_NTFY_TOPIC": "a topic/with slash"})

    assert config is not None
    assert " " not in config.url


def test_no_topic_disables_pushes_rather_than_erroring() -> None:
    """A developer without the topic should get a working cycle, not a crash."""

    assert load_push_config({}) is None


def test_the_master_switch_mutes_everything() -> None:
    for value in ("0", "false", "no", "off", "OFF"):
        assert load_push_config({"VS_NTFY_TOPIC": "t", "VS_PUSH_ENABLED": value}) is None


def test_a_non_http_server_is_refused() -> None:
    """A mistyped env var must not reach urlopen, which would accept file://."""

    assert (
        load_push_config({"VS_NTFY_TOPIC": "t", "VS_NTFY_SERVER": "file:///etc/passwd"})
        is None
    )


# --- sending ------------------------------------------------------------------


def test_a_push_posts_the_message_with_ntfy_headers() -> None:
    transport = FakeTransport()

    result = send_push(
        CONFIG,
        label="open",
        title="VS2 live",
        message="3 orders to submit",
        priority=4,
        tags=("rocket",),
        transport=transport,
    )

    assert result.ok is True
    url, body, headers = transport.calls[0]
    assert url == "https://ntfy.example/secret-topic"
    assert body == b"3 orders to submit"
    assert headers["Title"] == "VS2 live"
    assert headers["Priority"] == "4"
    assert headers["Tags"] == "rocket"


def test_a_token_becomes_a_bearer_header() -> None:
    transport = FakeTransport()
    config = PushConfig(server="https://ntfy.example", topic="t", token="tk_secret")

    send_push(config, label="open", title="t", message="m", transport=transport)

    assert transport.calls[0][2]["Authorization"] == "Bearer tk_secret"


def test_no_config_skips_without_calling_the_transport() -> None:
    transport = FakeTransport()

    result = send_push(None, label="open", title="t", message="m", transport=transport)

    assert result.skipped is True
    assert transport.calls == []


def test_a_transport_exception_never_propagates() -> None:
    """The property that matters most: a dead notification channel must not
    take down a cycle that is about to place orders."""

    transport = FakeTransport(raises=RuntimeError("network is down"))

    result = send_push(
        CONFIG,
        label="open",
        title="t",
        message="m",
        transport=transport,
        retries=1,
        retry_base_seconds=0,
    )

    assert result.ok is False
    assert result.skipped is False
    assert result.error == "RuntimeError"


def test_an_http_error_status_is_a_failure_not_a_success() -> None:
    transport = FakeTransport(status=500)

    result = send_push(
        CONFIG, label="open", title="t", message="m",
        transport=transport, retries=0, retry_base_seconds=0,
    )

    assert result.ok is False
    assert result.error == "http_500"


def test_a_failed_push_is_retried() -> None:
    class FlakyTransport:
        def __init__(self) -> None:
            self.attempts = 0

        def __call__(self, url, body, headers) -> int:
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError("transient")
            return 200

    transport = FlakyTransport()
    result = send_push(
        CONFIG, label="open", title="t", message="m",
        transport=transport, retries=2, retry_base_seconds=0,
    )

    assert result.ok is True
    assert transport.attempts == 3


def test_the_secret_topic_never_appears_in_a_failure_result() -> None:
    """Log files are the least controlled place a secret could end up."""

    transport = FakeTransport(raises=RuntimeError("boom secret-topic boom"))

    result = send_push(
        CONFIG, label="open", title="t", message="m",
        transport=transport, retries=0, retry_base_seconds=0,
    )

    assert result.error is not None
    assert "secret-topic" not in result.error


# --- message composition ------------------------------------------------------


def test_the_open_message_says_what_is_about_to_happen() -> None:
    title, message = open_message(DAY, pending_orders=3, equity=100_000.0, invested=0.68)

    assert "VS2 live" in title
    assert "3 orders to submit" in message
    assert "$100,000" in message
    assert "68% invested" in message


def test_the_open_message_reads_naturally_for_a_single_order() -> None:
    _, message = open_message(DAY, pending_orders=1, equity=1.0, invested=0.0)

    assert "1 order to submit" in message


def test_a_quiet_open_says_so_rather_than_saying_zero() -> None:
    _, message = open_message(DAY, pending_orders=0, equity=1.0, invested=0.0)

    assert "nothing to submit" in message


def test_the_close_message_breaks_down_the_days_decisions() -> None:
    _, message = close_message(
        DAY,
        {"BUY": 2, "SELL": 1, "BUY_DECLINED_CASH": 3},
        equity=100_000.0,
        invested=0.7,
    )

    assert "2 buy" in message
    assert "1 sell" in message
    assert "3 declined/cash" in message


def test_order_failures_lead_the_close_message() -> None:
    """The point of a notification is the part you would act on."""

    _, message = close_message(DAY, {"BUY": 2}, equity=1.0, invested=0.0, failures=2)

    assert message.startswith("2 ORDER FAILURES")


def test_a_missed_session_leads_when_there_are_no_failures() -> None:
    _, message = close_message(DAY, {"BUY": 1}, 1.0, 0.0, missed_sessions=1)

    assert message.startswith("1 SESSION(S) MISSED")


def test_stale_bars_replace_the_close_message_entirely() -> None:
    title, message = close_message(DAY, {}, 1.0, 0.0, stale=True)

    assert "STALE BARS" in title
    assert "Nothing was traded" in message


def test_a_quiet_day_still_sends_something_readable() -> None:
    _, message = close_message(DAY, {"NO_ACTION": 30}, equity=100_000.0, invested=0.0)

    assert "no action" in message


def test_priority_and_tags_escalate_only_when_something_is_wrong() -> None:
    assert push_priority() == 3
    assert push_priority(failures=1) == 4
    assert push_priority(missed_sessions=1) == 4
    assert push_priority(stale=True) == 4
    assert push_tags() == ("checkered_flag",)
    assert "warning" in push_tags(failures=1)


# --- dedupe and health --------------------------------------------------------


def test_a_push_is_sent_once_per_day_however_many_times_cron_fires(
    tmp_path: Path,
) -> None:
    """Cron fires roughly ten times a day. Ten identical notifications is
    the same as none -- the reader stops looking."""

    path = tmp_path / "pushes.jsonl"
    transport = FakeTransport()
    sent = 0

    for _ in range(10):
        result = maybe_push(
            CONFIG, path, DAY, "open", "t", "m", transport=transport
        )
        if result.ok:
            sent += 1

    assert sent == 1
    assert len(transport.calls) == 1


def test_the_open_and_close_pushes_do_not_block_each_other(tmp_path: Path) -> None:
    path = tmp_path / "pushes.jsonl"
    transport = FakeTransport()

    maybe_push(CONFIG, path, DAY, "open", "t", "m", transport=transport)
    maybe_push(CONFIG, path, DAY, "close", "t", "m", transport=transport)

    assert len(transport.calls) == 2


def test_the_next_day_pushes_again(tmp_path: Path) -> None:
    path = tmp_path / "pushes.jsonl"
    transport = FakeTransport()

    maybe_push(CONFIG, path, DAY, "open", "t", "m", transport=transport)
    maybe_push(CONFIG, path, date(2026, 8, 11), "open", "t", "m", transport=transport)

    assert len(transport.calls) == 2


def test_a_failed_push_is_retried_on_the_next_tick(tmp_path: Path) -> None:
    """A failure must not count as 'already sent' -- otherwise one transient
    outage silently costs the whole day's notification."""

    path = tmp_path / "pushes.jsonl"
    failing = FakeTransport(raises=RuntimeError("down"))
    maybe_push(
        CONFIG, path, DAY, "open", "t", "m", transport=failing,
    )

    assert already_pushed(path, "open", DAY) is False

    working = FakeTransport()
    result = maybe_push(CONFIG, path, DAY, "open", "t", "m", transport=working)

    assert result.ok is True


def test_every_attempt_is_recorded_including_failures(tmp_path: Path) -> None:
    """You cannot rely on the alert channel to tell you it is broken."""

    path = tmp_path / "pushes.jsonl"
    record_push(path, DAY, PushResult(label="open", ok=False, skipped=False, error="http_500"))

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["ok"] is False
    assert row["error"] == "http_500"
    assert row["label"] == "open"


def test_recording_never_raises_on_an_unwritable_path(tmp_path: Path) -> None:
    unwritable = tmp_path / "pushes.jsonl"
    unwritable.mkdir()

    record_push(path=unwritable, day=DAY, result=PushResult("open", True, False, 200))


def test_an_unconfigured_push_is_not_recorded_as_an_attempt(tmp_path: Path) -> None:
    """Nothing was tried, so there is nothing to report on -- and the log must
    not fill with rows on a machine that simply has no topic set."""

    path = tmp_path / "pushes.jsonl"
    maybe_push(None, path, DAY, "open", "t", "m")

    assert not path.exists()
