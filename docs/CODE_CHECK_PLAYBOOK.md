# Code Check Playbook

An audit procedure for an independent session to verify this codebase, built
from the specific classes of bugs that have actually occurred here and in
value-steward (the predecessor). This is deliberately not a generic
code-review checklist — every item below traces to a real incident, named
inline, not a best-practice assumption. If you can't find where an item came
from, it doesn't belong in this file; remove it rather than let it decay into
padding.

**Read [`VS1_MECHANISM_NOTES.md`](VS1_MECHANISM_NOTES.md) first.** It covers
the predecessor's failure modes at the mechanism-design level (what to build).
This file covers this repository's own operational and code-level failure
modes (how to check what's built). They're complementary, not duplicates.

## How to use this

Work the sections in order. Each item names what to check and how — a
command, not just "review carefully." An audit that doesn't run anything and
only reads code will miss most of what's listed here; nearly every real
finding below was caught by executing something, not by reading it.

---

## 1. Methodology — techniques that have actually caught bugs here

Before the specific checks: the techniques below, each of which caught a real,
otherwise-invisible bug in this project. Default to these over reading code.

- **Simulate the real invocation environment, not your interactive shell.**
  A script that works when you run it directly can still fail under cron,
  which gets a nearly empty environment. Test with the actual command cron
  would run, wrapped in a minimal environment:
  `env -i HOME=/home/lukes PATH=/usr/bin:/bin sh -c "cd ... && /full/path/to/python -m vs2.run_daily >> logs/run_daily.log 2>&1"`.
  This is what caught the missing `.env` file — `load_dotenv()` with no
  arguments searches upward from the working directory, found nothing (VS1's
  `.env` is a sibling directory, not an ancestor), and would have crashed the
  first real cron invocation with a bare `KeyError`. It worked in every
  interactive test all day because those explicitly loaded VS1's `.env` by
  absolute path.
- **Clone fresh and test again before trusting a push.** A local working tree
  can pass tests while the actual pushed repository is broken — most
  dangerously, `.gitignore` patterns can silently exclude a file that
  `git status` in your working copy simply never shows as missing, because it
  was never staged in the first place. This caught a bare `data/` pattern in
  `.gitignore` that matched `src/vs2/data/` at any depth, not just the
  intended root-level runtime directory, silently excluding an entire source
  module from a commit whose message claimed to add it. Verify with:
  `rm -rf /tmp/check && git clone <url> /tmp/check && cd /tmp/check && python -m venv .venv && .venv/bin/pip install -r requirements.txt -e . && .venv/bin/python -m pytest`.
- **Re-derive numeric claims from source; don't take a doc's word for it.**
  Pick several specific numbers out of `DESIGN.md` or `README.md` and
  independently recompute them. If a number can't be traced to a script or a
  specific, currently-reproducible measurement, that's a finding, not a
  detail. This caught a "fourteen months" project-age claim that had no basis
  anywhere in the actual git history (real span: 82 days), which had been
  copied across seven files and one direct answer to the user before anyone
  checked it against `git log`. It separately caught a spread-cost figure
  (8.52bp) computed against the old 503-name universe and never re-measured
  after the universe changed to the Dow 30 (real figure: 2.55bp, about a
  third the size) — carried forward silently into a design doc for a full
  session because narrowing the universe didn't visibly touch that number.
- **A tool reporting success is not the same as the outcome being correct.**
  `git status` showing nothing does not mean nothing is wrong — it can mean a
  file is being incorrectly ignored (see above). When a check matters, verify
  the *content*, not just that a command completed: read the actual file
  back, count actual rows, print the actual value.
- **Drive the real entrypoint through a scenario its tests never construct.**
  Not a new fake, not a new unit test — the actual composed `run()`, with the
  existing fakes, on an input the suite happens never to produce. This caught
  the stale-bar defect on 2026-08-08: every one of `test_run_daily.py`'s cases
  set the newest bar's date to today, so 169 passing tests said nothing about
  what happens when it is three days old. A twenty-line script that answered
  "what if the bars are stale?" showed `run(execute=True)` submitting a real
  order and stamping the execution log with the stale date, silently. The
  technique generalises: list the inputs the fixtures always hold constant, and
  vary one. A constant in every fixture is an assumption nobody has tested.
- **Check arithmetic that spans two units.** The buying-power defect found the
  same day came from sizing in *equity* and paying in *cash*: correct on day
  one, wrong as soon as holdings appreciated, and invisible to any test that
  used a flat account. Where a calculation crosses two quantities that start
  equal and drift apart, write down the case where they have drifted.
- **`cmd; echo "$?"` is not a reliable way to check an exit code in this
  environment — `cmd && echo pass || echo fail` is.** Verified directly while
  writing this file: `grep "x" file_with_no_match; echo "$?"` printed `0`
  even though the same `grep` run alone (nothing chained after it) correctly
  reported exit 1, and `grep "x" file && echo found || echo not found`
  correctly resolved to "not found" every time. The semicolon-then-`$?`
  pattern goes through an extra layer here (this Bash tool invokes `wsl.exe`,
  which invokes `bash -lc "..."`) that doesn't reliably preserve the inner
  command's real exit status across to a separately-read `$?`; `&&`/`||`
  resolve immediately within the same shell and don't have this problem.
  This explains more than one confusing "the exit code contradicts what I'm
  looking at" moment earlier in this project's history — treat any
  `; echo $?` result you find in old command output with suspicion, and use
  `&&`/`||` (or a dedicated tool call) for any check that actually matters.

## 2. Environment and assumption verification

- **Every path reference that will run under cron must be tested with the
  simulated-cron technique above**, not just run directly. This applies to
  any new script added to the crontab, and to the crontab file itself after
  any edit — confirm `crontab -l` shows what you think it shows, not what the
  template file says (they can drift; the template is not automatically
  installed).
- **`.gitignore` patterns must be anchored** (`/data/`, not `data/`) unless
  the intent is genuinely to match that name at every depth. Check any new
  top-level-named directory actually gets tracked: create a throwaway file
  inside it and confirm `git status` reports it as untracked (visible), not
  silently absent.
- **Check the real API/SDK behavior, don't assume it from memory or
  documentation prose.** Several of this project's better-founded design
  decisions came from testing an assumption and finding it wrong: the
  `adjustment` parameter for historical bars defaults to `raw`
  (unadjusted — a stock split reads as a ~90% crash) unless set explicitly;
  the `sip` data feed cannot be queried within ~15 minutes of the current
  time regardless of entitlement; the screener endpoint caps at 100 results
  and ranks by share volume, not dollar volume. None of these are visible
  from reading the code that calls the SDK — they were found by calling the
  real API and inspecting the response.

## 3. Write-path / financial-consequence code

This codebase draws a hard line: `src/vs2/data/orders.py` is the only module
that can move money, everything else is read-only by construction. Verify
that boundary still holds, and scrutinize anything inside it harder than
anywhere else in the codebase.

- **Grep for any retry decorator and confirm it is never applied to a write.**
  `grep -rn "retry_alpaca" src/` and check every result: reads (bars, account,
  positions, calendar) are safe to retry blindly. A write is not — a
  transient-looking error (429/500/502) does not mean the action was
  rejected, only that the response was lost, and retrying an action that
  actually landed duplicates it. This was a real bug, found and fixed in
  `orders.py`'s `submit()`; confirm it hasn't crept back in, including in any
  new write path added since.
- **For any "guard that prevents duplicate action," trace the exact sequence
  of what's checked, when, and what makes the check return differently next
  time.** Specifically: is there a gap between the action happening and the
  durable record of the action being written? Could two overlapping
  invocations of the same entrypoint both pass the guard before either
  writes the record that would block the second one? This class of bug has
  been found twice in this codebase already — once between deciding and
  submitting (fixed), and a documented, **still-open** narrower case between
  two processes racing the same guard, and a second narrower case between a
  successful submission and a failed log write for that submission's
  outcome. See "Known open findings" below before assuming this is settled.
- **Every write should produce a durable, per-item outcome record — success
  or failure — not just a log line.** If a batch operation can partially
  fail, confirm it isolates failures per item rather than aborting the whole
  batch on the first one (check for a bare list comprehension or similar
  around a loop that calls an API — that pattern aborts on first exception).

## 4. Test fixture realism

- **Any fixture representing external data (an API response, a scraped HTML
  page) must be extracted from real captured data, not hand-typed to look
  plausible.** Check the fixture file's own header comment for this claim,
  and spot-check by re-fetching the real source and confirming the fixture's
  structure still matches. Hand-written "realistic-looking" fixtures are
  exactly what let value-steward's predecessor ship four separate
  population/sign bugs that every one of its own tests passed — the fixtures
  modeled a population production never actually produced.
- **A fixture that is cleaner than production is not a test.** If a fixture
  has none of the messiness of the real data it's standing in for (missing
  fields, unexpected ordering, duplicate-looking rows), that's a gap, not a
  convenience.

## 5. Silent-drop / population-completeness checks

- **For every function that classifies or filters a set of items, confirm
  every input item ends up in some output category — nothing can silently
  vanish.** Look for an explicit invariant test asserting output count
  matches input count (or a stated, deliberate exception). This is the
  single most repeated defect class in this project's history: declined
  candidates, skipped symbols, and blocked trades were dropped from records
  across at least four separate modules in the predecessor before being
  caught. `build_decisions` in this codebase was written specifically to
  never do this — confirm that discipline hasn't eroded in anything added
  since.

## 6. Destructive or git operations

- **Before any `reset`, `checkout --`, or `clean` in a repository, run
  `git status` first**, even if — especially if — the identical command
  sequence just worked cleanly elsewhere in the same session. This project's
  predecessor has a live cron process that continuously mutates tracked
  files, which turned a routine `git reset --hard` (moving a commit onto a
  branch after a rejected direct push) into an incident that discarded live
  runtime state, including silently reverting an engaged kill switch, purely
  because the check was skipped after several other repos in the same
  session had clean trees. This repository does not currently have that same
  live-cron hazard on its own tracked files, but confirm that assumption is
  still true before treating a reset here as automatically safe — check
  `git status` and skim `.gitignore` for what's actually tracked before
  assuming.

## 7. Standing procedure for any audit pass

1. `git pull` on `main`, confirm the working tree is clean.
2. Run the full gate locally: `.venv/bin/python -m pytest`, `ruff check`,
   `mypy src`, `bandit -r src`. All four, not a subset — confirm pass/fail by
   reading the actual output, not by assuming a prior session's claim still
   holds.
3. Check CI status on GitHub directly (`gh pr checks <n>` or
   `gh run list --repo lukeinthecity/value-steward-mk-ii`) rather than
   trusting that local results match CI — they can diverge (different Python
   patch version, different dependency resolution).
4. Do the fresh-clone test from section 1.
5. Read "Known open findings" below and check whether each has actually been
   addressed, not just discussed.
6. If a run is in progress or just finished, run `python -m vs2.report` and
   read the caveats section first. It is designed to say why a run is not
   readable; a green-looking return above an unread warning list is exactly
   the mistake this project exists to stop making.
7. Only after 1–6: read the code changes since the last audit and apply
   sections 2–6 above to anything new.

---

## Known open findings

Don't let this section silently become "empty because no one's looked lately"
instead of "empty because nothing is open." If you find something new, add it
above this note in the same format.

### Open

1. **Alpaca's intraday daily-bar behaviour is inferred, not tested.** The
   design assumes the daily-bars endpoint serves an in-progress bar for the
   current session during market hours, and `run_daily` drops any bar dated
   after the last completed session on that basis. The guard is correct either
   way — if no partial bar is served, it simply never fires — but the
   assumption itself has not been checked against the live account, and this
   playbook's own rule is not to take SDK behaviour from documentation prose.
   **Check during the dry-run week**, before arming.
2. **What Alpaca does with a `TimeInForce.DAY` market order submitted after the
   close has never been observed here.** The cadence was changed specifically
   so orders are submitted during regular hours instead, which avoids the
   question in normal operation — but a late-running decide tick could still
   land one outside hours. Place one deliberate paper order after the close and
   record what happens, in `API_MENU.md` section 9.
3. **The report's fixtures do not yet come from a real run.** `test_report.py`
   drives the production pipeline and reports on the logs it actually wrote,
   which is far better than hand-typed rows, but those rows still come from
   fakes. Once the dry-run week has produced genuine `sessions.jsonl` and
   `fills.jsonl` rows against the live account, capture a trimmed copy as a
   fixture and assert against it — per section 4, a fixture cleaner than
   production is not a test.

### Resolved

1. ~~**Concurrent cron invocations can both pass the execution guard.**~~
   Fixed by wrapping the whole cycle in a single-instance file lock
   (`run_lock.py`, `fcntl.flock`, non-blocking — a second invocation fails
   fast rather than waiting). Verified two ways: a unit test contending two
   open file descriptions against the same lock path within one process
   (`flock` locks belong to the open file description, not the process, so
   this is a real test of the mechanism, not a simulation of one), and a
   live check forking a genuinely separate OS process to hold the lock while
   the real `run()` ran against it — confirmed `already_running=True` while
   held, and normal operation resumed correctly once released.
2. ~~**A failed execution-log write after a successful order submission loses
   the record that the order happened.**~~ Fixed: the write now retries a
   few times (safe here, unlike `orders.submit`'s retry — a local append
   either lands or raises, without the network's "maybe it landed" ambiguity)
   and, if every retry still fails, logs the raw results at `CRITICAL` before
   re-raising, so a human has something to reconcile from even without the
   structured log file.

3. ~~**A stale bar is evaluated and traded as though it were current.**~~
   Fixed 2026-08-08. The decision day came from whichever symbol sorted first
   in the signal list, not from the calendar, so with the newest bar three days
   old `run(execute=True)` submitted a real order and stamped the execution log
   with the stale date, with no warning anywhere. `MarketCalendar` gained
   `latest_completed_session()`; bars that do not reach it are refused as
   `STALE_BARS` and nothing is decided. Found by driving the real `run()`
   through a scenario no test constructed — see section 1.
4. ~~**Buys could exceed available cash and be rejected by the broker.**~~
   Fixed 2026-08-08. Equal-weight sizing divides equity while buys are paid
   from cash; once holdings appreciate the two diverge (14 holdings up 20%
   overdraws by $4,200 on a $100k account). `build_decisions` now caps buys at
   available cash and records the remainder as `BUY_DECLINED_CASH`, so a
   capacity limit stays a capacity decision instead of becoming an execution
   failure that loses the signal.

**Deferred, not forgotten:** a more robust version of resolved item #2 would have the
guard ask the broker directly ("did I place today's orders?" via a dated
order query) instead of trusting a local log file at all — the log would
still exist for humans to read, but wouldn't be the guard's source of truth.
**Half of this is now in place**: orders carry deterministic
`client_order_id`s (`vs2-{day}-{symbol}-{action}`), so the broker itself
rejects a duplicate submission of an order that landed, and `fills.py` already
queries orders back by that key. What remains is moving the *guard* itself onto
that query rather than the local log. Reconsider once this has run unattended
for real, not before.
