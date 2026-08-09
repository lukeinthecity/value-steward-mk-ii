"""Bring VS1's world-context history into VS2, without trusting it blindly.

The dataset this imports is the direct input to DESIGN.md's deferred
world-state gating mechanism. Importing it is not that mechanism -- nothing
here feeds a trading decision -- but the data's condition matters enough to
check rather than assume, because a later analysis over a misread population
is the exact failure this project was rebooted to escape.

**What is imported, and what is deliberately not.** The working series is the
most recent *contiguous* run of VS1's collection: 2026-05-05 to 2026-08-07.
An earlier block (2026-01-23 to 2026-03-20) survives only inside a git object
in VS1's repository, separated from the working series by a permanent six-week
hole where collection had stopped. That block is worth keeping and is worth
*not* merging: stitching two runs across an unmarked discontinuity produces a
series every later analysis must either special-case or silently average over.
It is archived separately by `extract_legacy_block`, labelled pre-gap.

**Where the data lives.** Two files, because one file cannot be both safe and
live. `world_history/` holds tracked, immutable snapshots -- the copy that
survives a mistaken `git reset`. `data/world_context.jsonl` is the gitignored
working file the collector appends to. VS1 kept only the second kind, which is
why its history spent five months one careless command away from gone.

Idempotent: rows are identified by (date, slot, generated_at), so re-running
against a file that has grown imports only what is new.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# From VS1's world/schema.worldContext.json. Reproduced rather than imported
# because VS2 must not depend on VS1's tree existing.
REQUIRED_FIELDS = (
    "date",
    "generated_at",
    "summary",
    "tags",
    "sources_used",
    "raw_count",
    "notes",
    "errors",
)
TAG_KEYS = (
    "macro_risk",
    "rate_hawkishness",
    "geopolitical_tension",
    "energy_shock_risk",
    "recession_fear",
)


@dataclass(frozen=True)
class RowProblem:
    """One row that could not be accepted, and why. Never silently dropped."""

    line_number: int
    reason: str


@dataclass(frozen=True)
class ImportReport:
    """What the import actually did. Every input row is accounted for."""

    source_rows: int
    imported: int
    already_present: int
    problems: list[RowProblem] = field(default_factory=list)
    first_date: date | None = None
    last_date: date | None = None
    missing_dates: list[date] = field(default_factory=list)

    @property
    def accounted_for(self) -> bool:
        """Input count equals the sum of every outcome.

        The playbook's most-repeated defect class is a row vanishing between
        stages. This makes that arithmetic explicit rather than assumed.
        """

        return self.source_rows == (
            self.imported + self.already_present + len(self.problems)
        )

    @property
    def missing_weekdays(self) -> list[date]:
        """Missing dates that fall Monday-Friday.

        Reported separately because a weekend gap in an RSS-driven pipeline may
        be ordinary, while a weekday gap is a collection failure. The
        distinction is offered, not assumed -- both lists are available.
        """

        return [d for d in self.missing_dates if d.weekday() < 5]


def row_key(row: dict[str, Any]) -> str:
    """Identity of a context row: a hash of its entire content.

    The obvious key is `(date, slot, generated_at)` -- VS1 wrote several rows
    per date from four intraday slots, so `date` alone is not unique. That key
    is wrong, and VS1's real data proves it: 2026-03-12 / pre_close /
    2026-03-13T02:54:57.838Z appears **twice**, with the two rows differing in
    `scout_cached` (absent on one, `true` on the other). Keying on the triple
    would have silently discarded a genuinely distinct row -- the single most
    repeated defect class in this project's history.

    Hashing the whole row is both safer and simpler: byte-identical duplicates
    collapse, anything that differs in any field is kept. Idempotency is
    unaffected, since re-importing an unchanged file still matches every row.
    """

    return hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_row(row: Any) -> str | None:
    """Return a reason the row is unusable, or None if it is fine.

    Checks the shape VS1's schema declares. Unknown extra fields are allowed
    and preserved -- the schema itself sets `additionalProperties: true`, and
    rewriting an archive to match a newer shape is how provenance is lost.
    """

    if not isinstance(row, dict):
        return f"not a JSON object (got {type(row).__name__})"

    missing = [f for f in REQUIRED_FIELDS if f not in row]
    if missing:
        return f"missing required field(s): {', '.join(missing)}"

    raw_date = row.get("date")
    if not isinstance(raw_date, str):
        return f"date is not a string: {raw_date!r}"
    try:
        date.fromisoformat(raw_date)
    except ValueError:
        return f"date is not an ISO calendar date: {raw_date!r}"

    tags = row.get("tags")
    if not isinstance(tags, dict):
        return f"tags is not an object: {type(tags).__name__}"
    for key in TAG_KEYS:
        if key not in tags:
            return f"tags missing {key!r}"
        value = tags[key]
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"tags.{key} is not a number or null: {value!r}"
        if not 0.0 <= float(value) <= 1.0:
            return f"tags.{key} outside [0,1]: {value!r}"

    raw_count = row.get("raw_count")
    if not isinstance(raw_count, int) or isinstance(raw_count, bool):
        return f"raw_count is not an integer: {raw_count!r}"
    if raw_count < 0:
        return f"raw_count is negative: {raw_count!r}"

    return None


def missing_dates_between(dates: Iterable[date]) -> list[date]:
    """Calendar dates absent between the first and last date present.

    A gap is a fact about the dataset, not a detail to smooth over: a run that
    quietly covers fewer days than its date range implies is unreadable in
    exactly the way DESIGN.md's measurement section warns about.
    """

    unique = set(dates)
    if len(unique) < 2:
        return []
    present = sorted(unique)
    missing: list[date] = []
    cursor = present[0] + timedelta(days=1)
    while cursor < present[-1]:
        if cursor not in unique:
            missing.append(cursor)
        cursor += timedelta(days=1)
    return missing


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[RowProblem]]:
    """Parse a JSONL file, reporting unparseable lines rather than skipping."""

    rows: list[dict[str, Any]] = []
    problems: list[RowProblem] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                problems.append(RowProblem(number, f"unparseable JSON: {exc.msg}"))
    return rows, problems


def existing_keys(path: Path) -> set[str]:
    """Row keys already in the destination, so a re-run imports nothing new."""

    if not path.exists():
        return set()
    rows, _ = read_jsonl(path)
    return {row_key(row) for row in rows}


def import_history(
    source: Path,
    destination: Path,
    *,
    since: date | None = None,
) -> ImportReport:
    """Append every valid, not-already-present row from `source`.

    `since` restricts the import to the contiguous working series, keeping an
    earlier disconnected run out of it. Rows are written verbatim; this is an
    archive, not a migration.
    """

    raw_rows, problems = read_jsonl(source)
    unparseable = len(problems)
    seen = existing_keys(destination)

    accepted: list[dict[str, Any]] = []
    kept_dates: list[date] = []
    already = 0
    for index, row in enumerate(raw_rows, start=1):
        reason = validate_row(row)
        if reason is not None:
            problems.append(RowProblem(index, reason))
            continue
        if since is not None and date.fromisoformat(row["date"]) < since:
            problems.append(
                RowProblem(index, f"before the working series start {since}")
            )
            continue
        kept_dates.append(date.fromisoformat(row["date"]))
        key = row_key(row)
        if key in seen:
            already += 1
            continue
        seen.add(key)
        accepted.append(row)

    if accepted:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as handle:
            for row in accepted:
                handle.write(json.dumps(row) + "\n")

    return ImportReport(
        source_rows=len(raw_rows) + unparseable,
        imported=len(accepted),
        already_present=already,
        problems=problems,
        first_date=min(kept_dates) if kept_dates else None,
        last_date=max(kept_dates) if kept_dates else None,
        missing_dates=missing_dates_between(kept_dates),
    )


def write_snapshot(source: Path, snapshot: Path) -> int:
    """Gzip the working file into a tracked, immutable archive copy.

    The point of the second copy is surviving a mistake on the first. Returns
    the number of bytes written so a caller can assert it is non-empty rather
    than trusting that the call returned.
    """

    snapshot.parent.mkdir(parents=True, exist_ok=True)
    payload = source.read_bytes()
    with gzip.open(snapshot, "wb") as handle:
        handle.write(payload)
    return snapshot.stat().st_size


def format_report(report: ImportReport, source: Path, destination: Path) -> str:
    lines = [
        "world-context import",
        "=" * 56,
        f"  source        {source}",
        f"  destination   {destination}",
        f"  source rows   {report.source_rows}",
        f"  imported      {report.imported}",
        f"  already there {report.already_present}",
        f"  problems      {len(report.problems)}",
        f"  dates         {report.first_date} .. {report.last_date}",
    ]
    if report.missing_dates:
        weekdays = report.missing_weekdays
        lines += [
            "",
            f"  MISSING DATES inside the range: {len(report.missing_dates)} "
            f"({len(weekdays)} of them weekdays)",
        ]
        shown = report.missing_dates[:10]
        lines += [f"      {d}" for d in shown]
        if len(report.missing_dates) > len(shown):
            lines.append(f"      ... and {len(report.missing_dates) - len(shown)} more")
    if report.problems:
        lines += ["", "  PROBLEM ROWS (none were silently dropped):"]
        for problem in report.problems[:10]:
            lines.append(f"      line {problem.line_number}: {problem.reason}")
        if len(report.problems) > 10:
            lines.append(f"      ... and {len(report.problems) - 10} more")
    lines += [
        "",
        f"  every row accounted for: {'yes' if report.accounted_for else 'NO'}",
    ]
    return "\n".join(lines)
