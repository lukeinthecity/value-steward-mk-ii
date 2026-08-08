"""Turn today's cross signals into buy/sell decisions. Pure computation.

No network, no filesystem, no side effects -- same discipline as crossover.py.
This module decides nothing about *how* an order is placed; it only decides
*what* should happen. Submission lives in vs2.data.orders, the one module in
this codebase that can move money, so the boundary between "decided" and
"executed" is a file boundary, not a convention someone has to remember.

Every universe symbol gets exactly one Decision every day, including the ones
nothing happens to. VS1's central, repeated defect was dropping declined or
inactionable rows from its own record -- see VS1_MECHANISM_NOTES.md entries 6
and 7 -- so a symbol that was skipped, held, or declined for lack of room is
recorded with the same weight as one that was bought.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from vs2.core.crossover import CrossSignal
from vs2.data.broker import Holding

ActionType = Literal[
    "BUY", "SELL", "HOLD", "NO_ACTION", "BUY_DECLINED_FULL", "BUY_DECLINED_CASH"
]

ORDER_ACTIONS: frozenset[str] = frozenset({"BUY", "SELL"})

# Sells are qty-denominated, so their proceeds depend on a fill price nobody
# knows yet: the decision is made on one session's close and executed in the
# next session, across an overnight gap. Buys are notional (dollar) orders and
# cost exactly their notional whatever the price, so this haircut exists only
# to keep an optimistic estimate of *sell* proceeds from funding a buy that
# then overdraws the account.
SELL_PROCEEDS_HAIRCUT = 0.95


@dataclass(frozen=True)
class Decision:
    """One symbol's verdict for one decision day -- the row DESIGN.md's
    measurement section specifies: symbol, date, action, reason code, price,
    sma50, and the prior day's values that established the cross.

    The tiebreak and capacity fields are carried on every row because
    DESIGN.md's "oversubscription problem" requires the tiebreak's effect to be
    measurable separately from the crossover rule itself. Without
    `dollar_volume` and `tiebreak_rank` on the row, there is no way to
    reconstruct after the fact why one candidate was bought and another
    declined.
    """

    symbol: str
    day: date | None
    action: ActionType
    reason_code: str
    close: float | None
    sma: float | None
    prior_close: float | None
    prior_sma: float | None
    notional: float | None  # set only for BUY
    qty: float | None  # set only for SELL
    dollar_volume: float | None = None  # tiebreak input, cross-up candidates only
    tiebreak_rank: int | None = None  # 1 = highest dollar volume that day
    available_cash: float | None = None  # the day's spending ceiling
    slots_free: int | None = None  # slots open after sells were resolved

    @property
    def is_order(self) -> bool:
        return self.action in ORDER_ACTIONS


def build_decisions(
    signals: list[CrossSignal],
    holdings: dict[str, Holding],
    equity: float,
    slots: int,
    dollar_volume: dict[str, float],
    cash: float,
) -> list[Decision]:
    """Classify every symbol in `signals` as BUY, SELL, HOLD, NO_ACTION,
    BUY_DECLINED_FULL, or BUY_DECLINED_CASH, and size each order.

    `dollar_volume` is the oversubscription tiebreak DESIGN.md specifies --
    trailing dollar volume, highest first -- passed in rather than computed
    here so this function stays a pure function of its inputs. Missing entries
    tiebreak last, not first: a symbol this function knows nothing about
    should never win over one it does.

    Sells are resolved before buys, and free the slots they vacate for the
    same day's buys -- consistent with the capacity measurement in DESIGN.md's
    "Sizing the universe to the signal", which used the same rule.

    **Two different capacity limits, two different reason codes.** A slot limit
    is the portfolio being full (BUY_DECLINED_FULL); a cash limit is the
    account being unable to pay for a slot that is genuinely open
    (BUY_DECLINED_CASH). They are recorded separately because they mean
    different things about the strategy: the first says the universe is too
    large for the position limit, the second says equal-weight sizing against
    *equity* has outrun the *cash* actually available -- which happens
    routinely once holdings appreciate, since each held slot is then worth more
    than equity/slots. Before this split, those buys were submitted anyway and
    rejected by the broker, turning a known capacity limit into an execution
    failure and losing the signal -- the exact leak DESIGN.md's "Why market
    orders" exists to prevent.

    Held symbols with no evaluable signal today (INSUFFICIENT_HISTORY,
    UNSORTED_BARS, INVALID_CLOSE, or simply absent from `signals` because the
    fetch failed) default to HOLD rather than a forced sell -- missing data is
    never a reason to act, only a reason to wait -- but the row's reason_code
    preserves exactly what happened, so the gap is auditable rather than
    silent. See VS1_MECHANISM_NOTES.md entry 10 on failing loud.
    """

    if slots <= 0:
        raise ValueError(f"slots must be positive, got {slots}")
    if equity <= 0:
        raise ValueError(f"equity must be positive, got {equity}")

    held = set(holdings)
    notional_per_slot = equity / slots

    remaining_held = set(held)
    sell_symbols: set[str] = set()
    for sig in signals:
        if sig.symbol in held and sig.signal == "CROSS_DOWN":
            sell_symbols.add(sig.symbol)
            remaining_held.discard(sig.symbol)

    free_slots = max(0, slots - len(remaining_held))

    sell_proceeds = sum(holdings[symbol].market_value for symbol in sell_symbols)
    available_cash = max(0.0, cash + sell_proceeds * SELL_PROCEEDS_HAIRCUT)

    candidates = sorted(
        (s for s in signals if s.symbol not in held and s.signal == "CROSS_UP"),
        key=lambda s: dollar_volume.get(s.symbol, 0.0),
        reverse=True,
    )
    ranks = {sig.symbol: index + 1 for index, sig in enumerate(candidates)}

    # Slot capacity first, then cash: a candidate with no slot is declined as
    # FULL even if the account is flush, because the position limit is the
    # primary constraint DESIGN.md specifies.
    taken_symbols: set[str] = set()
    cash_declined_symbols: set[str] = set()
    spent = 0.0
    for sig in candidates[:free_slots]:
        if spent + notional_per_slot <= available_cash:
            taken_symbols.add(sig.symbol)
            spent += notional_per_slot
        else:
            cash_declined_symbols.add(sig.symbol)
    declined_symbols = {s.symbol for s in candidates[free_slots:]}

    def base(symbol: str, sig: CrossSignal | None) -> dict:
        common = {
            "dollar_volume": dollar_volume.get(symbol) if symbol in ranks else None,
            "tiebreak_rank": ranks.get(symbol),
            "available_cash": available_cash,
            "slots_free": free_slots,
        }
        if sig is None:
            return {
                "symbol": symbol,
                "day": None,
                "close": None,
                "sma": None,
                "prior_close": None,
                "prior_sma": None,
                **common,
            }
        return {
            "symbol": symbol,
            "day": sig.day,
            "close": sig.close,
            "sma": sig.sma,
            "prior_close": sig.prior_close,
            "prior_sma": sig.prior_sma,
            **common,
        }

    decisions: list[Decision] = []
    covered: set[str] = set()

    for sig in signals:
        covered.add(sig.symbol)
        row = base(sig.symbol, sig)
        if sig.symbol in sell_symbols:
            decisions.append(
                Decision(
                    **row,
                    action="SELL",
                    reason_code=sig.signal,
                    notional=None,
                    qty=holdings[sig.symbol].qty,
                )
            )
        elif sig.symbol in taken_symbols:
            decisions.append(
                Decision(
                    **row,
                    action="BUY",
                    reason_code=sig.signal,
                    notional=notional_per_slot,
                    qty=None,
                )
            )
        elif sig.symbol in declined_symbols:
            decisions.append(
                Decision(
                    **row,
                    action="BUY_DECLINED_FULL",
                    reason_code=sig.signal,
                    notional=None,
                    qty=None,
                )
            )
        elif sig.symbol in cash_declined_symbols:
            decisions.append(
                Decision(
                    **row,
                    action="BUY_DECLINED_CASH",
                    reason_code=sig.signal,
                    notional=None,
                    qty=None,
                )
            )
        elif sig.symbol in held:
            decisions.append(
                Decision(
                    **row, action="HOLD", reason_code=sig.signal, notional=None, qty=None
                )
            )
        else:
            decisions.append(
                Decision(
                    **row,
                    action="NO_ACTION",
                    reason_code=sig.signal,
                    notional=None,
                    qty=None,
                )
            )

    # A held symbol absent from `signals` entirely (e.g. dropped from the
    # universe, or its bar fetch failed outright) would otherwise vanish from
    # the record without ever being sold or explained.
    for symbol in held - covered:
        decisions.append(
            Decision(
                **base(symbol, None),
                action="HOLD",
                reason_code="NO_SIGNAL_DATA",
                notional=None,
                qty=None,
            )
        )

    return decisions
