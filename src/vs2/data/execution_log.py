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

`append_execution_results` itself retries on failure, unlike `orders.submit`.
That is not a contradiction of orders.py's "never retry a write" rule: this is
a local file append, not a broker call, and does not have the network's
"maybe it landed, maybe it didn't" ambiguity -- a local write either lands or
raises, essentially every time, so retrying it is safe. If every retry still
fails (disk full, permissions), that means real orders were placed with no
durable record of it -- the single scenario this module exists to make
visible -- so the raw outcome is logged at CRITICAL before re-raising, giving
a human something to reconcile from by hand even without the structured log.
The exception is left to propagate and crash the process rather than being
swallowed: a silent "successful" exit here would be worse than a loud crash,
because it would hide the one failure mode that most needs attention.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from vs2.data.orders import SubmissionResult

logger = logging.getLogger(__name__)


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


def _build_rows(results: list[SubmissionResult], day: date) -> list[dict[str, Any]]:
    logged_at = _utc_now_z()
    return [
        {
            "day": day.isoformat(),
            "symbol": result.decision.symbol,
            "action": result.decision.action,
            "notional": result.decision.notional,
            "qty": result.decision.qty,
            "succeeded": result.succeeded,
            "order_id": _order_id(result.order),
            "error": result.error,
            "logged_at": logged_at,
        }
        for result in results
    ]


def append_execution_results(
    results: list[SubmissionResult],
    day: date,
    path: Path,
    *,
    retries: int = 3,
    backoff: float = 0.5,
) -> None:
    """Append one row per submission attempt, success or failure alike.

    Retries local write failures a few times before giving up -- see the
    module docstring for why that is safe here in a way it is not for
    orders.submit. Raises after exhausting retries, having already logged
    the raw results at CRITICAL so they are not lost even if the log file
    itself couldn't be written.
    """

    rows = _build_rows(results, day)

    last_exc: OSError | None = None
    for attempt in range(retries):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            return
        except OSError as exc:
            last_exc = exc
            logger.warning(
                "failed to write execution log (attempt %d/%d): %s",
                attempt + 1,
                retries,
                exc,
            )
            if attempt + 1 < retries:
                time.sleep(backoff * (2**attempt))

    if last_exc is None:
        # retries<=0: the loop body never ran, so nothing was ever attempted.
        # A misuse of this function, not a write failure -- distinct from the
        # real failure case below, which always has an exception to report.
        raise RuntimeError(
            "append_execution_results called with retries<=0; no attempt was made"
        )

    logger.critical(
        "EXECUTION LOG WRITE FAILED after %d attempts -- orders were placed "
        "but the durable record was not written. Manual reconciliation "
        "needed. Raw results: %s",
        retries,
        rows,
    )
    raise last_exc


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
