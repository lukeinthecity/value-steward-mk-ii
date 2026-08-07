# Value Steward 2

A paper-trading agent built around a 50-day moving-average crossover and nothing
else.

Buy when a symbol's close crosses above its own 50-day simple moving average;
sell when it crosses back below. There is no score, no factor weighting, and no
second exit condition.

The purpose is to get one readable answer: does a fully specified, externally
documented rule beat a buy-and-hold benchmark on the same universe over the same
dates? Measurement is designed before the strategy and tested against fixtures
that model production rows.

Read [`DESIGN.md`](DESIGN.md) for the rule, the parameters, and the list of
things deliberately left out.

Value Steward 1, at `../value-steward`, was retired from trading on 2026-08-07
after three runs. VS2 inherits its Alpaca paper account. VS1's world-context
pipeline keeps running, because it contacts no broker and builds the dataset
VS2 needs for the world-state gating it has deferred.

## Status

Design stage. No trading code yet.

## Trading

Alpaca **paper** account only.
