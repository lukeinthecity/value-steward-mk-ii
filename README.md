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

This is a separate track from Value Steward 1, which remains live and unchanged
at `../value-steward`.

## Status

Design stage. No trading code yet.

## Trading

Alpaca **paper** account only.
