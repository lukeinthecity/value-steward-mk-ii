"""Import VS1's world-context history: `python -m vs2.world.cli`.

Read-only with respect to the source. It appends to VS2's working file and
writes a tracked snapshot; it never modifies, truncates or rewrites VS1's own
data. In particular it never runs `git checkout` against VS1's tree -- the
early block is read with `git show`, because checking that path out would
overwrite the live file with a five-month-old version, which is the shape of
the incident that already cost this project live state once.
"""

from __future__ import annotations

import argparse
import logging
import subprocess  # nosec B404 - used only for a fixed-form `git show`
import sys
from datetime import date
from pathlib import Path

from vs2.world.import_history import (
    format_report,
    import_history,
    write_snapshot,
)

logger = logging.getLogger(__name__)

# The most recent contiguous run. Rows before this belong to an earlier
# collection separated by a permanent gap; see import_history's docstring.
WORKING_SERIES_START = date(2026, 5, 5)

# The last commit of VS1's world-context.jsonl. The rows in it exist nowhere
# else, so the legacy extraction pins the commit rather than a branch name.
LEGACY_COMMIT = "b5e28a7"


def extract_legacy_block(vs1_repo: Path, out: Path) -> int:
    """Write the pre-gap block out of VS1's git history, without touching its
    working tree. Returns the number of lines recovered."""

    # nosec B603,B607: fixed argv, no shell, no user-controlled element. The
    # bare "git" is deliberate -- resolving it from PATH is what makes this work
    # under any reasonable environment, and the alternative (a hardcoded
    # /usr/bin/git) is the kind of assumption this project has been bitten by.
    result = subprocess.run(  # nosec B603 B607
        ["git", "-C", str(vs1_repo), "show", f"{LEGACY_COMMIT}:data/world-context.jsonl"],
        capture_output=True,
        check=True,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(result.stdout)
    return result.stdout.decode("utf-8").count("\n")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.home() / "value-steward" / "data" / "world-context.jsonl",
        help="VS1's live world-context.jsonl.",
    )
    parser.add_argument(
        "--vs1-repo",
        type=Path,
        default=Path.home() / "value-steward",
        help="VS1's repository, for recovering the pre-gap block.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be imported without writing anything.",
    )
    parser.add_argument(
        "--skip-legacy",
        action="store_true",
        help="Do not extract the pre-gap block.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[3]
    destination = repo_root / "data" / "world_context.jsonl"
    history_dir = repo_root / "world_history"

    if not args.source.exists():
        print(f"[world] source not found: {args.source}")
        sys.exit(1)

    if args.dry_run:
        # Import into a throwaway destination so the report is real without
        # anything being written where it matters.
        import tempfile

        scratch = Path(tempfile.mkdtemp()) / "world_context.jsonl"
        report = import_history(
            args.source, scratch, since=WORKING_SERIES_START
        )
        print(format_report(report, args.source, destination))
        print("\n[world] dry run -- nothing was written.")
        return

    report = import_history(args.source, destination, since=WORKING_SERIES_START)
    print(format_report(report, args.source, destination))

    if not report.accounted_for:
        print("\n[world] ABORTING: rows are unaccounted for. Nothing further written.")
        sys.exit(1)

    snapshot = history_dir / f"snapshot-{date.today().isoformat()}.jsonl.gz"
    size = write_snapshot(destination, snapshot)
    print(f"\n[world] snapshot  {snapshot}  ({size:,} bytes)")

    if not args.skip_legacy:
        legacy = history_dir / "legacy-2026-01-23_2026-03-20.jsonl.gz"
        if legacy.exists():
            print(f"[world] legacy block already archived at {legacy}")
        else:
            import tempfile

            raw = Path(tempfile.mkdtemp()) / "legacy.jsonl"
            try:
                rows = extract_legacy_block(args.vs1_repo, raw)
            except subprocess.CalledProcessError as exc:
                print(f"[world] could not recover the pre-gap block: {exc}")
                return
            write_snapshot(raw, legacy)
            print(
                f"[world] pre-gap block  {legacy}  ({rows} rows, "
                "2026-01-23..2026-03-20, NOT part of the working series)"
            )


if __name__ == "__main__":
    main()
