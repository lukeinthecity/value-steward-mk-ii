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
[`docs/CODE_CHECK_PLAYBOOK.md`](docs/CODE_CHECK_PLAYBOOK.md) is an audit
procedure for an independent session to verify this codebase, built from the
specific bugs that have actually occurred here — read it before running or
reviewing anything, and see its "Known open findings" for what isn't fixed
yet. `CLAUDE.md` points a fresh Claude Code session at all of the above
automatically.

## What the first iteration taught

[`docs/VS1_MECHANISM_NOTES.md`](docs/VS1_MECHANISM_NOTES.md) catalogues eleven
mechanisms from the first system: what each was for, what happened to it, and
the structural rule that follows. It ends with a checklist to run before adding
any mechanism here. It is the main reason the first repository is worth keeping.

The first iteration was retired from trading on 2026-08-07 after three runs
(2026-05-18 to 2026-08-07, 82 calendar days), having never produced a readable
answer about whether its strategy worked. Its world-context pipeline outlived
that retirement, because it contacts no broker — but its dataset is now being
brought into VS2 and gathered here natively; see "World state" below. Nothing
in `crontab -l` schedules it as of 2026-08-09, so any claim that it is "still
running" needs a timestamp behind it before it is repeated.

## Status

The pipeline and its measurement are both built: universe, daily bars, trading
calendar, account state, 50-day crossover detection, decision-making, order
submission, fill reconciliation, and the run review. `python -m vs2.run_daily`
computes and logs decisions; add `--execute` to submit orders. `python -m
vs2.report` produces the review.

**`--execute` has never been passed against this account.** The crontab is
installed dry-run only — see `crontab` for the template, and `crontab -l` for
what is actually scheduled, which is the only source of truth.

Deciding and executing are separate phases, because the rule needs a finished
close and one does not exist until the market shuts. Decisions are computed
after the close and submitted during the *next* session, at the first of
several intraday attempts — see DESIGN.md, "When orders are actually placed".
Only a session whose close has passed is ever evaluated; bars that fail to
reach it are refused rather than traded as if current.

Four things guard the write path. Order submission does not retry on failure (a
write is not safe to retry blindly, unlike every read here). A decision day's
execution attempt — success, failure, or partial — is tracked separately from
whether decisions were merely computed. The whole cycle runs under a
single-instance lock, verified against a genuinely separate OS process. And
every order carries a deterministic `client_order_id`, so a duplicate
submission is rejected by the broker rather than relying on our own bookkeeping
being perfect.

Four append-only logs under `data/` are what the run is measured from:
`decisions.jsonl` (intent), `executions.jsonl` (what was attempted),
`fills.jsonl` (what it became, and realized slippage), and `sessions.jsonl`
(equity and exposure per session, plus any session that was skipped and why).

## Notifications

Two push notifications per trading day, via [ntfy](https://ntfy.sh) — an
"open" one on the first in-session tick, and a "close" one once the day is
decided. Order failures, missed sessions and stale bars lead the message and
raise its priority, because the part you would act on should be the part you
read first.

Configured with the **same environment variables value-steward uses**
(`VS_NTFY_TOPIC`, `VS_NTFY_SERVER`, `VS_NTFY_TOKEN`, `VS_PUSH_ENABLED`), so the
`.env` copied across for the Alpaca credentials points both systems at one
topic and one phone subscription — see `.env.example`. Confirm the wiring with
`python -m vs2.push_test` (the equivalent of VS1's `npm run push:test`) before
relying on it. Unset `VS_NTFY_TOPIC` and pushes are simply
off — not an error. The topic is a secret and lives only in the gitignored
`.env`; it is never logged, never in an error message, and never committed.

A push is observability, never a control-path step: sending cannot raise, so a
dead notification channel can never affect a cycle that is about to place
orders. Every attempt is recorded in `data/pushes.jsonl`, because you cannot
rely on the alert channel to tell you the alert channel is broken.

## Health checks

`python -m vs2.health` reads the append-only logs from outside a cycle and
reports what a run cannot report about itself. `run_daily` announces a failed
order or a missed session; it cannot announce never having started, because a
process that does not start sends nothing. That is not hypothetical — the
first dry-run week lost a session to a VM that shut down with its terminal,
and the only reason it was noticed is that somebody went looking.

Eight checks, each traceable to a defect that actually occurred: a stalled
run, missed sessions, either once-per-day guard failing to converge, a day
that decided fewer than the full universe, a null exposure figure, stale bars,
and a failed order. Silence is the healthy outcome — findings push at priority
4 and exit non-zero so `MAILTO` backs the push, while a daily "all is well"
notification would be swiped away unread within a week.

It is a reader: no lock, no writes to the four logs, no import from
`vs2.core`, and it cannot reach a trading decision.

**It cannot detect its own host being down.** If the VM stops, this does not
run either, and `RUN_STALLED` reports the outage only once the machine is
back. The live signal for a dead host is the absence of the two daily pushes.
Closing that properly needs an off-box dead man's switch, which is not built
here.

## World state

`src/vs2/world/` collects the world-context dataset DESIGN.md names as the
input to its deferred second mechanism. **Collection is not that mechanism** —
it feeds no decision and cannot suppress a buy, and the gate is still deferred
until the crossover baseline produces a readable result. The boundary is
asserted against the real import graph in `tests/test_world_isolation.py`
rather than left to convention.

`python -m vs2.world.cli` imports the history from value-steward — run and
archived 2026-08-09. The working series is 939 rows, 2026-05-05 to 2026-08-07,
verified genuine before import (every row's `generated_at` falls on its own
`date`, across 82 distinct generation days — a batch-written block cannot
produce that). An earlier 136-row block (2026-01-23 to 2026-03-20) is archived
separately: a permanent six-week gap separates the two, and merging across it
would produce a series later analysis must silently average over. Both live in
[`world_history/`](world_history/README.md), tracked and immutable.

## Trading

Alpaca **paper** account only.
