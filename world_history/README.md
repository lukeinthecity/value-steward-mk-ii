# World-context history

Two kinds of file, because one file cannot be both safe and live.

**Here (`world_history/`) — tracked, immutable.** Snapshots. Never appended to,
never edited. This is the copy that survives a mistaken `reset` or `checkout`
on the working file.

**`data/world_context.jsonl` — gitignored, live.** What the collector appends
to. It is the working series; it is not a backup of anything.

VS1 kept only the second kind. Its `data/world-context.jsonl` was tracked but
continuously rewritten by cron and last committed 2026-03-20, so by August the
real dataset was five months of uncommitted changes to a tracked file — one
careless command from gone, and in a repository where a routine `git reset
--hard` had already discarded live state once.

## What is in here

| File | Rows | Range | Status |
|---|---|---|---|
| `snapshot-YYYY-MM-DD.jsonl.gz` | varies | 2026-05-05 → | The working series. Re-snapshot occasionally. |
| `legacy-2026-01-23_2026-03-20.jsonl.gz` | 136 | 2026-01-23 → 2026-03-20 | **Pre-gap. Not part of the working series.** |

## The gap, and why the two are not merged

Collection stopped after **2026-03-20** and restarted on **2026-05-05**. The
six weeks between exist in neither file and are not recoverable: VS1's rotation
(`worldArtifactRotation.js`) archives `world-inbox` and `world-hydrated` only —
`world-context` was never archived.

The legacy block is kept because it costs nothing and otherwise exists solely
inside a git object of a repository being retired. It is *not* merged into the
working series: joining two runs across an unmarked six-week discontinuity
produces a series every later analysis must either special-case or silently
average over, and averaging across a hole is how a population gets misread.

Anything reading this data should use the working series alone unless it
handles the discontinuity deliberately.
