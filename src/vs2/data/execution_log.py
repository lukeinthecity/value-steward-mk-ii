"""Record what actually happened when orders were submitted -- a fact
`decision_log.py` cannot answer on its own.

`decision_log.py` records intent and is written before submission is
attempted, on purpose: even a total crash leaves an accurate record of what
was decided. But that same design means the decision log cannot tell you
whether execution was ever attempted, let alone whether it succeeded --
"decided" and "done" are different facts, and this file is where the second
one lives.

`already_executed_today` is the guard `run_daily.run()` uses in `--execute`
mode: once execution has been attempted for a decision day -- regardless of
whether every order succeeded -- it will not attempt again automatically. A
partial failure is deliberately not auto-retried. Recomputing and resubmitting
could recreate the same failure, or partially duplicate the orders that did
succeed, since a rerun starts from whatever the account looks like *then*, not
from where the original attempt left off. A human reading this log has that
context; the once-per-day guard does not, so it stops and waits rather than
guessing. Use `force=True` to retry deliberately.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from vs2.data.orders import SubmissionResult


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _order_id(order: Any) -> str | None:
    """Alpaca's real Order model exposes `.id`; test fakes commonly return a
    plain dict with an "id" key. Try both rather than assuming one shape."""

    if order is None:
        return None
    order_id = getattr(order, "id", None)
    if order_id is None and isinstance(order, dict):
        order_id = order.get("id")
    return str(order_id) if order_id is not None else None


def append_execution_results(
    results: list[SubmissionResult], day: date, path: Path
) -> None:
    """Append one row per submission attempt, success or failure alike."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for result in results:
            row = {
                "day": day.isoformat(),
                "symbol": result.decision.symbol,
                "action": result.decision.action,
                "notional": result.decision.notional,
                "qty": result.decision.qty,
                "succeeded": result.succeeded,
                "order_id": _order_id(result.order),
                "error": result.error,
                "logged_at": _utc_now_z(),
            }
            handle.write(json.dumps(row) + "\n")


def already_executed_today(path: Path, day: date) -> bool:
    """True if an execution attempt (any outcome) is already on record for
    this decision day."""

    if not path.exists():
        return False
    target = day.isoformat()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if json.loads(line).get("day") == target:
                return True
    return False
