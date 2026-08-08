"""The daily entrypoint: reconcile, decide, log, and -- only with `--execute`
and only while the market is open -- submit.

Defaults to a dry run. Computing and logging every day's decisions costs
nothing and is the whole point of the measurement design; actually placing
orders is the one consequential step, so it requires an explicit flag rather
than being the default behavior of running this file. This mirrors
value-steward's own `phase:reset` (dry run is the default, `--execute` is
required to act) rather than inventing a new convention.

**Deciding and executing happen in different sessions, and this file is run
many times a day.** The rule needs a *finished* close, which does not exist
until the bell rings, so a decision can only be made after the market shuts.
Executing it therefore has to wait for the next session -- and rather than
leave a market order queued overnight to fill at an unpredictable opening
auction, the cron fires repeatedly through that next session and submits at
the first tick, in liquid regular hours, at a price a human could have seen.

One invocation does whichever of those two jobs is outstanding:

    Mon 16:15  decide Monday (market shut -- no execution)
    Tue 09:45  execute Monday's decisions (Tuesday has not closed yet)
    Tue 16:15  decide Tuesday
    Wed 09:45  execute Tuesday's decisions, reconcile Monday's fills

The decision day is always `calendar.latest_completed_session(...)`, never the
newest bar's own date. That distinction is the safety property this module
turns on: asked at 11am, the calendar answers *yesterday*, so an in-progress
daily bar -- whose "close" is only the last trade so far -- can never be read
as a finished one. Bars that do not reach the completed session are refused
outright as STALE_BARS rather than decided on. Before this gate existed, a
three-day-old bar was traded as though it were today's, with no warning
anywhere.

Execution replays the *logged* decisions rather than recomputing them.
Recomputing the next morning would read a portfolio that morning's own sells
had already changed, and would silently produce different orders from the ones
on record.

Two different questions get two different guards. Dry-run mode asks "did I
already compute this session's decisions" (decision_log.py) -- repeating that
is harmless, so it only prevents redundant log rows. `--execute` mode asks "did
I already attempt to place that day's orders" (execution_log.py) -- that guard
has to be independent, because it must still say yes even if every order in the
attempt failed. See execution_log.py's docstring for why a partial failure is
not auto-retried.

The whole cycle runs under a single-instance file lock (run_lock.py). Cron
does not wait for a prior invocation to finish before starting the next
scheduled one, and there is a real gap between the guard check above and the
guard-relevant write that would make it return differently next time -- see
run_lock.py's docstring for the incident this closes. The lock, not the
guard, is what prevents two overlapping invocations from both proceeding.

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
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from alpaca.data.models import Bar

from vs2.core.crossover import bar_day as _bar_day
from vs2.core.crossover import detect_crosses
from vs2.core.decision import Decision, build_decisions
from vs2.data.bars import BarsClient, create_bars_client
from vs2.data.broker import BrokerClient
from vs2.data.decision_log import append_decisions, load_decisions
from vs2.data.execution_log import (
    already_executed_today,
    append_execution_results,
    pending_submissions,
)
from vs2.data.fills import FillReader, append_fills, reconciled_ids
from vs2.data.market_calendar import MarketCalendar
from vs2.data.orders import OrderClient, SubmissionResult
from vs2.data.run_lock import AlreadyRunningError, single_instance
from vs2.data.session_log import append_session, build_record, last_decided_day

logger = logging.getLogger(__name__)

DEFAULT_SLOTS = 20
DEFAULT_LOOKBACK_DAYS = 110  # >> the 51 bars the rule needs, to absorb gaps


@dataclass(frozen=True)
class LogPaths:
    """Where the four append-only records live.

    Grouped into one object because `run()` needs all of them and threading
    four separate Path arguments through every call site made the signature
    harder to read than the thing it describes.
    """

    decisions: Path
    executions: Path
    sessions: Path
    fills: Path
    lock: Path

    @classmethod
    def under(cls, root: Path) -> LogPaths:
        return cls(
            decisions=root / "decisions.jsonl",
            executions=root / "executions.jsonl",
            sessions=root / "sessions.jsonl",
            fills=root / "fills.jsonl",
            lock=root / "run_daily.lock",
        )


@dataclass(frozen=True)
class RunResult:
    day: date | None
    trading_day: bool
    already_ran: bool
    already_running: bool
    decisions: list[Decision]
    submitted: list[SubmissionResult]
    executed: bool
    stale_bars: bool = False
    bars_as_of: date | None = None
    missed_sessions: int = 0
    decided: bool = False
    market_open: bool = False
    reconciled: int = 0
    declined_for_cash: list[str] = field(default_factory=list)


def run(
    calendar: MarketCalendar,
    broker: BrokerClient,
    bars_client: BarsClient,
    order_client: OrderClient,
    universe: list[str],
    paths: LogPaths,
    *,
    fill_reader: FillReader | None = None,
    slots: int = DEFAULT_SLOTS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    execute: bool = False,
    force: bool = False,
    now: datetime | None = None,
) -> RunResult:
    """Run one cycle. Safe to call repeatedly through the day, including
    concurrently: a second overlapping invocation fails fast on the lock rather
    than racing the first one.

    The cycle decides for the most recently *completed* session and executes
    that session's logged decisions during the next open market -- see the
    module docstring. Whichever of those is already done is skipped, so the
    many cron ticks a day converge on exactly one decision and one execution
    attempt per session.

    `now` is injected rather than read from the clock so the whole schedule is
    testable; it is interpreted as US Eastern when naive, matching the
    broker's own calendar. Both guards are skipped by `force=True` -- the lock
    is not; force does not mean "run even on top of another live invocation."
    """

    now = now or datetime.now()

    try:
        with single_instance(paths.lock):
            return _run_locked(
                calendar,
                broker,
                bars_client,
                order_client,
                universe,
                paths,
                fill_reader=fill_reader,
                slots=slots,
                lookback_days=lookback_days,
                execute=execute,
                force=force,
                now=now,
            )
    except AlreadyRunningError:
        logger.warning(
            "%s: another instance is already running (lock at %s); skipping",
            now,
            paths.lock,
        )
        return RunResult(None, False, False, True, [], [], False)


def _count_missed_sessions(
    calendar: MarketCalendar, previous: date | None, current: date
) -> int:
    """Sessions between the last decided day and this one that were skipped.

    A day with no decision rows is otherwise ambiguous between "the rule found
    nothing" and "the run never happened", and over sixty sessions those mean
    opposite things. Counted from the broker's calendar rather than by
    subtracting dates, so weekends and holidays are not miscounted as misses.
    """

    if previous is None or current <= previous:
        return 0
    sessions = calendar.get_sessions(previous, current)
    return max(0, len([s for s in sessions if previous < s.day < current]))


def _trim_to_session(
    bars_by_symbol: dict[str, list[Bar]], decision_day: date
) -> tuple[dict[str, list[Bar]], date | None]:
    """Drop bars dated after the completed session; report the newest day left.

    During market hours Alpaca serves a daily bar for the session in progress,
    whose "close" is only the last trade so far. Evaluating a crossover on it
    would compare a partial price against an average that assumes a final one.
    Trimming happens before `detect_crosses` ever sees the data, so the rule
    itself never has to know the difference.
    """

    trimmed: dict[str, list[Bar]] = {}
    newest: date | None = None
    for symbol, bars in bars_by_symbol.items():
        kept = [
            bar
            for bar in bars
            if (day := _bar_day(bar)) is not None and day <= decision_day
        ]
        trimmed[symbol] = kept
        if kept:
            last = _bar_day(kept[-1])
            if last is not None and (newest is None or last > newest):
                newest = last
    return trimmed, newest


def _reconcile(
    fill_reader: FillReader | None, paths: LogPaths
) -> int:
    """Ask the broker what previously submitted orders became. Read-only."""

    if fill_reader is None:
        return 0
    pending = pending_submissions(paths.executions, reconciled_ids(paths.fills))
    if not pending:
        return 0
    fills = fill_reader.reconcile(pending)
    terminal = [fill for fill in fills if fill.is_terminal]
    append_fills(terminal, paths.fills)
    if terminal:
        logger.info("reconciled %d order(s) to a terminal state", len(terminal))
    return len(terminal)


def _run_locked(
    calendar: MarketCalendar,
    broker: BrokerClient,
    bars_client: BarsClient,
    order_client: OrderClient,
    universe: list[str],
    paths: LogPaths,
    *,
    fill_reader: FillReader | None,
    slots: int,
    lookback_days: int,
    execute: bool,
    force: bool,
    now: datetime,
) -> RunResult:
    """The actual cycle, called only while `run()` holds the lock."""

    reconciled = _reconcile(fill_reader, paths)

    session = calendar.latest_completed_session(now)
    if session is None:
        logger.info("%s: no completed trading session to act on yet", now)
        append_session(
            build_record(None, "NO_COMPLETED_SESSION", detail=str(now)), paths.sessions
        )
        return RunResult(None, False, False, False, [], [], False, reconciled=reconciled)

    decision_day = session.day
    market_open = calendar.is_open(now)

    account = broker.get_account_state()
    holding_list = broker.get_holdings()
    holdings = {h.symbol: h for h in holding_list}

    existing = load_decisions(paths.decisions, decision_day)
    decisions = existing
    decided_now = False

    if force or not existing:
        bars_by_symbol = bars_client.get_daily_bars(
            universe, lookback_days=lookback_days
        )
        trimmed, bars_as_of = _trim_to_session(bars_by_symbol, decision_day)
        if bars_as_of != decision_day:
            logger.error(
                "STALE BARS: newest bar is %s but the completed session is %s; "
                "deciding nothing. The rule is not evaluated on an old close.",
                bars_as_of,
                decision_day,
            )
            append_session(
                build_record(
                    decision_day,
                    "STALE_BARS",
                    detail=f"newest bar {bars_as_of}, expected {decision_day}",
                    account=account,
                    holdings=holding_list,
                    bars_as_of=bars_as_of,
                ),
                paths.sessions,
            )
            return RunResult(
                decision_day,
                True,
                False,
                False,
                [],
                [],
                False,
                stale_bars=True,
                bars_as_of=bars_as_of,
                market_open=market_open,
                reconciled=reconciled,
            )

        signals = detect_crosses(trimmed)
        dollar_volume = {
            symbol: (bars[-1].close or 0.0) * float(getattr(bars[-1], "volume", 0) or 0)
            for symbol, bars in trimmed.items()
            if bars
        }
        decisions = build_decisions(
            signals,
            holdings,
            equity=account.equity,
            slots=slots,
            dollar_volume=dollar_volume,
            cash=account.cash,
        )
        append_decisions(decisions, paths.decisions)
        decided_now = True

        missed = _count_missed_sessions(
            calendar, last_decided_day(paths.sessions), decision_day
        )
        if missed:
            logger.error(
                "MISSED SESSIONS: %d trading session(s) between the last decided "
                "day and %s have no decisions on record",
                missed,
                decision_day,
            )
        append_session(
            build_record(
                decision_day,
                "DECIDED",
                detail=f"{len(decisions)} decisions",
                account=account,
                holdings=holding_list,
                bars_as_of=bars_as_of,
                missed_sessions=missed,
            ),
            paths.sessions,
        )
    else:
        missed = 0
        logger.info("%s already decided; %d decisions on record", decision_day, len(existing))

    declined_for_cash = [d.symbol for d in decisions if d.action == "BUY_DECLINED_CASH"]
    if declined_for_cash:
        logger.warning(
            "%s: %d buy(s) declined for cash, not slots -- %s",
            decision_day,
            len(declined_for_cash),
            ", ".join(declined_for_cash),
        )

    submitted: list[SubmissionResult] = []
    executed = False

    if execute:
        if not market_open:
            logger.info(
                "%s: market is shut at %s; orders wait for the next session "
                "rather than queueing overnight",
                decision_day,
                now,
            )
        elif not force and already_executed_today(paths.executions, decision_day):
            logger.info(
                "%s already has an execution attempt on record; "
                "pass force=True to retry",
                decision_day,
            )
        else:
            submitted = order_client.submit_all(decisions)
            executed = True
            append_execution_results(submitted, decision_day, paths.executions)
            failed = [r for r in submitted if not r.succeeded]
            if failed:
                logger.error(
                    "EXECUTED: %d/%d orders failed -- %s",
                    len(failed),
                    len(submitted),
                    ", ".join(f"{r.decision.symbol}: {r.error}" for r in failed),
                )
            else:
                logger.info(
                    "EXECUTED: %d/%d orders submitted successfully",
                    len(submitted),
                    len(submitted),
                )
            append_session(
                build_record(
                    decision_day,
                    "EXECUTED",
                    detail=f"{len(submitted) - len(failed)}/{len(submitted)} succeeded",
                    account=account,
                    holdings=holding_list,
                ),
                paths.sessions,
            )
    elif decided_now:
        order_count = sum(1 for d in decisions if d.is_order)
        logger.info(
            "DRY RUN: %d orders would be submitted (not sent) -- pass --execute to send them",
            order_count,
        )

    return RunResult(
        decision_day,
        True,
        not decided_now,
        False,
        decisions,
        submitted,
        executed,
        bars_as_of=decision_day,
        missed_sessions=missed,
        decided=decided_now,
        market_open=market_open,
        reconciled=reconciled,
        declined_for_cash=declined_for_cash,
    )


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
        help="Run even if today's decision day is already logged (dry run) "
        "or already has an execution attempt on record (--execute).",
    )
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    repo_root = Path(__file__).resolve().parents[2]
    universe = (repo_root / "config" / "universe.txt").read_text().split()
    paths = LogPaths.under(repo_root / "data")

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
        paths=paths,
        fill_reader=FillReader(trading_client),
        execute=args.execute,
        force=args.force,
    )

    if result.stale_bars or not result.trading_day:
        return
    if not result.decided and not result.executed:
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

    if result.executed:
        failed = [r for r in result.submitted if not r.succeeded]
        if failed:
            # Non-zero exit is the cheap, dependency-free half of "alert on
            # failure" -- it gives cron's own MAILTO or any external monitor
            # something to key off without this project taking on a
            # notification service as a new dependency. The other half is the
            # ERROR-level log line already written inside run().
            import sys

            sys.exit(1)


if __name__ == "__main__":
    main()
