# value-steward-mk-ii

Second iteration of Value Steward, rebooted from first principles after three
test runs turned up critical instrumentation errors.

A paper-trading agent built around a 50-day moving-average crossover and nothing
else. Buy when a symbol's close crosses above its own 50-day simple moving
average; sell when it crosses back below. There is no score, no factor
weighting, and no second exit condition.

The purpose is to get one readable answer: does a fully specified, externally
documented rule beat a buy-and-hold benchmark on the same universe over the same
dates? Measurement is designed before the strategy and tested against fixtures
that model production rows.

Read [`DESIGN.md`](DESIGN.md) for the rule, the parameters, and the list of
things deliberately left out. [`docs/API_MENU.md`](docs/API_MENU.md) records
what the broker actually offers and which of it this system uses.

## What the first iteration taught

[`docs/VS1_MECHANISM_NOTES.md`](docs/VS1_MECHANISM_NOTES.md) catalogues eleven
mechanisms from the first system: what each was for, what happened to it, and
the structural rule that follows. It ends with a checklist to run before adding
any mechanism here. It is the main reason the first repository is worth keeping.

The first iteration was retired from trading on 2026-08-07 after three runs
(2026-05-18 to 2026-08-07, 82 calendar days), having never produced a readable
answer about whether its strategy worked. Its world-context pipeline still
runs, because it contacts no broker and builds a dataset this system will need
later.

## Status

I/O layer and decision logic built, both verified against the live paper
account: universe, daily bars, trading calendar, account equity, positions, and
50-day crossover detection. **No order placement** — nothing here can currently
trade.

## Trading

Alpaca **paper** account only.
