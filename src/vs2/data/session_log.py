"""What happened on each session, and what the account looked like at the time.

`decision_log.py` records what was decided per symbol; `execution_log.py`
records what was submitted. Neither answers two questions the Day-60 review
cannot be written without:

* **How much of the account was actually invested?** DESIGN.md's "Partial
  investment is the strategy, not a defect" is explicit that a verdict is
  unreadable without it: a partially-invested strategy underperforms a
  100%-invested benchmark in a rising market regardless of whether its timing
  is any good. Equity and holdings were being read every day and thrown away,
  and no amount of later analysis can reconstruct a daily exposure series that
  was never written down.
* **Did a session get skipped, and why?** A day with no decision rows is
  ambiguous between "the rule found nothing" and "the run never happened" --
  and over sixty sessions those mean opposite things about the measurement.

So every meaningful outcome appends a row, including the ones where nothing
was decided. The file is append-only, like the other two logs: a session's
decide and execute phases happen hours apart in separate processes, so a row
is never revised, only followed by another. Events are read back by kind, and
the end-of-day `DECIDED` rows are what the exposure series is built from.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from vs2.data.broker import AccountState, Holding

logger = logging.getLogger(__name__)

SessionEvent = Literal[
    "DECIDED",  # the day's decisions were computed and logged
    "EXECUTED",  # an execution attempt was made for that decision day
    "STALE_BARS",  # bars did not reach the completed session; nothing decided
    "NO_COMPLETED_SESSION",  # asked before any session had closed
]


@dataclass(frozen=True)
class SessionRecord:
    """One event on one decision day, with the account state at that moment."""

    day: date | None
    event: SessionEvent
    detail: str
    equity: float | None
    cash: float | None
    long_market_value: float | None
    position_count: int | None
    positions: dict[str, float] = field(default_factory=dict)
    bars_as_of: date | None = None
    missed_sessions: int = 0

    @property
    def invested_fraction(self) -> float | None:
        """Share of equity held in positions, the number DESIGN.md requires."""

        if self.equity is None or not self.equity or self.long_market_value is None:
            return None
        return self.long_market_value / self.equity


def snapshot(
    account: AccountState, holdings: list[Holding]
) -> tuple[float, float, int, dict[str, float]]:
    """Reduce account and positions to the four fields a session row carries."""

    long_market_value = sum(holding.market_value for holding in holdings)
    positions = {holding.symbol: holding.market_value for holding in holdings}
    return long_market_value, account.cash, len(holdings), positions


def build_record(
    day: date | None,
    event: SessionEvent,
    *,
    detail: str = "",
    account: AccountState | None = None,
    holdings: list[Holding] | None = None,
    bars_as_of: date | None = None,
    missed_sessions: int = 0,
) -> SessionRecord:
    """Assemble a row. Account state is optional because the events that fire
    before the broker is reached (no completed session) still have to be
    recorded -- an unwritten row is the thing this module exists to prevent."""

    if account is None:
        return SessionRecord(
            day=day,
            event=event,
            detail=detail,
            equity=None,
            cash=None,
            long_market_value=None,
            position_count=None,
            bars_as_of=bars_as_of,
            missed_sessions=missed_sessions,
        )
    long_market_value, cash, count, positions = snapshot(account, holdings or [])
    return SessionRecord(
        day=day,
        event=event,
        detail=detail,
        equity=account.equity,
        cash=cash,
        long_market_value=long_market_value,
        position_count=count,
        positions=positions,
        bars_as_of=bars_as_of,
        missed_sessions=missed_sessions,
    )


def append_session(record: SessionRecord, path: Path) -> None:
    """Append one row. Never raises on a write failure.

    Deliberately unlike `execution_log.append_execution_results`, which retries
    and then crashes: that log is the only record that real orders were placed,
    so losing it is worse than stopping. This one is instrumentation. Failing
    to write an exposure row must not abort a cycle that is about to place
    orders, so the failure is logged loudly and the cycle continues.
    """

    row: dict[str, Any] = asdict(record)
    row["day"] = record.day.isoformat() if record.day else None
    row["bars_as_of"] = record.bars_as_of.isoformat() if record.bars_as_of else None
    row["invested_fraction"] = record.invested_fraction
    row["logged_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError as exc:
        logger.error("could not write the session log at %s: %s", path, exc)


def load_sessions(path: Path) -> list[dict[str, Any]]:
    """Every row, oldest first. Used by the report, not by the daily cycle."""

    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def last_decided_day(path: Path) -> date | None:
    """The most recent day a DECIDED row was written for.

    Used to notice a gap: if the session about to be decided is more than one
    session after this, sessions were missed and the report has to say so
    rather than average over the days that happen to be present.
    """

    latest: str | None = None
    for row in load_sessions(path):
        if row.get("event") == "DECIDED" and row.get("day"):
            day = row["day"]
            if latest is None or day > latest:
                latest = day
    return date.fromisoformat(latest) if latest else None
