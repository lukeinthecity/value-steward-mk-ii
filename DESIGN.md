# Value Steward mk II — design

## What this is

A paper-trading agent built around one published, well-known trading rule and
nothing else. It exists to answer a question the first system never answered in
three runs: **does a simple, fully specified rule produce excess return over a
benchmark, and can we measure that correctly?**

Value Steward 1 was retired from trading on 2026-08-07 after three runs. Its
planned Run 4 was cancelled rather than started, on the grounds that it would
have spent sixty trading days generating data from an instrument already judged
untrustworthy. VS2 inherits its Alpaca paper account.

## Account

VS2 uses the existing Alpaca paper account, flat and fully in cash at handover:
0 positions and $100,023.49 as of 2026-08-07, after VS1's positions were
liquidated.

**Exactly one system may trade this account.** Both read positions from the
broker rather than from their own ledger — VS1 does so at
`src/valuesteward/data/alpaca_client.py:128` via `get_all_positions()` — so a
second trading system on the same account would be read as this one's own
holdings, and vice versa:

- VS1's volatility stop iterates over account positions and would sell VS2's
  holdings.
- VS1's `risk_exposure_pct` would include VS2's market value, read the account
  as fully deployed, and stop buying.

VS1 is held off the account by three independent measures: its trading cron jobs
are removed, `trading_enabled` is false, and `force_no_trade` is true. Alpaca
does support multiple paper accounts per login with separate keys, which is the
route to take if VS1 is ever restarted rather than sharing this one.

## The rule

**50-day moving-average crossover.**

- **Buy** a symbol when its closing price crosses from below to above its own
  50-day simple moving average.
- **Sell** a held symbol when its closing price crosses from above to below that
  same average.

That is the entire decision logic. There is no score, no weighting, no blend of
factors, and no second exit condition. The cross down *is* the exit — adding a
stop-loss or a maximum holding period would make the system test two mechanisms
at once, which is the specific failure this design is reacting to.

The rule was chosen because it is externally documented and pre-dates this
project. Nothing about it was invented here, so a poor result reflects on the
rule or on the implementation, not on an untested idea that arrived with no
provenance.

### Cross detection

A cross-up on day *t* requires both:

```
close[t-1] <= sma50[t-1]
close[t]    > sma50[t]
```

A cross-down is the same comparison inverted. This needs 51 daily bars per
symbol. A symbol with fewer than 51 bars is skipped and the skip is logged with
a reason code — it is not treated as "no cross."

## Deliberate exclusions

Each of these was present in VS1 and is left out here on purpose.

| Excluded | Why |
|---|---|
| Signal weight training | There is no score to train weights for. |
| Champion-challenger | Nothing mutates the policy, so nothing needs rollback. |
| Score-gate posteriors, Thompson sampling | No gates. |
| Pattern library, realized-alpha and persistence nudges | Score adjustments to a score that no longer exists. |
| Correlation matrix | Not needed by the rule. This also removes the O(n²) step that forced VS1's universe cap. |
| Intraday execution slots | The rule reads daily closes, so it decides once per day. |
| World-state gating | Deferred by decision. See "Planned second mechanism". |

## Parameters

These are the starting values. They are configuration, not code, and each one is
a number a reader can check by hand against a price chart.

| Parameter | Starting value | Note |
|---|---|---|
| Moving-average window | 50 trading days | The published convention. |
| Universe | the S&P 500, snapshotted to `config/universe.txt` and refreshed monthly from Wikipedia's constituent table (`src/vs2/data/sp500.py`) | Real index membership rather than a liquidity proxy for it. Verified 2026-08-07: all 503 parsed symbols, including the two dotted tickers `BRK.B`/`BF.B`, match a tradable, active Alpaca asset with no reformatting -- see `docs/API_MENU.md`. |
| Maximum concurrent positions | 20 | Bounds how many crosses can be acted on. |
| Position size | equal weight, full account across the position limit | Roughly 5% of equity each. No conviction sizing — conviction would be a second mechanism. |
| Decision cadence | once per trading day | |
| Stop loss | none | The cross-down is the exit. |
| Maximum holding period | none | Same reason. |

### The oversubscription problem

On a day when more symbols cross up than there are open position slots, a choice
has to be made, and any choice is an additional rule. The tiebreak is **trailing
dollar volume, highest first** — chosen because it is a liquidity and
fill-quality property, not a prediction about return. It is recorded in the
decision log as a tiebreak so its effect can be measured separately from the
crossover rule itself.

If oversubscription turns out to be frequent rather than occasional, the
universe is too large for the position limit and the universe should shrink.
Shrinking the universe is preferable to making the tiebreak smarter.

## Measurement

Measurement is specified before the system is built, and its correctness is
tested against fixtures that model real production rows rather than
hand-written ideal ones. Both of those are direct responses to VS1's Run 2 and
Run 3, which were abandoned for measurement faults rather than for results.

- Every decision writes one row: symbol, date, action, reason code, price,
  `sma50`, and the prior day's values that established the cross.
- One row per symbol per decision day. The daily cadence removes the duplicate
  rows that VS1 produced from four intraday slots.
- The benchmark is a buy-and-hold position in the same universe, computed over
  the same dates from the same bars.
- The headline number is the return of positions actually taken versus that
  benchmark. Positions not taken are reported separately and never averaged into
  the headline.

## Planned second mechanism (not built yet)

World-state gating — permitting the analysis layer to suppress buying on
identified risk conditions — is the intended next step, and only after the
crossover baseline has produced a readable result. It enters as a yes/no vote on
whether to act, never as a weight on a score, so its effect stays separable.

Adding it before the baseline reads would repeat VS1's central mistake:
introducing a second mechanism before the first one was measured.

VS1's world-context pipeline stays running for exactly this reason. It contacts
no broker, so it is unaffected by the trading retirement, and it continues to
accumulate the dataset this mechanism will need.

## What VS1 leaves behind

**A catalog of how to structure mechanisms**, in
[`docs/VS1_MECHANISM_NOTES.md`](docs/VS1_MECHANISM_NOTES.md). Eleven mechanisms,
what each was for, what happened to it, and the structural rule that follows —
plus a checklist to run before adding any mechanism here. This is the reason to
keep the VS1 repository, and it should be read before VS2 gains its second
mechanism.

**A world-context history.** 929 context rows and 9,046 hydrated items,
accumulated daily and still growing, contacting no broker. This is the direct
input to the deferred gating mechanism above.

**Not a trading dataset.** 214 scorecard rows across three runs, inflated by
duplication from four intraday execution slots. Keyed on `(symbol,
entry_date)`, those collapse to **104 unique decisions** (29 / 45 / 30) across
2026-05-18 to 2026-08-07, 82 calendar days. That is a set of worked examples to
read, not a population to compute over. Any statistic from those rows has to be
recomputed — the raw observations are sound, but the metrics built on them came
from the measurement faults that ended runs 2 and 3.

## Stack

Python only. VS1's Node.js half exists for the world-context layer, which is
deferred, so VS2 has no Node dependency, no second test runner, and no second
lint configuration.

Reused from VS1 by direct port, because it is I/O that already works against
the live account and carries operational knowledge that took real incidents to
acquire:

- Alpaca client wrapper, including its retry and backoff behavior
- Daily-bar fetching, including the 16-minute SIP delay allowance
- The market-holiday and early-close calendar

Everything else is written fresh.
