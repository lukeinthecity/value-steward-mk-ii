# Value Steward 2 — design

## What this is

A paper-trading agent built around one published, well-known trading rule and
nothing else. It exists to answer a question the first system never answered in
three runs: **does a simple, fully specified rule produce excess return over a
benchmark, and can we measure that correctly?**

Value Steward 1 remains live and unchanged. VS2 is a separate track, not a
replacement.

## Account separation

**VS2 runs on its own Alpaca paper account, with its own API keys.** VS1 keeps
the existing account. This is a requirement, not a preference.

Both systems read positions from the broker rather than from their own ledger —
VS1 does so at `src/valuesteward/data/alpaca_client.py:128` via
`get_all_positions()`. On a shared account each system would therefore treat the
other's holdings as its own, with two concrete consequences:

- VS1's volatility stop iterates over account positions and would sell VS2's
  holdings.
- VS1's `risk_exposure_pct` would include VS2's market value, read the account
  as fully deployed, and stop buying.

The results of both runs would be uninterpretable. Alpaca supports multiple
paper accounts under one login, each with separate keys, so the separation costs
nothing beyond generating a second key pair.

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
| Universe | top 500 by trailing dollar volume, snapshotted to `config/universe.txt` and refreshed monthly | Computed from the same daily bars the rule reads, so it is reproducible without an external index source. |
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
