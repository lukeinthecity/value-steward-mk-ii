"""The daily entrypoint: check the calendar, decide, log, and -- only with
`--execute` -- submit.

Defaults to a dry run. Computing and logging every day's decisions costs
nothing and is the whole point of the measurement design; actually placing
orders is the one consequential step, so it requires an explicit flag rather
than being the default behavior of running this file. This mirrors
value-steward's own `phase:reset` (dry run is the default, `--execute` is
required to act) rather than inventing a new convention.

`run()` takes every client as a parameter and touches no global state, so it
is fully testable against fakes -- see test_run_daily.py. `main()` is the only
place real credentials and real clients are constructed, and is deliberately
thin: argument parsing and composition, nothing else. Guard the entrypoint so
importing this module for tests never executes real work, per this project's
established convention (see index_membership.py's own main()).
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from vs2.core.crossover import detect_crosses
from vs2.core.decision import Decision, build_decisions
from vs2.data.bars import BarsClient, create_bars_client
from vs2.data.broker import BrokerClient
from vs2.data.decision_log import append_decisions, last_logged_day
from vs2.data.market_calendar import MarketCalendar
from vs2.data.orders import OrderClient

logger = logging.getLogger(__name__)

DEFAULT_SLOTS = 20
DEFAULT_LOOKBACK_DAYS = 110  # >> the 51 bars the rule needs, to absorb gaps


@dataclass(frozen=True)
class RunResult:
    day: date | None
    trading_day: bool
    already_ran: bool
    decisions: list[Decision]
    submitted: list[Any]
    executed: bool


def run(
    calendar: MarketCalendar,
    broker: BrokerClient,
    bars_client: BarsClient,
    order_client: OrderClient,
    universe: list[str],
    log_path: Path,
    *,
    slots: int = DEFAULT_SLOTS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    execute: bool = False,
    force: bool = False,
    today: date | None = None,
) -> RunResult:
    """Run one day's cycle. Safe to call repeatedly: a non-trading day or an
    already-logged decision day is a no-op, not an error, unless `force` skips
    that guard."""

    today = today or date.today()

    if not calendar.is_trading_day(today):
        logger.info("%s is not a trading day; nothing to do", today)
        return RunResult(today, False, False, [], [], False)

    bars_by_symbol = bars_client.get_daily_bars(universe, lookback_days=lookback_days)
    signals = detect_crosses(bars_by_symbol)
    decision_day = next((s.day for s in signals if s.day is not None), None)

    if not force and decision_day is not None:
        last_day = last_logged_day(log_path)
        if last_day is not None and last_day >= decision_day:
            logger.info(
                "%s already logged (last logged day %s); pass force=True to re-run",
                decision_day,
                last_day,
            )
            return RunResult(decision_day, True, True, [], [], False)

    equity = broker.get_equity()
    holdings = {h.symbol: h for h in broker.get_holdings()}
    dollar_volume = {
        symbol: (bars[-1].close or 0.0) * float(getattr(bars[-1], "volume", 0) or 0)
        for symbol, bars in bars_by_symbol.items()
        if bars
    }

    decisions = build_decisions(
        signals, holdings, equity=equity, slots=slots, dollar_volume=dollar_volume
    )
    append_decisions(decisions, log_path)

    submitted: list[Any] = []
    if execute:
        submitted = order_client.submit_all(decisions)
        logger.info("EXECUTED: submitted %d orders", len(submitted))
    else:
        order_count = sum(1 for d in decisions if d.is_order)
        logger.info(
            "DRY RUN: %d orders would be submitted (not sent) -- pass --execute to send them",
            order_count,
        )

    return RunResult(decision_day, True, False, decisions, submitted, execute)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit orders. Without this flag, decisions are computed and "
        "logged but nothing is sent to the broker.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if today's decision day is already logged.",
    )
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    repo_root = Path(__file__).resolve().parents[2]
    universe = (repo_root / "config" / "universe.txt").read_text().split()
    log_path = repo_root / "data" / "decisions.jsonl"

    api_key = os.environ["ALPACA_API_KEY_ID"]
    secret_key = os.environ["ALPACA_SECRET_KEY"]

    from alpaca.trading.client import TradingClient

    trading_client = TradingClient(api_key, secret_key, paper=True)

    result = run(
        calendar=MarketCalendar(trading_client),
        broker=BrokerClient(trading_client),
        bars_client=create_bars_client(api_key, secret_key),
        order_client=OrderClient(trading_client),
        universe=universe,
        log_path=log_path,
        execute=args.execute,
        force=args.force,
    )

    if not result.trading_day or result.already_ran:
        return

    by_action: dict[str, int] = {}
    for decision in result.decisions:
        by_action[decision.action] = by_action.get(decision.action, 0) + 1
    logger.info("%s: %d decisions -- %s", result.day, len(result.decisions), by_action)

    for decision in result.decisions:
        if decision.is_order:
            logger.info(
                "  %s %s notional=%s qty=%s reason=%s",
                decision.action,
                decision.symbol,
                decision.notional,
                decision.qty,
                decision.reason_code,
            )


if __name__ == "__main__":
    main()
