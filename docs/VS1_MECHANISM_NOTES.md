# What VS1 taught about building mechanisms

Value Steward 1 ran for fourteen months and three runs. It never produced a
readable answer about whether its strategy worked, but it produced a detailed
record of how each of its mechanisms was structured and how each one failed.
That record is the reason to keep the repository.

This file exists to be read **before adding any mechanism to VS2**, not as
history. Each entry states what the mechanism was for, what actually happened,
and the structural rule that follows.

VS2 starts with one mechanism on purpose. Every entry below describes something
that was added to VS1 before the thing beneath it had been measured.

---

## The mechanisms

| Mechanism | Outcome |
|---|---|
| Gate calibration | Worked. The only mechanism that produced usable evidence. |
| 7-gate stack | Untested except for one gate. |
| Signal weight trainer | Never had the sample size to be valid. |
| Champion-challenger | Mutated live policy using a broken metric. |
| Score-gate posteriors, Thompson sampling | Inherited a 4× data defect silently. |
| Three score nudges | Made attribution impossible. |
| Correlation matrix | A performance limit became a strategy limit. |
| Four intraday execution slots | Contaminated the measurement layer. |
| Circuit breaker and window guard over sells | Capital protection that inverts. Confirmed, and reachable at VS2's sizing. |
| Volatility stop | Fails silently in the case it exists for. |
| Market-open notice | Reported state it never read. |

---

## 1. Gate calibration — the one that worked

**For:** measuring whether each individual gate was earning its place.

**What happened:** it produced a real result. `rel_strength_60d` measured
justified at n=37, mean −0.82%, t = −2.65. That is the only number VS1 produced
that survived scrutiny, and it survived because the gate was tested *in
isolation*, against its own recorded decisions, with a stated sample size.

**Rule for VS2:** every gate or filter ships with the test that measures it
alone. A mechanism that cannot be measured in isolation should not be added.

## 2. The 7-gate stack

**For:** filtering candidates before execution.

**What happened:** seven gates were added over time. Exactly one was ever
calibrated. The other six ran for fourteen months with no evidence that any of
them improved anything, and collectively they starved the funnel to roughly 0.5
candidates per day — which is itself why no mechanism downstream had sample size.

**Rule for VS2:** count what a filter removes before deciding whether to keep it.
Filters compound multiplicatively, so each additional one looks cheap and the
stack becomes the binding constraint without anyone choosing it.

## 3. Signal weight trainer

**For:** learning the relative weight of momentum, volatility, and drawdown rank.

**What happened:** it was fitting three coefficients against roughly 130 unique
decisions accumulated over fourteen months, and it fired on 2 of 20 cycles
because it kept failing its own minimum-sample check. The machinery was
correct — the sample size for it never arrived, and never would have at that
decision rate.

**Rule for VS2:** before building a learner, state the sample size at which it
becomes valid and how long that will take to accumulate at the actual decision
rate. If that answer is "years," build the fixed version and revisit.

## 4. Champion-challenger

**For:** automatically promoting improved weights and rolling back regressions.

**What happened:** it consumed the out-of-sample metric to decide promotions.
That metric was scoring most of its population backwards. So the mechanism was
faithfully promoting and reverting live policy based on an inverted reading, for
an entire run. This is what ended run 3 — not a bad result, but the discovery
that the live policy had been mutating on a broken number.

**Rule for VS2:** a metric that drives automatic action needs a far higher bar
than a metric that is merely reported. Nothing mutates policy automatically
until the metric driving it has been validated independently of the code that
produces it.

## 5. Score-gate posteriors and Thompson sampling

**For:** learning which score ranges converted, and sampling accordingly.

**What happened:** the posteriors were inflated roughly 4× because the rows they
counted were duplicated upstream (see 7). The statistics were computed
correctly. They were computed over a population that did not exist.

**Rule for VS2:** statistical machinery inherits every upstream data defect
silently and confidently. The defect surfaces as a plausible number, never as an
error. Validate the population before trusting any statistic over it.

## 6. The three score nudges

**For:** adjusting the ranking score by execution quality, realized alpha, and
intraday persistence.

**What happened:** each was a weighted blend into a single score. Once blended,
no decision could be attributed to any component — there was no way to ask
whether execution quality had helped, because its contribution was summed away
before anything was recorded. One of the three was hardcoded with no
configuration hook at all.

**Rule for VS2:** mechanisms vote, they do not blend. A yes/no vote that is
recorded separately can be measured and removed. A weight added into a scalar
cannot be recovered afterwards. This is why world-state gating enters VS2 as a
veto rather than as a weight.

## 7. Four intraday execution slots

**For:** retrying execution at 30, 20, 10, and 5 minutes before the close, to
improve fill rates on thin volume.

**What happened:** the execution layer wrote one decision row per attempt, so a
single decision produced four near-identical rows. Every downstream statistic
was computed over replicated values: minimum-sample floors were effectively
quartered, and standard deviations were computed across duplicates, which
inflated Sharpe ratios. The execution behavior was reasonable. The logging
convention silently corrupted every statistic in the system.

**Rule for VS2:** the decision log is keyed on the decision, not the attempt.
Execution attempts are logged separately. Retrying is an execution concern and
must never change the shape of the measurement data.

## 8. Correlation matrix

**For:** avoiding concentrated positions in correlated names.

**What happened:** it was O(n²) in universe size — 6.6s at 200 symbols, 148.7s
at 1200, and around 70 minutes at the full tradable universe. To keep tick
runtime under the cadence, the universe was capped. That cap then became the
reason the candidate funnel was starved, which is the reason no mechanism
downstream had sample size. Nobody chose to trade a 200-symbol universe; it fell
out of an implementation cost.

**Rule for VS2:** know which limits were chosen and which were inherited from
implementation cost. Write down the reason next to the number. VS2's crossover
rule needs no correlation matrix, which is why its universe can be sized by
judgment instead.

## 9. Circuit breaker and window guard over sells — confirmed

**For:** halting trading after a daily loss threshold, and confining execution to
a window before the close.

**What happened:** both gates block the volatility stop. Traced end to end on
2026-08-07:

1. The vol-stop builds `IntentRecord(action_type="SELL",
   reason_code="VOL_STOP")` — `decision_engine.py:683-699`.
2. That intent reaches `execute_intent` through the single tick path —
   `cli.py:516`.
3. `execution_engine.py:242` applies the window guard to
   `action_type in {"BUY", "SELL"}`. The window is 30 to 5 minutes before the
   close, so a position cratering at 10:00 cannot be exited until 15:30.
4. `execution_engine.py:258` applies the daily-loss circuit breaker with no
   order-type distinction at all. Default threshold is 3%.

The second one is the inversion. The circuit breaker fires when the account is
down on the day, and the vol-stop fires when a position is down hard — **these
are the same event.** The protection is most likely to be disabled exactly when
it is most needed.

**The code already knew the principle.** `execution_engine.py:365-375` exempts
sells from the per-trade notional cap, with a comment reading: *"SELLs are
risk-reducing (VOL\_STOP panic exits, CAP\_BREACH\_SELL, rebalance trims). They
must NOT be throttled..."* The reasoning was correct and was applied to exactly
one of the three gates.

**Why it never bit VS1, and why it would bite VS2.** VS1 deployed at most $2,000
against ~$100,000 of equity, so no single position could move the account 3% and
the circuit-breaker half was unreachable. The window-guard half applied every
day and simply went unnoticed, because the vol-stop rarely fired.

VS2 plans 20 equal slots against the full account — roughly 5% each. One
position halving is a 2.5% account move. **The defect becomes reachable at
precisely the sizing VS2 has chosen.** Whatever exit mechanism VS2 eventually
adds has to be built with this already in mind.

**Rule for VS2:** risk-reducing actions are exempt from risk gates. A guard that
can block an exit is not a guard. And note that position sizing changes which
latent defects are reachable — a bug that is unreachable at 2% deployment is not
fixed, only hidden.

## 10. Volatility stop

**For:** exiting a position after an outsized adverse move.

**What happened:** the loop looked up each held symbol in the current signal
results, and did nothing when the symbol was absent. A symbol goes absent when
its bars are stale or missing — a halted stock, for instance. The protection
silently did not run in precisely the situation that most warranted it, and
logged nothing.

**Rule for VS2:** protective mechanisms fail loud. If a guard cannot evaluate,
that is an event worth recording and alerting on, never a silent skip.

## 11. Market-open notice

**For:** a daily push notification confirming the system was live.

**What happened:** it announced the system as active on a morning when trading
had been disabled for a day, because it never read the control state it was
reporting on. No trade occurred — the kill switch worked. The reporting layer
simply had no connection to it.

**Rule for VS2:** status reporting reads the same state the actor reads. A
monitoring surface that derives its answer independently will eventually
disagree with reality, and it will be believed.

---

## The pattern underneath

Four separate modules were found to have the same class of fault — wrong
population or wrong sign — in a single audit. They were written at different
times for different purposes. The common cause was upstream of all of them:
**the test fixtures modeled a population that production never produced.**
Hand-written ideal rows, each with the fields the test needed, none resembling
the messy replicated rows the live system actually wrote. Every module was
correct against its fixtures and wrong against reality.

**Rule for VS2:** fixtures are built from real recorded rows, or from a generator
that reproduces their defects. A fixture that is cleaner than production is not
a test.

The second pattern is that measurement was written alongside the thing it
measured, by the same author, in the same commits. It was never independently
checked, and it was wrong twice in a way that ended two runs. VS2 specifies
measurement before the strategy for this reason.

---

## Checklist before adding any mechanism to VS2

1. What does this mechanism decide, and can that decision be recorded on its own?
2. Can it be measured in isolation, and what is the test?
3. What sample size does it need to be valid, and how long is that at the current
   decision rate?
4. Does it vote, or does it blend? Blending needs a specific justification.
5. If it consumes a metric, has that metric been validated independently?
6. If it can block an action, can it block a risk-reducing action? Fix that
   first — and check *every* gate, not the one that prompted the question.
   VS1 exempted sells from one of three.
7. If it can fail to evaluate, does it say so loudly?
8. Are its fixtures built from real recorded rows?
9. At the position sizing actually in use, is this mechanism reachable? A defect
   that cannot trigger at 2% deployment is hidden, not fixed, and VS2 deploys at
   100%.
10. Is the mechanism beneath it already measured?

Question 10 is the one VS1 never asked.
