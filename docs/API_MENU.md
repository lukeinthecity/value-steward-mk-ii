# What VS2 can see and do through the Alpaca API

Everything available, in plain language, with terms explained. The point is to
choose from a known menu rather than accept whatever a default happens to be.

Verified against `alpaca-py 0.43.2` on 2026-08-07, against the live paper
account. Where something was tested rather than read from documentation, it says
so.

**Nothing here is a recommendation.** It is the list of what exists. What VS2
actually uses is the short list at the end.

---

## 1. Bars — the core price data

A **bar** is one time period summarised into five numbers, the standard unit of
price history. Written OHLCV.

| Field | Means |
|---|---|
| `open` | first traded price of the period |
| `high` | highest price during it |
| `low` | lowest |
| `close` | last traded price — **the number the crossover rule uses** |
| `volume` | shares traded |
| `trade_count` | how many separate trades made up that volume |
| `vwap` | volume-weighted average price: the average price paid, weighted by size. Closer to "what it actually cost people" than the midpoint of the range. |

Available timeframes run from 1 minute up to 1 month. The crossover rule uses
**daily** bars only.

### `adjustment` — the setting that must not be left alone

When a company splits its stock 10-for-1, every share becomes ten shares worth a
tenth as much. Nothing about the company changed, but the raw price falls ~90%
overnight.

| Value | Means |
|---|---|
| `raw` | **the API default.** Prices exactly as they traded — a split looks like a crash. |
| `split` | past prices rescaled so splits do not appear as moves |
| `dividend` | past prices adjusted for dividends paid |
| `all` | both |

**Tested on 2026-08-07** against NVDA's 10:1 split of 2024-06-10:

| Date | `raw` close | move | `all` close | move |
|---|---|---|---|---|
| 2024-06-07 | 1208.88 | −0.1% | 120.68 | −0.1% |
| 2024-06-10 | 121.79 | **−89.9%** | 121.58 | **+0.7%** |

The `raw` column is not a crash. It is arithmetic.

**This matters more to VS2 than it did to VS1.** A crossover rule compares price
to its own 50-day average. A split drives price ~90% below that average
instantly, producing a guaranteed false sell — and on a reverse split, a
guaranteed false buy. **VS2 must pass `adjustment=all` explicitly on every bar
request.**

VS1 never set it, so it ranked momentum, volatility and drawdown on raw prices
for its entire three-run history, and its 2-SD volatility stop would have read
every split in the universe as a catastrophe.

### `feed` — where the prices come from

| Value | Means |
|---|---|
| `sip` | the consolidated feed: every US exchange. Complete, but a paid entitlement. |
| `iex` | one exchange only (IEX). Free, but a small share of total volume, so thin names look thinner than they are. |
| `delayed_sip` | the complete feed, delayed 15 minutes. Free. |

VS1 worked around this by requesting data ending 16 minutes ago. **Worth
confirming which entitlement this account actually has before relying on
either.** For a rule that reads *daily closes*, the delay is irrelevant — by the
next morning yesterday's close is final on any feed.

---

## 2. Quotes, trades and snapshots — real-time microstructure

- **Quote** — the current best `bid` (highest price a buyer will pay) and `ask`
  (lowest a seller will accept), with sizes. The gap between them is the
  **spread**, effectively the cost of trading immediately.
- **Trade** — an individual execution: price, size, exchange, timestamp.
- **Snapshot** — a convenience bundle: latest trade, latest quote, current
  minute bar, today's daily bar, and yesterday's daily bar.

The crossover rule needs none of this. It is listed because "check the spread
before trading" is a plausible future mechanism, and because `previous_daily_bar`
in a snapshot is a cheap way to get yesterday's close.

---

## 3. Screener — tested, and not suitable for the universe

`get_most_actives(top, by)` where `by` is `volume` or `trades`.

**Tested 2026-08-07:** `top` is capped at **100** — requesting 500 returns
`invalid top: should not be larger than 100`. The top names returned were
`HCWC`, `YYAI`, `HTZ`, `SPCX`, `MSTU`.

Two problems for our purposes. It is capped far below the 500 names sketched in
`DESIGN.md`; and `by=volume` counts **shares, not dollars**, so it is dominated
by very cheap stocks and leveraged ETFs. A $2 stock trading 50M shares outranks a
$400 stock trading 5M, despite being a twentieth the money.

There is also `get_market_movers` for biggest gainers and losers. Same cap.

**Conclusion: the universe cannot come from the screener.** See open question 1
below for where it comes from instead.

---

## 4. Assets — what is tradable at all

`get_all_assets()` returns every instrument, each with: `symbol`, `name`,
`exchange`, `tradable`, `fractionable` (can you buy a fraction of a share),
`shortable`, `easy_to_borrow`, `status` (active/inactive), and
`maintenance_margin_requirement`.

This is the honest starting universe: several thousand names. VS1 filtered it
with a name-pattern blocklist for SPACs, warrants and buffer ETFs.

---

## 5. Corporate actions

`CorporateActionsRequest` returns splits, dividends, mergers, spin-offs and name
changes over a date range.

Mostly made unnecessary by using `adjustment=all`. Worth knowing it exists,
because it is how you would *detect* a split rather than merely neutralise it —
relevant if VS2 ever wants to skip trading a name around a corporate event.

---

## 6. News

`NewsRequest` returns headlines with `symbols`, `headline`, `summary`, `source`,
`created_at`, and optionally full article content.

Relevant only to the deferred world-state gating, and worth noting because VS1
built an entire RSS ingestion layer in Node to get something similar. If that
mechanism is ever revisited, compare this against rebuilding that.

---

## 7. Account

Includes `equity` (total value), `cash`, `buying_power`, `portfolio_value`,
`last_equity` (yesterday's close — the honest denominator for "how are we doing
today"), `long_market_value`, `daytrade_count`, and the blocking flags
`trading_blocked`, `account_blocked`, `trade_suspended_by_user`.

`daytrade_count` is worth knowing: buying and selling the same stock on the same
day is a **day trade**, and an account under $25,000 is limited to three in five
business days. This paper account is at $100k, so it does not currently bind —
but a crossover rule that buys and sells on consecutive signals could bump into
it at smaller sizes.

## 8. Positions

Per position: `symbol`, `qty`, `qty_available`, `avg_entry_price`,
`market_value`, `cost_basis`, `unrealized_pl` and `unrealized_plpc` (profit or
loss, absolute and as a percentage), `current_price`, `lastday_price`, and
`change_today`.

Note `unrealized_plpc` and `change_today` are computed by the broker. VS2 does
not need to derive them.

---

## 9. Orders — including the one that changes the vol-stop story

| Type | Means |
|---|---|
| **Market** | buy or sell now at whatever the current price is. Certain to execute, uncertain price. |
| **Limit** | execute only at my price or better. Certain price, uncertain execution. |
| **Stop** | *becomes* a market order once price passes a trigger. This is a stop-loss. |
| **Stop-limit** | becomes a limit order at the trigger. Protects against a terrible fill, at the risk of no fill. |
| **Trailing stop** | a stop whose trigger follows the price up, staying a set distance or percentage below the peak. Locks in gains without a fixed exit price. |

**This is the structural answer to VS1's worst defect.** VS1 implemented its
volatility stop as *code that had to run*: a tick had to fire, reach
`execute_intent`, and pass every gate — and it was blocked by both the execution
window and the daily-loss circuit breaker, as confirmed in
[`VS1_MECHANISM_NOTES.md`](VS1_MECHANISM_NOTES.md) entry 9.

A **stop order lives at the broker.** Once placed it executes whether or not our
code runs, whether or not the machine is on, whether or not a guard would have
blocked it. Every outage VS1 had — the Windows Update reboot, the storm weekend —
left its positions genuinely unprotected. A resting stop order would not have
been.

That is a real design choice for VS2 and not a foregone one: a resting stop can
be triggered by a brief intraday spike that a daily-close rule would ignore, so
it can take you out of a position the rule still wants. It is listed here as a
choice to make deliberately.

Also available: `close_position` (sell one holding) and `close_all_positions`.

## 10. Calendar and clock

`get_calendar(start, end)` returns each trading day with its actual `open` and
`close` times — including **early closes** such as the 1pm finish before
Thanksgiving.

`get_clock()` returns `is_open`, `next_open`, `next_close`.

**VS1 hand-wrote 241 lines of holiday and early-close logic** in
`market_holidays.py`, and one of its audits found a missed early close. The
broker publishes this. VS2 should ask rather than encode.

---

## What the crossover rule actually needs

The whole menu above, reduced to what the rule in `DESIGN.md` requires:

1. The Wikipedia constituent fetch (`src/vs2/data/index_membership.py`) —
   monthly, to refresh the universe
2. Daily bars, **`adjustment=all`**, 51 days back — the only recurring Alpaca
   data call
3. `get_calendar()` — to know which days are trading days
4. Account `equity` — to size positions
5. Positions — to know what is held
6. Market orders — to buy and sell

Six items. Everything else on this menu is a future decision, to be added only
with a stated reason.

---

## Open questions this menu raises

1. ~~**Where does the universe come from?**~~ **Resolved 2026-08-07: the real
   S&P 500, scraped from Wikipedia's constituent table.** The screener was out
   (capped at 100, share-volume biased, returned penny stocks). Third-party
   sources were surveyed and tested live: the [Nasdaq/NYSE symbol
   directory](https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt) and
   [SEC `company_tickers.json`](https://www.sec.gov/files/company_tickers.json)
   both work but carry no index membership or ranking; iShares publishes IVV's
   real holdings (literally the S&P 500) but the CSV endpoint returns a bot
   challenge page, not data, so it isn't scriptable. Wikipedia's table parsed
   cleanly and all 503 symbols were cross-checked against Alpaca's asset list:
   every one is tradable and active, including the two dotted tickers `BRK.B`
   and `BF.B`, with no reformatting needed. Implemented in
   `src/vs2/data/index_membership.py`, tested in
   `tests/test_index_membership.py` against real trimmed fixtures of the actual
   table markup.

   **Narrowed 2026-08-08 to the Dow 30.** A capacity measurement found that at
   503 names a 20-slot portfolio could act on only 8.1% of the rule's cross-up
   signals, so the dollar-volume tiebreak rather than the crossover was
   selecting the portfolio. The Dow 30 acts on 91.5%, and its membership is
   externally published. Both pages expose the same `id="constituents"` table,
   so one parser serves both and the S&P URL is retained for analysis. See
   `DESIGN.md`, "Sizing the universe to the signal".
2. ~~**Which data feed does this account have?**~~ **Resolved 2026-08-07: SIP.**
   Tested directly: `sip` and `iex` both return data, `delayed_sip` is rejected
   as an invalid feed, and an unset `feed` matches `sip` exactly (AAPL close
   311.00 vs IEX's 310.94), so SIP is already this account's default. Note SIP
   cannot be queried close to real time — a request ending at the current
   instant returns `subscription does not permit querying recent SIP data` —
   which is why `bars.py` keeps a 16-minute buffer. Irrelevant to a
   daily-close rule, but now known rather than assumed.
3. ~~**Resting stop orders, or none at all?**~~ **Resolved 2026-08-08: no
   stop, and market orders for entries.** Both measured against the Dow 30
   specifically — see `DESIGN.md`, "Why market orders" and "Why there is no
   resting stop order". A stop would fire prematurely (price recovers before
   the rule's own exit) on 31–50% of fires at every level tested, against
   only 2.2% of holdings ever reaching a 10% drawdown at all. A limit order
   would save at most the 2.55bp measured spread while risking VS1's measured
   40% fill rate on a universe already built to act on 91.5% of its signals.
