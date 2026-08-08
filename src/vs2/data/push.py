"""Push notifications via ntfy — the same server and topic value-steward uses.

A direct port of value-steward's `core/pushNotifications.js`, deliberately
keeping **the same environment variable names** (`VS_NTFY_TOPIC`,
`VS_NTFY_SERVER`, `VS_NTFY_TOKEN`, `VS_PUSH_ENABLED`) so that copying VS1's
`.env` across — which is already how this project gets its Alpaca credentials
— points VS2 at the same topic with no extra configuration, and one phone
subscription covers both systems.

**A push is an observability signal, never a control-path step.** `send_push`
never raises: a broken notification channel must not stop a cycle that is about
to place orders. It returns a result object and records the outcome, because
you cannot rely on the alert channel to tell you the alert channel is broken —
VS1's reasoning, kept.

Two pushes per trading day, matching VS1's initialize/off pair:

* **open** — the first in-session tick, "VS2 is live today and here is what it
  is about to do".
* **close** — after the bell, "here is what today's close decided".

Both are deduplicated against `data/pushes.jsonl`, because cron fires roughly
ten times a day and a notification that arrives ten times is one nobody reads.
That file doubles as the health record: every attempt, success or failure, is
written with its error.

**The topic is a secret** — anyone holding it can read and publish to it. It is
never logged, never included in an error message, and never committed; it lives
only in the gitignored `.env`.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote

logger = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"
FALSEY = {"0", "false", "no", "off"}


class Transport(Protocol):
    """Posts a body and headers to a URL, returning an HTTP status."""

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> int: ...


@dataclass(frozen=True)
class PushConfig:
    server: str
    topic: str
    token: str | None

    @property
    def url(self) -> str:
        return f"{self.server}/{quote(self.topic)}"


@dataclass(frozen=True)
class PushResult:
    label: str
    ok: bool
    skipped: bool
    status: int | None = None
    error: str | None = None


def load_push_config(env: Mapping[str, str]) -> PushConfig | None:
    """Read the ntfy settings, or None when pushes are off or unconfigured.

    Unconfigured is not an error. A developer running this locally without the
    topic should get a working cycle and a log line, not a crash.
    """

    if str(env.get("VS_PUSH_ENABLED", "true")).strip().lower() in FALSEY:
        return None

    topic = (env.get("VS_NTFY_TOPIC") or "").strip()
    if not topic:
        return None

    server = (env.get("VS_NTFY_SERVER") or DEFAULT_SERVER).strip().rstrip("/")
    if not server.startswith(("http://", "https://")):
        # Refused rather than passed to urlopen, which would otherwise happily
        # accept file:// or similar from a mistyped env var.
        logger.warning("[push] VS_NTFY_SERVER is not an http(s) URL; pushes disabled")
        return None

    token = (env.get("VS_NTFY_TOKEN") or "").strip()
    return PushConfig(server=server, topic=topic, token=token or None)


def _urllib_transport(url: str, body: bytes, headers: Mapping[str, str]) -> int:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    # nosec B310: load_push_config has already rejected any scheme other than
    # http/https, so this cannot reach file:// or a custom opener.
    with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
        return int(response.status)


def send_push(
    config: PushConfig | None,
    *,
    label: str,
    title: str,
    message: str,
    priority: int = 3,
    tags: tuple[str, ...] = (),
    transport: Transport | None = None,
    retries: int = 2,
    retry_base_seconds: float = 1.0,
) -> PushResult:
    """Post one notification. Never raises.

    Retries a few times on failure — safe, unlike `orders.submit`, because a
    duplicate notification is harmless where a duplicate order is not.
    """

    if config is None:
        return PushResult(label=label, ok=False, skipped=True)

    send = transport or _urllib_transport
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": title,
        "Priority": str(priority),
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    body = message.encode("utf-8")
    last_error: str | None = None

    for attempt in range(retries + 1):
        try:
            status = send(config.url, body, headers)
            if 200 <= status < 300:
                return PushResult(label=label, ok=True, skipped=False, status=status)
            last_error = f"http_{status}"
        except Exception as exc:  # noqa: BLE001 - a push must never break a cycle
            last_error = type(exc).__name__
        if attempt < retries:
            time.sleep(retry_base_seconds * (2**attempt))

    # The topic is deliberately absent from this line: it is a secret, and log
    # files are the least controlled place it could end up.
    logger.warning("[push] %s failed after %d attempt(s): %s", label, retries + 1, last_error)
    return PushResult(label=label, ok=False, skipped=False, error=last_error)


# --- message composition (pure, so the wording is directly testable) ---------


def _money(value: float | None) -> str:
    return "--" if value is None else f"${value:,.0f}"


def _pct(value: float | None) -> str:
    return "--" if value is None else f"{value:.0%}"


def open_message(
    day: date,
    pending_orders: int,
    equity: float | None,
    invested: float | None,
) -> tuple[str, str]:
    """The "we are live" push, sent on the first in-session tick."""

    orders = (
        "nothing to submit"
        if pending_orders == 0
        else f"{pending_orders} order{'s' if pending_orders != 1 else ''} to submit"
    )
    return (
        f"VS2 live · {day:%a %d %b}",
        f"{orders} from {day.isoformat()} · equity {_money(equity)} · "
        f"{_pct(invested)} invested",
    )


def close_message(
    day: date,
    by_action: Mapping[str, int],
    equity: float | None,
    invested: float | None,
    failures: int = 0,
    missed_sessions: int = 0,
    stale: bool = False,
) -> tuple[str, str]:
    """The "today is decided" push, sent after the bell.

    Anything that went wrong leads the message rather than trailing it — the
    point of a notification is the part you would act on.
    """

    if stale:
        return (
            f"VS2 STALE BARS · {day:%a %d %b}",
            f"No decision for {day.isoformat()}: bars did not reach the "
            "completed session. Nothing was traded.",
        )

    parts = [
        f"{by_action.get(action, 0)} {label}"
        for action, label in (
            ("BUY", "buy"),
            ("SELL", "sell"),
            ("BUY_DECLINED_FULL", "declined/full"),
            ("BUY_DECLINED_CASH", "declined/cash"),
        )
        if by_action.get(action, 0)
    ]
    summary = ", ".join(parts) if parts else "no action"

    prefix = ""
    if failures:
        prefix = f"{failures} ORDER FAILURE{'S' if failures != 1 else ''} · "
    elif missed_sessions:
        prefix = f"{missed_sessions} SESSION(S) MISSED · "

    return (
        f"VS2 done · {day:%a %d %b}",
        f"{prefix}{summary} · equity {_money(equity)} · {_pct(invested)} invested",
    )


def push_priority(failures: int = 0, missed_sessions: int = 0, stale: bool = False) -> int:
    """3 for a normal day, 4 when a human should look. Mirrors VS1's
    `unexpected` flag rather than inventing a new scale."""

    return 4 if (failures or missed_sessions or stale) else 3


def push_tags(failures: int = 0, missed_sessions: int = 0, stale: bool = False) -> tuple[str, ...]:
    if failures or stale:
        return ("warning", "octagonal_sign")
    if missed_sessions:
        return ("warning",)
    return ("checkered_flag",)


# --- dedupe and health -------------------------------------------------------


def already_pushed(path: Path, label: str, day: date) -> bool:
    """True if this push already went out for this day.

    Cron fires roughly ten times a day; without this the phone gets ten
    identical notifications and the reader learns to swipe them away.
    """

    if not path.exists():
        return False
    target = day.isoformat()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("day") == target and row.get("label") == label and row.get("ok"):
                return True
    return False


def record_push(path: Path, day: date, result: PushResult) -> None:
    """Append the outcome. Never raises — this is the health record for an
    observability channel, and losing it must not break a cycle."""

    row: dict[str, Any] = {
        "day": day.isoformat(),
        "label": result.label,
        "ok": result.ok,
        "skipped": result.skipped,
        "status": result.status,
        "error": result.error,
        "logged_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError as exc:
        logger.error("could not write the push log at %s: %s", path, exc)


def maybe_push(
    config: PushConfig | None,
    path: Path,
    day: date,
    label: str,
    title: str,
    message: str,
    *,
    priority: int = 3,
    tags: tuple[str, ...] = (),
    transport: Transport | None = None,
    sender: Callable[..., PushResult] = send_push,
) -> PushResult:
    """Send once per day per label, and record the attempt either way."""

    if already_pushed(path, label, day):
        return PushResult(label=label, ok=False, skipped=True)
    result = sender(
        config,
        label=label,
        title=title,
        message=message,
        priority=priority,
        tags=tags,
        transport=transport,
    )
    if not result.skipped:
        record_push(path, day, result)
    return result
