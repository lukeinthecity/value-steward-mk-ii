"""Persist decisions to an append-only JSONL log -- the audit record
DESIGN.md's measurement section requires: one row per symbol per decision day,
including every HOLD, NO_ACTION and BUY_DECLINED_FULL, not just executed
trades. See VS1_MECHANISM_NOTES.md entries 6 and 7 for why dropping those rows
was a recurring, root-cause defect worth deliberately not repeating.

The log doubles as the source of truth for "did today already run" -- see
`last_logged_day` -- rather than inventing a second state file that could
drift from it.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

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
