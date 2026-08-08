"""What an order actually became -- the fact submission alone cannot report.

`execution_log.py` records that an order was *submitted*, and that is all it
can honestly record: a market order comes back from Alpaca `accepted` or
`pending_new`, with no price, because it has not traded yet. DESIGN.md's "Why
market orders" ends by promising that "realized slippage against the
decision-day close is recorded per fill, so the 0.31% estimate can be checked
against reality rather than assumed" -- and until this module existed, nothing
ever went back to look.

Reading an order's terminal state is a GET, so `retry_alpaca` applies here,
unlike `orders.submit`. Nothing in this file can place, cancel, or modify an
order; `orders.py` remains the only module that can move money.

Reconciliation runs at the start of each cycle and looks *backwards*, because
an order submitted during one session is not necessarily finished when that
invocation ends. The lookup key is the deterministic `client_order_id` set at
submission time, so a fill is matched to the decision that caused it rather
than guessed at from symbol and timestamp.

Slippage is signed so that **positive always means worse than the decision
close** -- paying above it on a buy, receiving below it on a sell. A single
convention in one place, rather than a sign that has to be reasoned about at
each call site, which is precisely the sort of detail VS1 got wrong in
measurement code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from vs2.data.retry import retry_alpaca

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset(
    {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}
)


class _OrderLookup(Protocol):
    def get_order_by_client_id(self, client_id: str) -> Any: ...


@dataclass(frozen=True)
class Fill:
    """One order's outcome, joined to the decision that produced it."""

    day: date
    symbol: str
    action: str
    client_order_id: str
    status: str
    filled_qty: float | None
    filled_avg_price: float | None
    filled_at: str | None
    decision_close: float | None
    slippage_bp: float | None
    notional: float | None

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in TERMINAL_STATUSES


def _opt_float(value: Any) -> float | None:
    """Alpaca returns numbers as strings and omits them until they exist."""

    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _field(order: Any, name: str) -> Any:
    """Read a field from an SDK model or from a plain dict test fake."""

    value = getattr(order, name, None)
    if value is None and isinstance(order, dict):
        value = order.get(name)
    return value


def slippage_bp(
    action: str, decision_close: float | None, filled_avg_price: float | None
) -> float | None:
    """Basis points of execution cost against the decision-day close.

    Positive is always the unfavourable direction: a buy filled above the close
    or a sell filled below it. Returns None when either price is missing, never
    zero -- "we don't know" and "there was no slippage" are different facts and
    averaging the first into the second would understate the cost.
    """

    if not decision_close or filled_avg_price is None:
        return None
    difference = filled_avg_price - decision_close
    if action == "SELL":
        difference = -difference
    return (difference / decision_close) * 10_000


class FillReader:
    """Reads back submitted orders. Read-only: it cannot place or cancel."""

    def __init__(self, order_source: _OrderLookup) -> None:
        self._order_source = order_source

    @retry_alpaca()
    def fetch(self, client_id: str) -> Any:
        return self._order_source.get_order_by_client_id(client_id)

    def reconcile(self, pending: list[dict[str, Any]]) -> list[Fill]:
        """Look up every pending submission and return what it became.

        One failed lookup does not abort the rest -- the same isolation
        `orders.submit_all` applies to submission, for the same reason: a batch
        that stops at the first error leaves no record of the items behind it.
        """

        fills: list[Fill] = []
        for row in pending:
            client_id = row.get("client_order_id")
            if not client_id:
                continue
            try:
                order = self.fetch(client_id)
            except Exception as exc:  # noqa: BLE001 - every failure must be recorded
                logger.error("could not reconcile %s: %s", client_id, exc)
                continue
            if order is None:
                logger.warning("no order found at the broker for %s", client_id)
                continue

            action = str(row.get("action", ""))
            filled_avg_price = _opt_float(_field(order, "filled_avg_price"))
            decision_close = _opt_float(row.get("decision_close"))
            filled_at = _field(order, "filled_at")
            fills.append(
                Fill(
                    day=date.fromisoformat(str(row["day"])),
                    symbol=str(row.get("symbol", "")),
                    action=action,
                    client_order_id=str(client_id),
                    status=str(_field(order, "status") or "unknown"),
                    filled_qty=_opt_float(_field(order, "filled_qty")),
                    filled_avg_price=filled_avg_price,
                    filled_at=str(filled_at) if filled_at else None,
                    decision_close=decision_close,
                    slippage_bp=slippage_bp(action, decision_close, filled_avg_price),
                    notional=_opt_float(row.get("notional")),
                )
            )
        return fills


def append_fills(fills: list[Fill], path: Path) -> None:
    """Append one row per reconciled order. Logs and continues on failure --
    this is measurement, not the record that orders were placed, so it must
    never abort a cycle. See session_log.append_session for the same call."""

    if not fills:
        return
    logged_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for fill in fills:
                row = asdict(fill)
                row["day"] = fill.day.isoformat()
                row["logged_at"] = logged_at
                handle.write(json.dumps(row) + "\n")
    except OSError as exc:
        logger.error("could not write the fills log at %s: %s", path, exc)


def reconciled_ids(path: Path) -> set[str]:
    """Client order ids already recorded with a terminal status.

    Anything not in this set is still worth asking the broker about; anything
    in it is finished and never looked up again.
    """

    if not path.exists():
        return set()
    done: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            status = str(row.get("status", "")).lower()
            client_id = row.get("client_order_id")
            if client_id and status in TERMINAL_STATUSES:
                done.add(str(client_id))
    return done
