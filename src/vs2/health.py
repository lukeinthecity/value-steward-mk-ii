"""Standing checks over the run's own logs, for a run nobody is watching.

`run_daily` already reports the problems it can see from inside a cycle: a
failed order, a stale bar, a missed session. What it cannot report is **not
having run at all** -- a process that never starts sends no push, writes no
row, and raises no alarm. That is not hypothetical here: the first dry-run
week lost a session to a WSL VM that shut down with its terminal, and the only
reason it was noticed is that somebody went looking.

So this reads the append-only logs from outside and asks whether they still
look like a healthy run. It is deliberately a separate process on a separate
schedule: a checker that only runs as part of the thing it checks cannot
detect the thing it is most important to detect.

**What it cannot catch is its own host being down.** If the VM stops, neither
`run_daily` nor this runs, and nothing is sent -- `RUN_STALLED` reports the
outage only once the machine is back, which makes it a record rather than an
alarm. The live signal for a dead host is the *absence* of the two daily
pushes, and noticing an absence is a human's job. Closing that properly needs
an off-box dead man's switch, which is deliberately not built here: it would
add an external dependency and a second secret URL to keep out of git.

**Every check here traces to a defect that actually occurred**, in this
codebase or VS1's:

* `RUN_STALLED`   -- the WSL outage above. Nothing else notices silence.
* `DUPLICATE_DECIDED` / `EXECUTION_REPEATED` -- the once-per-day guards not
  converging. VS1's 214 scorecard rows were 104 real decisions; VS2 shipped
  the same class of bug on zero-order days and fixed it in #12.
* `DECISION_COUNT` -- population completeness. A day that decides 27 of 30
  symbols is not a quiet day, it is a broken one, and it reads identically in
  a summary that only counts actions.
* `NO_EXPOSURE`   -- DESIGN.md makes invested exposure a precondition for a
  readable verdict, not a nicety.
* `MISSED_SESSIONS`, `STALE_BARS`, `ORDER_FAILED` -- surfaced again here
  because a push can be missed and a MAILTO can go to a mailbox nobody opens.

`check` is pure: rows in, findings out, no clock and no filesystem beyond what
the caller passes. `main` is the thin shell that reads the files and pushes.
Nothing here can reach a trading decision, and nothing here writes to the four
logs -- it is a reader.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from vs2.data.push import load_push_config, maybe_push

logger = logging.getLogger(__name__)

# Four calendar days spans a normal weekend plus a Monday holiday, so a healthy
# run never trips it. Five would also miss a genuine two-day outage before the
# following weekend hid it.
STALE_AFTER_DAYS = 4

UNIVERSE_SIZE = 30


@dataclass(frozen=True)
class Finding:
    """One thing wrong, named by a stable code so alerts stay greppable."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Every JSON row in an append-only log. A missing file is empty, not an
    error -- a fresh clone has no `data/`, and that is a fact to report through
    a finding rather than a traceback."""

    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _day_of(row: Mapping[str, Any]) -> str:
    return str(row.get("day") or "")


def check(
    sessions: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    executions: Sequence[Mapping[str, Any]],
    *,
    today: date,
    universe_size: int = UNIVERSE_SIZE,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> list[Finding]:
    """Findings, worst-first-ish, or an empty list when the run looks healthy.

    Pure. The caller supplies `today` rather than this reading a clock, so the
    staleness check is testable without freezing time.
    """

    findings: list[Finding] = []
    decided = [row for row in sessions if row.get("event") == "DECIDED"]

    # --- is it running at all? ------------------------------------------------
    if not decided:
        findings.append(Finding("RUN_STALLED", "no DECIDED rows in sessions.jsonl"))
    else:
        newest = max(_day_of(row) for row in decided)
        age = (today - date.fromisoformat(newest)).days
        if age > stale_after_days:
            findings.append(
                Finding(
                    "RUN_STALLED",
                    f"newest decision is {newest}, {age} days old -- is the VM up?",
                )
            )

        latest = max(decided, key=_day_of)
        missed = int(latest.get("missed_sessions") or 0)
        if missed:
            findings.append(
                Finding("MISSED_SESSIONS", f"{_day_of(latest)}: {missed} session(s) missed")
            )

    # --- do the once-per-day guards converge? --------------------------------
    for day, count in sorted(Counter(_day_of(row) for row in decided).items()):
        if count > 1:
            findings.append(
                Finding("DUPLICATE_DECIDED", f"{day}: {count} DECIDED rows, expected 1")
            )

    no_orders = Counter(
        _day_of(row) for row in executions if row.get("action") == "NO_ORDERS"
    )
    for day, count in sorted(no_orders.items()):
        if count > 1:
            findings.append(
                Finding("EXECUTION_REPEATED", f"{day}: {count} NO_ORDERS rows, expected 1")
            )

    # --- is each session complete and measurable? ----------------------------
    per_day = Counter(_day_of(row) for row in decisions)
    for day in sorted({_day_of(row) for row in decided}):
        count = per_day.get(day, 0)
        if count != universe_size:
            findings.append(
                Finding(
                    "DECISION_COUNT",
                    f"{day}: {count} decision rows, expected {universe_size}",
                )
            )

    for row in decided:
        if row.get("invested_fraction") is None:
            findings.append(
                Finding("NO_EXPOSURE", f"{_day_of(row)}: invested_fraction is null")
            )

    # --- did anything go wrong that a push may have missed? ------------------
    for row in sessions:
        if row.get("event") == "STALE_BARS":
            findings.append(
                Finding("STALE_BARS", f"{_day_of(row)}: bars never reached the session close")
            )

    for row in executions:
        if row.get("succeeded") is False:
            findings.append(
                Finding(
                    "ORDER_FAILED",
                    f"{_day_of(row)} {row.get('symbol')}: {row.get('error')}",
                )
            )

    return findings


def summarise(findings: Sequence[Finding], *, limit: int = 8) -> tuple[str, str]:
    """Title and body for the alert. Truncates, because a notification nobody
    can read on a lock screen is one nobody reads at all."""

    count = len(findings)
    title = f"VS2 HEALTH · {count} issue{'s' if count != 1 else ''}"
    lines = [str(finding) for finding in findings[:limit]]
    if count > limit:
        lines.append(f"... and {count - limit} more")
    return title, "\n".join(lines)


def main() -> int:
    """Exit 0 when healthy, 1 when not, so cron's MAILTO is a second channel.

    Silence is the healthy outcome: a daily "all is well" push would be swiped
    away unread within a week, and then the one that mattered would be too.
    """

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    parser = argparse.ArgumentParser(description="Check the run's logs for trouble.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Where the append-only logs live. Defaults to the repo's data/.",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Report findings on stdout only, without notifying.",
    )
    args = parser.parse_args()

    root = args.data_dir or (Path(__file__).resolve().parents[2] / "data")
    findings = check(
        read_rows(root / "sessions.jsonl"),
        read_rows(root / "decisions.jsonl"),
        read_rows(root / "executions.jsonl"),
        today=date.today(),
    )

    if not findings:
        logger.info("[health] no findings")
        return 0

    for finding in findings:
        print(finding)

    if not args.no_push:
        # Imported here, not at module scope: `--no-push` is what the code-check
        # playbook tells an auditor to run, and it must work in a bare checkout
        # that has no .env and no dotenv installed.
        from dotenv import load_dotenv

        load_dotenv()

        title, message = summarise(findings)
        maybe_push(
            load_push_config(os.environ),
            root / "pushes.jsonl",
            date.today(),
            "health",
            title,
            message,
            priority=4,
            tags=("warning",),
        )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
