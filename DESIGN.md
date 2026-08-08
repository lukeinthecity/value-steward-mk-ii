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
| Universe | the **Dow 30**, snapshotted to `config/universe.txt` from Wikipedia's constituent table (`src/vs2/data/index_membership.py`), refreshed **by hand between runs, never during one** | Externally published membership, and sized so the rule's signals fit the position limit -- see "Sizing the universe to the signal" below. Verified 2026-08-08: all 30 symbols are tradable, active and fractionable on Alpaca. A universe that shifts mid-run moves the benchmark under the measurement, which is worse than a stale constituent for sixty sessions. |
| Maximum concurrent positions | 20 | Bounds how many crosses can be acted on. |
| Position size | equal weight, full account across the position limit | Roughly 5% of equity each. No conviction sizing — conviction would be a second mechanism. |
| Cash ceiling | a day's buys are capped at settled cash | Sizing divides *equity*; buys are paid from *cash*. See "Why buys are capped at cash" below. |
| Decision cadence | once per trading day, on a completed close | |
| Execution cadence | during the **following** session, at the first of several intraday attempts | The rule needs a finished close, so an order cannot be placed on the day it is decided. See "When orders are actually placed" below. |
| Order type | **market** | Guaranteed execution. At the measured 2.55bp Dow spread and 12.3× turnover this costs ~0.31%/yr; a limit order saves at most that and, at VS1's measured 40% fill rate, would discard ~60% of signals. See "Why market orders" below. |
| Stop loss | none | The cross-down is the exit. See "Why there is no resting stop order" below. |
| Maximum holding period | none | Same reason. |

### The oversubscription problem

On a day when more symbols cross up than there are open position slots, a choice
has to be made, and any choice is an additional rule. The tiebreak is **trailing
dollar volume, highest first** — chosen because it is a liquidity and
fill-quality property, not a prediction about return. Each candidate's
`dollar_volume` and its `tiebreak_rank` that day are written onto its decision
row, so the tiebreak's effect can be measured separately from the crossover
rule itself — without those two fields on the row, there is no way to
reconstruct afterwards why one candidate was bought and another declined.

If oversubscription turns out to be frequent rather than occasional, the
universe is too large for the position limit and the universe should shrink.
Shrinking the universe is preferable to making the tiebreak smarter.

### Sizing the universe to the signal

**Measured 2026-08-08**, running the production `detect_cross` over 451 trading
days (2024-10-18 to 2026-08-07) of split/dividend-adjusted daily bars. This is a
capacity study: it computes no returns and makes no claim about profitability,
only about whether a 20-slot portfolio can express the rule's own signals.

| Universe | Cross-up signals | Acted on | Declined, full |
|---|---|---|---|
| S&P 500 (503 names) | 8,279 | **8.1%** | 91.9% |
| **Dow 30** | 482 | **91.5%** | 8.5% |
| Top 40 by dollar volume | 609 | 80.6% | 19.4% |

At 503 names the rule produced roughly twelve times more signals than 20 slots
could hold, so **the dollar-volume tiebreak, not the crossover, was selecting
the portfolio** — and the tiebreak was chosen precisely because it is *not* a
prediction. The system would have been testing "do the highest-dollar-volume
names that recently crossed beat the benchmark," which is a different question
from the one this design asks.

The Dow 30 was chosen over a self-defined top-40 list on two grounds: it acts on
more of its signals (91.5% vs 80.6%), and its membership is externally published
rather than invented here — the same reason the crossover rule itself was
chosen. A smaller universe is a narrower test, and that is the accepted cost.

Supporting figures, same run: median holding period **4 trading days** (mean
13.5, max 277 — price oscillating around its own average crosses it
repeatedly), and implied turnover **12.3× per year** for the Dow 30.

**Spread cost, measured against the Dow 30 specifically (2026-08-08).** Median
per-symbol spread is **2.55bp** (AAPL 0.64, WMT 0.90, NVDA 0.90 at the tight
end; UNH 7.59, CAT 9.70, BA 10.34 at the wide end), from consolidated SIP
quotes during the 2026-08-07 session. A round trip crosses the full spread, so
12.3× turnover implies roughly **0.31% of the account per year**, before any
question of edge.

An earlier draft of this section quoted 8.52bp and ~1.05%/yr. That figure came
from a 60-name sample spanning the whole S&P 500, which includes mid-caps, and
was carried over unchanged when the universe narrowed. The Dow 30 is entirely
mega-cap and trades roughly 3.3× tighter. Re-measuring against the configured
universe rather than inheriting a number is the general lesson.

### Partial investment is the strategy, not a defect

Same run: with the Dow 30 and 20 slots, **the portfolio holds 14.2 names on
average** (median 16, and full at 20 on only 58 of 451 days). At 5% of equity
per slot that leaves roughly **29% of the account in cash on average**.

This is what a timing strategy does — being out of the market *is* the signal —
but it is a measurement hazard worth stating before any result is read. Against
a 100%-invested benchmark, a partially-invested strategy underperforms in a
rising market regardless of whether its timing is any good, and outperforms in a
falling one for the same reason. **A verdict therefore depends on which regime
the run samples**, and a short run in a trending market will mislead in a
predictable direction. The Day-60 review must report average invested exposure
alongside return, or the comparison is not readable.

## When orders are actually placed

The rule reads a **finished** close. A close does not exist until the bell
rings. So a signal detected on day *t* cannot be acted on during day *t* — the
earliest possible execution is the following session. This is arithmetic, not
a policy choice, and every measurement below is written to match it.

What *is* a choice is what happens in that following session. A market order
left queued overnight fills at the opening auction, which is the least
predictable price of the day and one nobody can see in advance. Instead the
cron fires repeatedly through the next session and submits at the first tick,
during liquid regular hours, at a price a human could have watched. Later ticks
that day are no-ops.

```
Mon 16:15   decide Monday from Monday's close      (market shut, nothing sent)
Tue 09:45   submit Monday's decisions              (market open)
Tue 16:15   decide Tuesday from Tuesday's close
Wed 09:45   submit Tuesday's decisions, reconcile Monday's fills
```

Two consequences the review has to respect. Execution is **one session behind**
the decision, so the benchmark enters at the same next-session open rather than
at the decision close — entering it at the close would hand it an overnight gap
the strategy never got. And a decision is *replayed* from the log at execution
time, never recomputed: by Tuesday morning the portfolio has already changed,
and a recompute would quietly produce different orders from the ones recorded.

Only a session whose close has passed is ever evaluated. During market hours
Alpaca serves a daily bar for the session in progress, whose "close" is just
the last trade so far; it is dropped before the rule sees it. Bars that fail to
reach the completed session are refused as `STALE_BARS` and nothing is decided
— an old close is never silently traded as a current one.

## Why buys are capped at cash

Equal-weight sizing divides **equity** by the slot count, but buys are paid for
out of **cash**, and the two diverge as soon as holdings appreciate: each held
slot is then worth more than `equity / slots`, so filling every free slot costs
more than the account has. Concretely, 14 holdings up 20% gives equity of
$114,000, a per-slot notional of $5,700, and six free slots wanting $34,200
against $30,000 of cash — short by $4,200.

Those buys used to be submitted anyway and rejected by the broker. That is the
wrong shape of failure: a knowable capacity limit showed up as an execution
fault, and the signal was lost — the very leak "Why market orders" exists to
prevent. Buys are now taken in tiebreak order until cash runs out, and the
remainder are recorded as `BUY_DECLINED_CASH`, kept distinct from
`BUY_DECLINED_FULL`. The two mean different things: *full* says the universe is
too large for the position limit, *cash* says equal-weight sizing has outrun the
account. Only the first is an argument for shrinking the universe.

Same-day sell proceeds count toward the ceiling at a 5% haircut, since a sell is
qty-denominated and its proceeds depend on a fill price that does not exist yet.
Settled cash is used rather than `buying_power`, which on a margin account
includes borrowed money — sizing against it would quietly run the test on
leverage.

## Measurement

Measurement is specified before the system is built, and its correctness is
tested against fixtures that model real production rows rather than
hand-written ideal ones. Both of those are direct responses to VS1's Run 2 and
Run 3, which were abandoned for measurement faults rather than for results.

- Every decision writes one row: symbol, date, action, reason code, price,
  `sma50`, the prior day's values that established the cross, and the capacity
  context it was decided under (`dollar_volume`, `tiebreak_rank`,
  `available_cash`, `slots_free`).
- One row per symbol per decision day. The daily cadence removes the duplicate
  rows that VS1 produced from four intraday slots. Note the many intraday cron
  ticks are *execution* attempts, not decision slots — the distinction is the
  whole reason VS1's 214 scorecard rows collapsed to 104 real ones.
- Each session writes one row of account state — equity, cash, per-position
  market value — to `data/sessions.jsonl`. Without it, average invested
  exposure cannot be computed at all, and cannot be reconstructed afterwards.
- Each order is read back after the fact into `data/fills.jsonl`: status, fill
  price, and realized slippage against the decision close.
- The benchmark is a buy-and-hold position in the same universe, computed over
  the same dates from the same bars, entered at the same next-session open the
  strategy trades at.
- The headline number is the return of positions actually taken versus that
  benchmark. Positions not taken are reported separately and never averaged into
  the headline.

`python -m vs2.report` produces the review from those four logs. It reports
**why it is not readable** as prominently as any return figure: missed
sessions, stale-bar days, missing exposure rows, signals that do not reconcile,
and intended buys that never became fills each suppress the verdict rather than
being averaged over. A run that cannot be read should say so — that is the
single thing VS1's three runs never did.

## Why market orders

A limit order buys a better price at the cost of uncertain execution. VS1 used
mid-point limit orders ("fishing") and **filled 6 of 15 attempts — 40%**. Sixty
percent of its intended trades simply never happened.

That trade-off is decisive here, and against the measured numbers it is not
close:

- Crossing the spread costs **2.55bp** median on this universe, or roughly
  **0.31% per year** at 12.3× turnover. That is the entire saving a limit order
  could capture, and only if it always filled.
- A 40% fill rate applied to a universe that already acts on 91.5% of its
  signals would drop effective signal capture to about **37%** — discarding
  most of the benefit the Dow 30 narrowing was chosen to gain.

The measurement argument is stronger than the cost one. An unfilled limit order
is a decision that never becomes data. VS1's central problem was never having
enough decisions to measure anything, and its execution layer was quietly
destroying more than half of them. **Market orders guarantee that every signal
becomes a recorded outcome**, which is what the run has to produce to be worth
running at all.

Realized slippage against the decision-day close is recorded per fill
(`src/vs2/data/fills.py` → `data/fills.jsonl`), so the 0.31% estimate can be
checked against reality rather than assumed. Signed so that positive always
means worse than the close, on both sides. That is an observation, not a
mechanism — it changes no behavior.

Note the fill happens in the session *after* the close it is measured against,
so this figure is spread plus overnight gap, not spread alone. It is still the
right number to check, because it is the cost the strategy actually pays; it is
simply not comparable to a quoted spread without saying so.

## Why there is no resting stop order

Alpaca supports broker-side stop, stop-limit and trailing-stop orders. A resting
stop is genuinely attractive for one reason: it lives at the broker and executes
whether or not our code runs, so it survives an outage. VS1 lost 6 of 60
weekdays to outages, during which its positions were entirely unprotected
because its volatility stop was code that had to run — and was additionally
blocked by two of its own gates (see
[`VS1_MECHANISM_NOTES.md`](docs/VS1_MECHANISM_NOTES.md) entry 9).

**Measured 2026-08-08** across 459 completed holdings on the Dow 30. A stop
fires on an intraday low; the crossover rule exits only on a daily close, so the
two disagree whenever price dips and recovers.

| Stop | Fires | % of holdings | Of those, the rule's own exit was better |
|---|---|---|---|
| 3% | 126 | 27.5% | 51 (40%) |
| 5% | 42 | 9.2% | 16 (38%) |
| 8% | 16 | 3.5% | 5 (31%) |
| 10% | 10 | 2.2% | 5 (50%) |

"The rule's own exit was better" means the stop fired intraday, but by the time
the crossover actually exited, price had recovered above the stop level — so the
stop sold lower than waiting would have.

**Between 31% and 50% of stop fires would be premature at every level tested.**
That is not a protective mechanism behaving reliably; it is close to a coin flip
that sometimes makes the outcome worse.

The same table also bounds the risk of *not* having one: only **2.2% of holdings
ever fell 10% below entry**, so deep adverse moves within a holding are rare.
Note this measures frequency, not tail magnitude — it does not rule out a single
catastrophic gap, and a genuine crash would be ridden down until the close
crossed below the average.

The decision is **no stop for the baseline run**, on three grounds. It is a
second exit condition, which this design exists to avoid. It fires prematurely
a third to half the time it fires at all. And the outage argument, while real,
is an operations problem — solving it with a price mechanism conflates
reliability with strategy, and would leave the run unable to say which one
produced the result.

Revisit at the post-run review, when the exit rule itself has been measured.

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
