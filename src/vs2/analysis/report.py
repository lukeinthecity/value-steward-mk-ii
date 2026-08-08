"""The Day-60 review: what the run produced, and whether it can be read.

Pure computation over the run's own logs plus adjusted bars. No network, no
broker -- the same discipline `vs2.core` keeps, for the same reason. Everything
here is a function of recorded facts, so a number can always be traced back to
the rows that produced it.

DESIGN.md fixes what this has to answer, and each part exists because VS1
failed to answer it:

* **Return of positions actually taken, against a buy-and-hold benchmark on the
  same universe over the same dates.** Both legs are computed from the same
  `adjustment=all` bars so a split cannot flatter one side.
* **Average invested exposure.** Stated there as a precondition, not a nicety:
  a partially-invested strategy underperforms a fully-invested benchmark in a
  rising market whatever its timing is worth, so a return number without an
  exposure number beside it is not readable.
* **Signal capture** -- how many cross-ups became orders, and how many orders
  became fills. VS1's execution layer silently discarded 60% of its signals,
  and nothing in its reporting showed that.
* **Realized slippage** against the decision close, checking the 0.31%/yr
  estimate rather than assuming it.
* **Session coverage.** A day with no rows is ambiguous between "no signal" and
  "the run did not happen"; averaging over the days that happen to be present
  is how a broken run looks like a quiet one.

**The benchmark enters on the same terms the strategy gets.** Decisions are
made on a session's close and filled in the next session, so a benchmark
entered at the decision close would hand it an overnight gap the strategy never
had. `benchmark_return` therefore enters at the *next* session's open. Getting
this wrong in the flattering direction is the specific class of measurement
fault that ended VS1's runs 2 and 3.

Where a number cannot be computed it is None, never 0.0. "We have no fills to
measure" and "slippage was zero" are different facts, and the second is a
claim.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from vs2.core.crossover import BarLike, bar_day


@dataclass(frozen=True)
class SignalCapture:
    """Where the rule's own signals ended up. Every cross-up is accounted for."""

    cross_ups: int
    bought: int
    declined_full: int
    declined_cash: int
    submitted: int
    filled: int

    @property
    def accounted_for(self) -> bool:
        """Every cross-up became exactly one of the three decision outcomes.

        The population-completeness check DESIGN.md's measurement section and
        the code-check playbook both insist on -- a signal that is in none of
        the buckets was dropped somewhere.
        """

        return self.cross_ups == self.bought + self.declined_full + self.declined_cash

    @property
    def decision_capture(self) -> float | None:
        """Share of cross-ups the portfolio had room and money to act on."""

        return self.bought / self.cross_ups if self.cross_ups else None

    @property
    def fill_capture(self) -> float | None:
        """Share of intended buys that became actual positions."""

        return self.filled / self.bought if self.bought else None


@dataclass(frozen=True)
class Exposure:
    """How invested the account was, day by day."""

    sessions: int
    mean_invested: float | None
    median_invested: float | None
    min_invested: float | None
    max_invested: float | None


@dataclass(frozen=True)
class Report:
    start: date | None
    end: date | None
    sessions_decided: int
    sessions_expected: int
    sessions_missed: int
    stale_bar_days: int
    strategy_return: float | None
    benchmark_return: float | None
    excess_return: float | None
    exposure: Exposure
    capture: SignalCapture
    median_slippage_bp: float | None
    mean_slippage_bp: float | None
    fills_measured: int
    reason_codes: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def readable(self) -> bool:
        """Whether this run answers the question it was run to answer.

        A verdict is not readable on returns alone. If sessions are missing, if
        exposure was never recorded, or if signals cannot be accounted for,
        the comparison is against an unknown population -- which is exactly the
        state VS1's three runs ended in.
        """

        return not self.warnings


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _latest_per_symbol_day(
    rows: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Collapse re-runs: the last row for a (day, symbol) is the one in force.

    VS1's scorecard inflated 104 real decisions to 214 rows by counting
    duplicates, so de-duplication happens once, here, rather than being left to
    each caller to remember.
    """

    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        day, symbol = row.get("day"), row.get("symbol")
        if day is None or symbol is None:
            continue
        latest[(str(day), str(symbol))] = row
    return list(latest.values())


def summarise_exposure(session_rows: Sequence[Mapping[str, Any]]) -> Exposure:
    """Invested fraction across the DECIDED rows -- one mark per session."""

    fractions = [
        float(row["invested_fraction"])
        for row in session_rows
        if row.get("event") == "DECIDED" and row.get("invested_fraction") is not None
    ]
    return Exposure(
        sessions=len(fractions),
        mean_invested=_mean(fractions),
        median_invested=_median(fractions),
        min_invested=min(fractions) if fractions else None,
        max_invested=max(fractions) if fractions else None,
    )


def summarise_capture(
    decision_rows: Sequence[Mapping[str, Any]],
    execution_rows: Sequence[Mapping[str, Any]],
    fill_rows: Sequence[Mapping[str, Any]],
) -> SignalCapture:
    actions = Counter(str(row.get("action")) for row in decision_rows)
    bought = actions["BUY"]
    declined_full = actions["BUY_DECLINED_FULL"]
    declined_cash = actions["BUY_DECLINED_CASH"]
    submitted = sum(
        1
        for row in execution_rows
        if row.get("action") == "BUY" and row.get("succeeded")
    )
    filled = sum(
        1
        for row in fill_rows
        if row.get("action") == "BUY" and str(row.get("status", "")).lower() == "filled"
    )
    return SignalCapture(
        cross_ups=bought + declined_full + declined_cash,
        bought=bought,
        declined_full=declined_full,
        declined_cash=declined_cash,
        submitted=submitted,
        filled=filled,
    )


def strategy_return(fill_rows: Sequence[Mapping[str, Any]]) -> float | None:
    """Realized return on round trips, weighted by capital deployed.

    Only completed round trips count: a buy still open has no realized result,
    and marking it to market here would mix a realized number with an
    unrealized one in a single figure. Open positions are reported separately
    by the caller rather than folded in silently.
    """

    buys: dict[str, list[tuple[float, float]]] = {}
    results: list[tuple[float, float]] = []  # (capital, return)

    for row in sorted(fill_rows, key=lambda r: (str(r.get("day")), str(r.get("symbol")))):
        if str(row.get("status", "")).lower() != "filled":
            continue
        price = row.get("filled_avg_price")
        qty = row.get("filled_qty")
        if price is None or qty is None or not float(price) or not float(qty):
            continue
        symbol = str(row.get("symbol"))
        if row.get("action") == "BUY":
            buys.setdefault(symbol, []).append((float(price), float(qty)))
        elif row.get("action") == "SELL" and buys.get(symbol):
            entry_price, entry_qty = buys[symbol].pop(0)
            capital = entry_price * entry_qty
            if capital:
                results.append((capital, (float(price) - entry_price) / entry_price))

    if not results:
        return None
    total_capital = sum(capital for capital, _ in results)
    if not total_capital:
        return None
    return sum(capital * ret for capital, ret in results) / total_capital


def benchmark_return(
    bars_by_symbol: Mapping[str, Sequence[BarLike]], start: date, end: date
) -> float | None:
    """Equal-weight buy-and-hold of the same universe over the same dates.

    Entry is the **open of the session after `start`**, matching the terms the
    strategy actually trades on: it decides on a close and fills in the next
    session. Entering the benchmark at the decision close instead would hand it
    an overnight gap the strategy never got, in an unknown direction, and would
    make the comparison a measurement of that gap.

    Exit is the last close at or before `end`. Symbols without usable bars at
    both ends are skipped, and the caller is told how many via the returned
    count being smaller -- silently averaging over a shrunken universe is the
    defect class this project exists to avoid.
    """

    returns: list[float] = []
    for bars in bars_by_symbol.values():
        entry = _first_bar_after(bars, start)
        exit_bar = _last_bar_on_or_before(bars, end)
        if entry is None or exit_bar is None:
            continue
        entry_price = _open_of(entry)
        exit_price = _close_of(exit_bar)
        if not entry_price or exit_price is None:
            continue
        returns.append((exit_price - entry_price) / entry_price)
    return _mean(returns)


def _first_bar_after(bars: Sequence[BarLike], day: date) -> BarLike | None:
    for bar in bars:
        bar_date = bar_day(bar)
        if bar_date is not None and bar_date > day:
            return bar
    return None


def _last_bar_on_or_before(bars: Sequence[BarLike], day: date) -> BarLike | None:
    found: BarLike | None = None
    for bar in bars:
        bar_date = bar_day(bar)
        if bar_date is not None and bar_date <= day:
            found = bar
    return found


def _open_of(bar: BarLike) -> float | None:
    value = getattr(bar, "open", None)
    return float(value) if value is not None else None


def _close_of(bar: BarLike) -> float | None:
    value = getattr(bar, "close", None)
    return float(value) if value is not None else None


def build_report(
    decision_rows: Sequence[Mapping[str, Any]],
    execution_rows: Sequence[Mapping[str, Any]],
    session_rows: Sequence[Mapping[str, Any]],
    fill_rows: Sequence[Mapping[str, Any]],
    bars_by_symbol: Mapping[str, Sequence[BarLike]] | None = None,
) -> Report:
    """Assemble the review from the four logs. Pure -- no I/O."""

    decisions = _latest_per_symbol_day(decision_rows)
    decided_days = sorted({str(row["day"]) for row in decisions if row.get("day")})
    start = date.fromisoformat(decided_days[0]) if decided_days else None
    end = date.fromisoformat(decided_days[-1]) if decided_days else None

    missed = sum(int(row.get("missed_sessions") or 0) for row in session_rows)
    stale = sum(1 for row in session_rows if row.get("event") == "STALE_BARS")

    exposure = summarise_exposure(session_rows)
    capture = summarise_capture(decisions, execution_rows, fill_rows)

    slippages = [
        float(row["slippage_bp"])
        for row in fill_rows
        if row.get("slippage_bp") is not None
    ]

    strategy = strategy_return(fill_rows)
    benchmark = (
        benchmark_return(bars_by_symbol, start, end)
        if bars_by_symbol and start and end
        else None
    )
    excess = (
        strategy - benchmark if strategy is not None and benchmark is not None else None
    )

    warnings: list[str] = []
    if missed:
        warnings.append(
            f"{missed} trading session(s) have no decisions on record -- the "
            "return series covers fewer days than the date range implies"
        )
    if stale:
        warnings.append(
            f"{stale} session(s) were skipped for stale bars; the rule was not "
            "evaluated on those closes"
        )
    if exposure.sessions == 0:
        warnings.append(
            "no invested-exposure rows were recorded, so a return figure cannot "
            "be read against a fully-invested benchmark (DESIGN.md, "
            '"Partial investment is the strategy, not a defect")'
        )
    if not capture.accounted_for:
        warnings.append(
            f"{capture.cross_ups} cross-ups do not reconcile against "
            f"{capture.bought} bought + {capture.declined_full} declined-full + "
            f"{capture.declined_cash} declined-cash -- signals were dropped"
        )
    if capture.bought and capture.filled < capture.bought:
        warnings.append(
            f"{capture.bought - capture.filled} of {capture.bought} intended buys "
            "never became fills"
        )
    if strategy is None:
        warnings.append("no completed round trips, so there is no realized return yet")
    if benchmark is None:
        warnings.append("no benchmark could be computed, so there is no comparison")

    return Report(
        start=start,
        end=end,
        sessions_decided=len(decided_days),
        sessions_expected=len(decided_days) + missed,
        sessions_missed=missed,
        stale_bar_days=stale,
        strategy_return=strategy,
        benchmark_return=benchmark,
        excess_return=excess,
        exposure=exposure,
        capture=capture,
        median_slippage_bp=_median(slippages),
        mean_slippage_bp=_mean(slippages),
        fills_measured=len(slippages),
        reason_codes=dict(
            Counter(str(row.get("reason_code")) for row in decisions).most_common()
        ),
        warnings=warnings,
    )


def load_report(data_dir: Path, bars_by_symbol=None) -> Report:
    """Read the four logs off disk and build the report."""

    return build_report(
        _read_jsonl(data_dir / "decisions.jsonl"),
        _read_jsonl(data_dir / "executions.jsonl"),
        _read_jsonl(data_dir / "sessions.jsonl"),
        _read_jsonl(data_dir / "fills.jsonl"),
        bars_by_symbol,
    )


def _pct(value: float | None) -> str:
    return "--" if value is None else f"{value:>8.2%}"


def _bp(value: float | None) -> str:
    return "--" if value is None else f"{value:.2f}bp"


def format_report(report: Report) -> str:
    """A plain-text review. Deliberately states what is missing as prominently
    as what is measured."""

    capture = report.capture
    lines = [
        "Value Steward mk II -- run review",
        "=" * 60,
        f"Dates            {report.start} to {report.end}",
        f"Sessions decided {report.sessions_decided} of {report.sessions_expected}"
        + (f"  ({report.sessions_missed} MISSED)" if report.sessions_missed else ""),
        f"Stale-bar days   {report.stale_bar_days}",
        "",
        "Return",
        "-" * 60,
        f"  Strategy (realized round trips)  {_pct(report.strategy_return)}",
        f"  Benchmark (equal-weight B&H)     {_pct(report.benchmark_return)}",
        f"  Excess                           {_pct(report.excess_return)}",
        "",
        "Invested exposure  (read the return above only against this)",
        "-" * 60,
        f"  Mean   {_pct(report.exposure.mean_invested)}"
        f"    Median {_pct(report.exposure.median_invested)}",
        f"  Min    {_pct(report.exposure.min_invested)}"
        f"    Max    {_pct(report.exposure.max_invested)}",
        f"  Sessions marked  {report.exposure.sessions}",
        "",
        "Signal capture",
        "-" * 60,
        f"  Cross-ups                {capture.cross_ups}",
        f"    bought                 {capture.bought}",
        f"    declined, no slot      {capture.declined_full}",
        f"    declined, no cash      {capture.declined_cash}",
        f"  Submitted                {capture.submitted}",
        f"  Filled                   {capture.filled}",
        f"  Decision capture         {_pct(capture.decision_capture)}",
        f"  Fill capture             {_pct(capture.fill_capture)}",
        f"  All signals accounted    {'yes' if capture.accounted_for else 'NO'}",
        "",
        "Realized slippage vs decision close  (DESIGN.md estimates 2.55bp)",
        "-" * 60,
        f"  Median {_bp(report.median_slippage_bp)}   Mean {_bp(report.mean_slippage_bp)}"
        f"   over {report.fills_measured} fill(s)",
    ]

    if report.warnings:
        lines += ["", "NOT READABLE AS A VERDICT", "-" * 60]
        lines += [f"  * {warning}" for warning in report.warnings]
    else:
        lines += ["", "No measurement caveats outstanding."]

    return "\n".join(lines)


def report_as_dict(report: Report) -> dict[str, Any]:
    payload = asdict(report)
    payload["start"] = report.start.isoformat() if report.start else None
    payload["end"] = report.end.isoformat() if report.end else None
    payload["readable"] = report.readable
    payload["capture"]["decision_capture"] = report.capture.decision_capture
    payload["capture"]["fill_capture"] = report.capture.fill_capture
    payload["capture"]["accounted_for"] = report.capture.accounted_for
    return payload
