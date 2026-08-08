"""Persist decisions to an append-only JSONL log -- the audit record
DESIGN.md's measurement section requires: one row per symbol per decision day,
including every HOLD, NO_ACTION and BUY_DECLINED_FULL, not just executed
trades. See VS1_MECHANISM_NOTES.md entries 6 and 7 for why dropping those rows
was a recurring, root-cause defect worth deliberately not repeating.

The log doubles as the source of truth for "did today already run" -- see
`last_logged_day` -- rather than inventing a second state file that could
drift from it.

It is also what execution *replays*. A decision is made on one session's
close and executed during the next session, so the two are separated by
hours and by process boundaries. `load_decisions` reads back a given day's
rows rather than recomputing them, and that distinction matters: recomputing
the next morning would read a portfolio the morning's own sells had already
changed, and would silently produce different decisions from the ones
recorded. The logged row is the contract.
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from vs2.core.decision import Decision


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def append_decisions(decisions: list[Decision], path: Path) -> None:
    """Append one JSON line per decision. Does not overwrite prior runs."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for decision in decisions:
            row = asdict(decision)
            row["day"] = decision.day.isoformat() if decision.day else None
            row["logged_at"] = _utc_now_z()
            handle.write(json.dumps(row) + "\n")


def _row_to_decision(row: dict[str, Any]) -> Decision:
    """Rebuild a Decision from a logged row, ignoring bookkeeping columns.

    Reads only the fields the dataclass declares, so a row written by an older
    version (before the tiebreak and capacity columns existed) loads with those
    fields at their defaults rather than raising -- a run in progress must not
    break because the schema grew.
    """

    known = {field.name for field in fields(Decision)}
    kwargs: dict[str, Any] = {key: value for key, value in row.items() if key in known}
    raw_day = kwargs.get("day")
    kwargs["day"] = date.fromisoformat(raw_day) if raw_day else None
    return Decision(**kwargs)


def load_decisions(path: Path, day: date) -> list[Decision]:
    """Every decision recorded for `day`, one per symbol, in symbol order.

    When a day has been logged more than once -- `--force` re-runs it, say --
    the *last* row for each symbol wins, so a replay executes the decisions
    that were actually in force rather than a superseded first attempt.
    """

    if not path.exists():
        return []
    target = day.isoformat()
    latest: dict[str, Decision] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("day") != target:
                continue
            decision = _row_to_decision(row)
            latest[decision.symbol] = decision
    return [latest[symbol] for symbol in sorted(latest)]


def last_logged_day(path: Path) -> date | None:
    """The most recent decision day already on record, or None if the log is
    absent or empty. Reads the whole file -- the log is small (30 rows/day)
    and this runs once per invocation, so simplicity wins over an index."""

    if not path.exists():
        return None
    last_day: str | None = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("day"):
                last_day = row["day"]
    return date.fromisoformat(last_day) if last_day else None
