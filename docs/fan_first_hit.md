# First-hit probabilities (`fan_first_hit_v1`)

Asked 2026-10-07, after the centre-shift experiments were archived (`fan_direction_v1`,
`fan_cond_ema_v1`, `fan_im_dir_v1`: no reliable directional improvement of the fan's endpoint).
This is a different question, not a relabelling of those:

> Within the next 15 minutes, will NQ reach an upper barrier first, an equally distant lower
> barrier first, or neither?

A market can finish near its start after a useful excursion; endpoint forecasting does not
measure that. A result here would support up-first / down-first / neither probabilities
**beside** the chart. It would not justify shifting the fan, which stays centred and untouched.
That first hits are easier to predict is untested, not assumed.

[forecaster/fan_first_hit.py](../forecaster/fan_first_hit.py), `scripts/fan.py first-hit`; tests
[tests/test_fan_first_hit.py](../tests/test_fan_first_hit.py).

## The definition (fixed before the run)

Hash **`9d36df152bdb8ac2`**, written to [fan_first_hit_v1_definition.json](fan_first_hit_v1_definition.json)
before any outcome was computed on real data. `first-hit` refuses to run if the code differs.

**Barrier (one rule, chosen before any outcome).** +-1 x `fan_rw_v2`'s walk-forward sigma of the
log price to 15 minutes, around the log of the origin's last close. That sigma is known at issuance
and is the same in every block. Under a random walk about a third of origins would fall in each
class.

**Path and outcome.** The active contract's 1-minute trade bars starting in the 15 minutes after
the origin (each bar's high and low). The first minute whose high reaches the upper barrier, or
whose low reaches the lower, decides. If both are reached in that same minute the case is
**ambiguous**: minute bars cannot order them, and no finer data is stored. If neither is reached
by the 15th minute the outcome is neither. Origins are the fan's: a price at both ends inside the
trading day. The loader checks that every bar's close equals the frame's price at that minute, and
refuses to run otherwise.

**Predictors.** Per block, each is fitted on the sessions before it.

| | Predictor |
|---|---|
| A `freq` | the training rows' class frequencies |
| B `own` | a 3-class `HistGradientBoostingClassifier` on NQ's own context: 1-, 5-, 15-minute returns, VWAP distance, `rv15`, `rv60`, `vol60`, phase |
| C `intermarket` | the same with `fan_im_dir_v1`'s 14 intermarket inputs added |

The model budget is the earlier experiments': depth {2, 3} x {50, 150} trees; learning rate 0.05,
at least 2,000 rows a leaf, L2 1.0. Each option is blended with A, p = lambda x model +
(1 - lambda) x freq, lambda in {0.25, 0.5, 1}, and A itself is an option. Selection is by the lowest
mean session multiclass Brier score on the validation sessions (the last 20 % of training),
followed by a refit on every training session. There is no activation interval this time: A is
itself a fitted forecast, and the out-of-sample blocks decide.

**Ambiguous origins** are left out of training and selection. They are scored under **both**
assignments (read as up first, read as down first), and their share is reported.

**Evaluation.** The same five chronological blocks (150 sessions, all development), every origin.
Score: multiclass Brier, the sum over the three classes of (p - outcome)^2, as session means. Paired
session-level differences use 5-session moving blocks. Reliability is reported per class.

**Hypotheses and decision** (Bonferroni over the two: 97.5 %):

- **H1:** B beats A, i.e. NQ's own context predicts first hits.
- **H2:** C beats B *and* A, i.e. the intermarket inputs add. Beating B alone could just reflect B
  overfitting; the synthetic no-signal control showed exactly that, so the rule was tightened
  before the real run.
- **Pass:** every difference's 97.5 % interval lies wholly below zero, under both ambiguity
  assignments. **Fail:** a mean difference is not below zero under either assignment.
  **Inconclusive:** otherwise.
- **Directional or size?** A barrier reached at all is partly a matter of size, which is known to
  be predictable. A passing model counts as *directional* only if it also beats its symmetric
  version (P(up) and P(down) averaged), with the 95 % interval wholly below zero under both
  assignments.
- After a pass: freeze, then fresh forward data before any probabilities are shown.

**Verification** (tests):

- a barrier is decided by the first minute that reaches it; a same-minute touch of both is
  ambiguous; neither the origin's own bar nor anything past minute 15 is read;
- missing bars count as no touch, and both assignments are scored;
- a planted directional relationship carried by ES's 5-minute return is recovered by C only and
  passes H2 as directional;
- three no-signal controls fail both hypotheses.

## Result (run once, 2026-10-07): **H1 pass, not directional; H2 fail**

[Report](reports/fan_first_hit_v1_fan_intermarket_v2_NQ.md). 204,749 origins in 150 sessions. The
high/low bars matched the frames' prices exactly (418,138 bars, none differing).

**Outcomes.** Down first 27.3 %, neither 46.6 %, up first 26.0 %. Ambiguous: 58 origins (0.03 %),
so the two assignments give the same results to the fourth decimal.

| Multiclass Brier (ambiguous read as up first) | freq | own | own, symmetric | intermarket | intermarket, symmetric |
|---|---|---|---|---|---|
| pooled | 0.64068 | 0.62866 | **0.62824** | 0.62876 | 0.62828 |

| Comparison | Difference | 95 % interval | Reading |
|---|---|---|---|
| own - freq (H1) | -1.88 % | [-0.0154, -0.0080] (97.5 % below zero, both assignments) | **pass** |
| intermarket - own (H2) | +0.015 % | [-0.0007, +0.0011] | **fail** |
| intermarket - freq | -1.86 % | below zero | (H2 needs both) |
| own - own symmetric | **+0.067 %** | [+0.0001, +0.0008], above zero | the up/down split costs |
| intermarket - intermarket symmetric | +0.077 % | above zero | likewise |

- **First hits are predictable, but only in their size.** NQ's own context beats training
  frequencies in every block (-0.8 % to -3.4 %) and every phase, and is well calibrated. Averaging
  its P(up) and P(down) makes it *better*, significantly so pooled and in four of five blocks.
  Everything it knows is whether a barrier is reached at all, i.e. how far NQ moves. That is the
  predictability the learned fan's width already uses. Which side is reached first is not
  predicted: by the declared rule the pass is **not directional**.
- **The intermarket inputs add nothing** to first hits either (H2 fail; +0.26 % to -0.28 % by
  block).
- No sufficiently reliable directional component was established. As with the centre shift, the
  simulations suggest limited sensitivity to small effects, so this is not proof that none exists.

## What follows

By the declared rule a pass is frozen and tested on fresh data before any probabilities are shown.
This pass does not answer the directional question the experiment was built for. Its useful part,
P(neither), restates how far NQ moves, which the fan already shows. So nothing is frozen or
displayed without the user's decision, and the search is not expanded. If first-hit probabilities
were wanted beside the chart, the honest form is the symmetric one: P(barrier reached) and an
even up/down split.
