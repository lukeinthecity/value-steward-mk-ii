"""Print the run review: `python -m vs2.report`.

Reads the four logs, and -- unless `--no-bars` is passed -- fetches the same
`adjustment=all` daily bars the rule itself uses, so the benchmark is computed
from the same prices rather than a second source that could disagree.

Read-only. This module cannot place an order; it imports nothing that can.
Guarded behind `if __name__ == "__main__"` per this project's convention, so
importing it for tests never reaches the network.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from vs2.analysis.report import (
    build_report,
    format_report,
    load_report,
    report_as_dict,
)

logger = logging.getLogger(__name__)

# The benchmark spans the whole run, so the fetch has to reach back past its
# first decision day, not just far enough for a 50-day average.
BENCHMARK_LOOKBACK_DAYS = 400


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-bars",
        action="store_true",
        help="Skip the bar fetch. Everything but the benchmark still computes, "
        "which is useful offline -- the review will say the comparison is "
        "missing rather than quietly omitting it.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the review as JSON instead of text."
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    data_dir = repo_root / "data"

    if args.no_bars:
        report = load_report(data_dir)
    else:
        from dotenv import load_dotenv

        load_dotenv()
        from vs2.analysis.report import _read_jsonl
        from vs2.data.bars import create_bars_client

        universe = (repo_root / "config" / "universe.txt").read_text().split()
        bars_client = create_bars_client(
            os.environ["ALPACA_API_KEY_ID"], os.environ["ALPACA_SECRET_KEY"]
        )
        bars = bars_client.get_daily_bars(
            universe, lookback_days=BENCHMARK_LOOKBACK_DAYS
        )
        report = build_report(
            _read_jsonl(data_dir / "decisions.jsonl"),
            _read_jsonl(data_dir / "executions.jsonl"),
            _read_jsonl(data_dir / "sessions.jsonl"),
            _read_jsonl(data_dir / "fills.jsonl"),
            bars,
        )

    if args.json:
        print(json.dumps(report_as_dict(report), indent=2, default=str))
    else:
        print(format_report(report))


if __name__ == "__main__":
    main()
