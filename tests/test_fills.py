"""Tests for fill reconciliation -- what an order actually became, and the
slippage that DESIGN.md promises is measured rather than assumed.

Order fakes are plain dicts and stub objects, matching both shapes the reader
has to cope with: Alpaca's real Order model exposes attributes, while test
fakes elsewhere in this suite return dicts.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from vs2.data.fills import (
    Fill,
    FillReader,
    append_fills,
    reconciled_ids,
    slippage_bp,
)

DAY = date(2026, 8, 10)


class FakeOrderSource:
    def __init__(self, orders: dict, fail: set[str] | None = None) -> None:
        self._orders = orders
        self._fail = fail or set()
        self.looked_up: list[str] = []

    def get_order_by_client_id(self, client_id: str):
        self.looked_up.append(client_id)
        if client_id in self._fail:
            raise RuntimeError(f"lookup failed: {client_id}")
        return self._orders.get(client_id)


def submission(
    symbol: str = "AAPL",
    action: str = "BUY",
    client_id: str = "vs2-2026-08-10-AAPL-BUY",
    decision_close: float | None = 100.0,
) -> dict:
    return {
        "day": "2026-08-10",
        "symbol": symbol,
        "action": action,
        "client_order_id": client_id,
        "decision_close": decision_close,
        "notional": 5000.0,
    }


# --- slippage sign convention -------------------------------------------------
#
# Positive always means worse than the decision close. A sign reasoned about
# separately at each call site is exactly the kind of measurement detail VS1
# got wrong.


def test_buying_above_the_close_is_positive_slippage() -> None:
    assert slippage_bp("BUY", 100.0, 100.5) == pytest.approx(50.0)


def test_buying_below_the_close_is_negative_slippage() -> None:
    assert slippage_bp("BUY", 100.0, 99.5) == pytest.approx(-50.0)


def test_selling_below_the_close_is_positive_slippage() -> None:
    assert slippage_bp("SELL", 100.0, 99.5) == pytest.approx(50.0)


def test_selling_above_the_close_is_negative_slippage() -> None:
    assert slippage_bp("SELL", 100.0, 100.5) == pytest.approx(-50.0)


def test_missing_prices_give_none_not_zero() -> None:
    """'We don't know' and 'there was no slippage' are different facts, and
    averaging the first into the second would understate the cost."""

    assert slippage_bp("BUY", None, 100.0) is None
    assert slippage_bp("BUY", 100.0, None) is None
    assert slippage_bp("BUY", 0.0, 100.0) is None


# --- reconciliation -----------------------------------------------------------


def test_a_filled_order_is_joined_to_its_decision_close() -> None:
    source = FakeOrderSource(
        {
            "vs2-2026-08-10-AAPL-BUY": {
                "status": "filled",
                "filled_qty": "49.5",
                "filled_avg_price": "101.0",
                "filled_at": "2026-08-11T14:00:00Z",
            }
        }
    )

    fills = FillReader(source).reconcile([submission()])

    assert len(fills) == 1
    fill = fills[0]
    assert fill.status == "filled"
    assert fill.filled_avg_price == pytest.approx(101.0)
    assert fill.decision_close == pytest.approx(100.0)
    assert fill.slippage_bp == pytest.approx(100.0)
    assert fill.is_terminal is True


def test_an_attribute_shaped_order_reads_the_same_as_a_dict() -> None:
    class SdkOrder:
        status = "filled"
        filled_qty = "10"
        filled_avg_price = "99.0"
        filled_at = "2026-08-11T14:00:00Z"

    source = FakeOrderSource({"vs2-2026-08-10-AAPL-BUY": SdkOrder()})
    fills = FillReader(source).reconcile([submission()])

    assert fills[0].filled_avg_price == pytest.approx(99.0)


def test_a_pending_order_is_reported_but_not_terminal() -> None:
    source = FakeOrderSource(
        {"vs2-2026-08-10-AAPL-BUY": {"status": "accepted", "filled_avg_price": None}}
    )

    fill = FillReader(source).reconcile([submission()])[0]

    assert fill.is_terminal is False
    assert fill.filled_avg_price is None
    assert fill.slippage_bp is None


def test_a_rejected_order_is_terminal_with_no_price() -> None:
    source = FakeOrderSource({"vs2-2026-08-10-AAPL-BUY": {"status": "rejected"}})

    fill = FillReader(source).reconcile([submission()])[0]

    assert fill.is_terminal is True
    assert fill.filled_avg_price is None


def test_one_failed_lookup_does_not_abort_the_rest() -> None:
    """The same isolation submit_all applies, for the same reason: a batch that
    stops at the first error leaves no record of the items behind it."""

    source = FakeOrderSource(
        {"vs2-2026-08-10-BBB-BUY": {"status": "filled", "filled_avg_price": "10"}},
        fail={"vs2-2026-08-10-AAA-BUY"},
    )

    fills = FillReader(source).reconcile(
        [
            submission("AAA", client_id="vs2-2026-08-10-AAA-BUY"),
            submission("BBB", client_id="vs2-2026-08-10-BBB-BUY"),
        ]
    )

    assert [f.symbol for f in fills] == ["BBB"]
    assert source.looked_up == ["vs2-2026-08-10-AAA-BUY", "vs2-2026-08-10-BBB-BUY"]


def test_an_order_the_broker_has_never_heard_of_is_skipped() -> None:
    source = FakeOrderSource({})
    assert FillReader(source).reconcile([submission()]) == []


def test_a_submission_without_a_client_id_is_not_looked_up() -> None:
    source = FakeOrderSource({})
    row = submission()
    row["client_order_id"] = None

    assert FillReader(source).reconcile([row]) == []
    assert source.looked_up == []


# --- persistence --------------------------------------------------------------


def fill(client_id: str = "vs2-2026-08-10-AAPL-BUY", status: str = "filled") -> Fill:
    return Fill(
        day=DAY,
        symbol="AAPL",
        action="BUY",
        client_order_id=client_id,
        status=status,
        filled_qty=10.0,
        filled_avg_price=101.0,
        filled_at="2026-08-11T14:00:00Z",
        decision_close=100.0,
        slippage_bp=100.0,
        notional=5000.0,
    )


def test_append_writes_one_row_per_fill(tmp_path: Path) -> None:
    path = tmp_path / "fills.jsonl"
    append_fills([fill(), fill("vs2-2026-08-10-BBB-BUY")], path)

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["day"] == "2026-08-10"


def test_append_of_nothing_creates_no_file(tmp_path: Path) -> None:
    path = tmp_path / "fills.jsonl"
    append_fills([], path)

    assert not path.exists()


def test_a_write_failure_is_logged_but_never_raises(tmp_path: Path) -> None:
    unwritable = tmp_path / "fills.jsonl"
    unwritable.mkdir()

    append_fills([fill()], unwritable)


def test_reconciled_ids_returns_only_terminal_rows(tmp_path: Path) -> None:
    """A pending order must stay eligible for another look; a finished one is
    never asked about again."""

    path = tmp_path / "fills.jsonl"
    append_fills(
        [
            fill("done", status="filled"),
            fill("still-going", status="accepted"),
            fill("gone", status="canceled"),
        ],
        path,
    )

    assert reconciled_ids(path) == {"done", "gone"}


def test_reconciled_ids_is_empty_for_a_missing_file(tmp_path: Path) -> None:
    assert reconciled_ids(tmp_path / "absent.jsonl") == set()
