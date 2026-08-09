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
- **No branch protection on this repo as of 2026-08-08** — direct pushes and
  `gh pr merge` both work without VS1's `GH013` rule-violation gate. Confirm
  this is still true before assuming it (`git push origin main` failing with
  a rule-violation error means it's since been added — branch and PR instead,
  matching VS1's convention).
- **Squash-merging a PR means `git branch -d` will refuse the local branch**
  afterward even though it's genuinely merged (git's ancestry check doesn't
  recognize a squashed commit as contained). Confirm the merge via
  `gh pr view <n> --json state,mergedAt` — don't trust a bare exit code —
  then `git branch -D` is safe.
- **The crontab is environment state, not something this repo's git history
  tracks.** `crontab` at the repo root is a *template*, not what's installed.
  Run `crontab -l` to see the actual live schedule — always, before saying
  anything about what is scheduled. **Checked 2026-08-09: `crontab -l` for
  `lukes` is empty. Nothing is scheduled and nothing here has ever traded.**
  An earlier version of this file said the crontab was "installed with
  `--execute` commented out on every line", which was never true of the live
  schedule; it described the template. Don't assume the line above is still
  current either — check.
  (Cron itself is running and the machine clock is US Eastern, both confirmed
  the same day: `systemctl status cron` showed the daemon active, and its
  timestamps read EDT, which is what the template's times assume.)
- **The cron fires many times a day, and that is not the same as deciding many
  times a day.** One decision per completed close; the intraday ticks are
  *execution* attempts at it, guarded to happen once. Confusing the two is how
  VS1's 214 scorecard rows turned out to be 104 real decisions, so keep the
  distinction sharp in any change to `run_daily.py`.
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
