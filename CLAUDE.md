# Value Steward mk II — Claude Code project guide

Value Steward mk II is a paper-trading agent built around a single published
rule — a 50-day moving-average crossover — and nothing else. It is the
successor to Value Steward 1 (`/home/lukes/value-steward`), retired from
trading on 2026-08-07 after three runs that never produced a readable answer
about whether its strategy worked. VS2 exists to answer that question
cleanly, and inherits VS1's Alpaca paper account.

**The local directory is `value-steward-2`; the GitHub repository is
`value-steward-mk-ii`.** These names don't match — the directory predates the
final repo name — and that mismatch has caused real confusion mid-session
before. `git remote -v` is the source of truth if anything seems off.

**Read in this order:** `README.md` for the overview, `DESIGN.md` for the
rule and every measured decision behind it (universe, order type, stop-loss —
each backed by a number, not an assertion), `docs/API_MENU.md` for what the
broker actually offers versus what's used, `docs/VS1_MECHANISM_NOTES.md` for
the predecessor's catalogued failure modes and the checklist to run before
adding any new mechanism here.

**Before reviewing or auditing anything, read
[`docs/CODE_CHECK_PLAYBOOK.md`](docs/CODE_CHECK_PLAYBOOK.md).** It is not a
generic checklist — every item traces to a real bug found in this codebase or
its predecessor, with the specific technique that caught it. It also lists
currently **known open findings** that have not yet been fixed; check that
list before assuming the codebase is clean.

**Prose must be factual and objective.** No self-praise, superlatives, or
performance claims anywhere in docs, comments, or commit messages —
"institutional-grade", "high-precision", "professional", "sophisticated", and
the like are banned vocabulary. Describe what the code does; let the reader
judge quality. This has been the voice of the whole project (VS1 and VS2)
since the beginning — keep it consistent.

---

## Environment & workflow

- **The repo lives in WSL** at `/home/lukes/value-steward-2` (Windows sees it
  as `\\wsl.localhost\ubuntu\home\lukes\value-steward-2`). Run git, tests, and
  everything else from WSL, not Windows Git Bash — mixing the two on the same
  `.git` causes object-ownership/permission errors, and this has bitten VS1
  before.
- **Python only, no Node dependency.** The `.venv` at the repo root has
  everything: `pytest`, `ruff`, `mypy`, `bandit`. Full gate:
  ```
  .venv/bin/python -m pytest && .venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/bandit -r src
  ```
- **`.env` is local and gitignored, not committed.** It holds the same Alpaca
  paper credentials VS1 uses (same account — see "Exactly one system may
  trade this account" in `DESIGN.md`), and the same ntfy push settings
  (`VS_NTFY_TOPIC`, `VS_NTFY_SERVER`, `VS_NTFY_TOKEN`, `VS_PUSH_ENABLED`) —
  the variable names are deliberately identical to VS1's so one copied `.env`
  configures both. **The ntfy topic is a secret**: anyone holding it can read
  and publish to it. Never log it, never put it in a commit, an error message
  or a doc, and never hardcode a server other than the public
  `https://ntfy.sh` default. If it's ever missing, `main()` in
  `run_daily.py` calls bare `load_dotenv()`, which searches upward from the
  working directory and will **not** find VS1's `.env` (a sibling directory,
  not an ancestor) — it will crash on `os.environ["ALPACA_API_KEY_ID"]`
  instead. Copy it at the OS level
  (`cp /home/lukes/value-steward/.env /home/lukes/value-steward-2/.env`) —
  never read or display the actual key values.
- **`gh` is authed in WSL only**, same as VS1 (shared machine, shared auth).
  Push and open PRs from WSL.
- **Branch protection IS on, as of 2026-08-09.** A direct `git push origin
  main` is rejected with `GH013: Repository rule violations found` — changes
  must go through a pull request, and 3 of 3 required status checks must pass.
  Branch and PR, matching VS1's convention. (This file said the opposite until
  2026-08-09; it was true on 2026-08-08 and stopped being true the next day,
  which is why it told you to confirm rather than assume.)
- **When a direct push to `main` is rejected, do NOT `git reset --hard` to move
  the commit onto a branch.** That exact sequence — rejected push, then
  `git branch x && git reset --hard HEAD~1` — is the worst incident in
  `agent-playbooks/INCIDENT-LOG.md`: it discarded live uncommitted runtime
  state, including a kill switch that had been engaged minutes earlier, and was
  found by accident days later. Use a sequence that cannot touch the working
  tree at all:
  ```
  git status                                   # look first, always
  git checkout -b <branch>                     # the commit comes with you
  git push -u origin <branch>
  git branch -f main origin/main               # main isn't checked out; only a pointer moves
  ```
  `git branch -f` on a branch you are not standing on moves a ref and nothing
  else. Prefer it to any reset that *would* have been fine.
- **Squash-merging a PR means `git branch -d` will refuse the local branch**
  afterward even though it's genuinely merged (git's ancestry check doesn't
  recognize a squashed commit as contained). Confirm the merge via
  `gh pr view <n> --json state,mergedAt` — don't trust a bare exit code —
  then `git branch -D` is safe.
- **The crontab is environment state, not something this repo's git history
  tracks.** `crontab` at the repo root is a *template*, not what's installed.
  Run `crontab -l` to see the actual live schedule — always, before saying
  anything about what is scheduled. **Checked 2026-08-14: the dry-run lines
  are installed (no `--execute` on any of them), and nothing here has ever
  traded.** They went in on 2026-08-09, when `crontab -l` was empty; the first
  week ran 2026-08-10..08-14. An earlier version of this file said the crontab
  was "installed with `--execute` commented out on every line", which was never
  true of the live schedule; it described the template. Don't assume the line
  above is still current either — check.
  (Cron itself is running and the machine clock is US Eastern, both confirmed
  the same day: `systemctl status cron` showed the daemon active, and its
  timestamps read EDT, which is what the template's times assume.)
- **WSL must stay resident, or cron does not run — installing the crontab is
  not enough.** WSL2 shuts the VM down once the last session closes, taking
  `cron` with it; an installed schedule and an active `cron.service` both stop
  meaning anything at that moment. Keep a WSL window open (VS1's arrangement on
  this always-on machine), or configure `vmIdleTimeout` / a Task Scheduler
  keepalive. **Measured 2026-08-14**: the window was closed after Thursday's
  session and Friday's decide window passed with the VM down — one session lost
  out of six in the first dry-run week. The loss is detected rather than
  silent: the next run reports `missed_sessions` and `main()` exits non-zero so
  cron's MAILTO fires, verified against a simulated four-day outage. But
  detection is not prevention, and each missed session is a hole in the
  measurement.
- **The cron fires many times a day, and that is not the same as deciding many
  times a day.** One decision per completed close; the intraday ticks are
  *execution* attempts at it, guarded to happen once. Confusing the two is how
  VS1's 214 scorecard rows turned out to be 104 real decisions, so keep the
  distinction sharp in any change to `run_daily.py`.
- **The world layer is not the trading layer.** `src/vs2/world/` collects
  world-state data for a mechanism DESIGN.md still defers. Nothing in it may
  reach a decision: separate entrypoint, separate cron, and an import-boundary
  test (`tests/test_world_isolation.py`) that fails if either side imports the
  other. Collecting is not gating; don't let a future change blur that.
- **World history lives in two places on purpose.** `world_history/*.jsonl.gz`
  is tracked and immutable; `data/world_context.jsonl` is gitignored and live.
  VS1 kept only the live kind, which is how its dataset ended up five months of
  uncommitted changes to a tracked file. The pre-gap block
  (2026-01-23..2026-03-20) is archived separately and **must not be merged**
  into the working series — a permanent six-week hole separates them.
- **Four append-only logs under `data/` are the run's only record**:
  `decisions.jsonl`, `executions.jsonl`, `fills.jsonl`, `sessions.jsonl`.
  Nothing recomputes them and nothing rewrites a row. If a change would make
  any of them unwritten for a session, that session becomes unmeasurable —
  `python -m vs2.report` will say so, and should be run after touching
  anything in the daily cycle.
- **Runnable scripts must guard `main()`** behind an
  `if __name__ == "__main__":` check (see `index_membership.py` or
  `run_daily.py`) so importing them for tests never executes real work
  against the live account.

## Agent discipline

1. **Surgical scope.** Only touch what the task requires.
2. **No speculative refactors or reformatting** outside scope.
3. **No regressions.** Existing tests must pass. Changing a shared interface
   means updating every call site in the same change.
4. **Match existing conventions** — the pure-computation/I-O split
   (`vs2.core` never touches a network or a file; `vs2.data` does, and
   `orders.py` is the only file in it that can move money), dependency
   injection for every Alpaca-facing client so tests never need a real one,
   explicit `reason_code`s on every decision row.
5. **Declare your footprint** — end with what changed and why.

## Definition of done

Requirement satisfied · full gate green (pytest, ruff, mypy, bandit) · CI
green on GitHub, not just locally · nothing outside scope changed · footprint
declared.
