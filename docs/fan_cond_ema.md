# Conditional EMA Direction (`fan_cond_ema_v1`)

Asked 2026-10-07, after `fan_direction_v1` ([docs/fan_direction.md](fan_direction.md)) found
no direction skill in a fixed EMA projection or an additive ridge. That result stands
unchanged. This experiment asks a narrower question: **do EMA slopes, alignment and price
extension, read jointly with timeframe, volatility, volume and session phase, add directional
information beyond recent returns and VWAP position?** The answer may be continuation,
reversal or nothing; every arm may end at zero shift. The production fan is untouched.

[forecaster/fan_cond_ema.py](../forecaster/fan_cond_ema.py), `scripts/fan.py cond-ema`; tests
[tests/test_fan_cond_ema.py](../tests/test_fan_cond_ema.py).

## The definition (fixed before the run)

Hash **`3ec8838a731fb229`**, written to [fan_cond_ema_v1_definition.json](fan_cond_ema_v1_definition.json)
before any real-data result; `cond-ema` refuses to run if the code's definition differs.

**The fan.** One shared width and shape per block: `lin_pois_ivx`'s multiplier, refitted on the
sessions before the block (for validation, on the sessions before the validation sessions), times
`fan_rw_v2`'s walk-forward sigma, with v2's shape. The frozen width model is not used: it was
trained on sessions inside these blocks. Every arm's fan is

```
P_t exp(mu_h(x_t) + s_h(x_t) z_q),   mu_h = lambda x clip(model(x), +-1) x s_h(x_t)
```

**Arms.**

| Arm | Shift |
|---|---|
| A `zero` | none: the current centred fan |
| B `context` | shallow gradient boosting on the context inputs |
| C `ema` | the same family and tuning budget on the context inputs plus the EMA set |

The essential comparison is C against B.

**Inputs** (NQ's own, point in time; sessions laid end to end on the 18:00 ET minute grid):

- *EMA:* Pine's `ta.ema` (alpha = 2 / (period + 1), seeded with its first value) of log price,
  periods 14 and 100, on two timeframes. **1-minute:** updated only at minutes where a 1-minute
  bar closed, carried between. **5-minute:** bars aligned to 18:00 ET; a bar completes at the end
  of its last minute and exists when a 1-minute bar closed inside it; its close is the last close
  known then; the EMA updates at completion and is carried until the next completed bar, never
  from a bar still forming. **Warm-up:** missing until the EMA has seen 3 x its period of its own
  bars (42 / 300 bars).
- *Trailing volatility* sd60: the RMS of the last 60 one-minute log returns.
- *EMA set (11):* per timeframe each EMA's slope (its change over the last 15 elapsed minutes,
  over sd60 x sqrt(15)), the price's distance from each EMA and EMA 14 - EMA 100 (over
  sd60 x sqrt(15)); `tf_agree` = the sign of the 1-minute gap where it equals the 5-minute gap's,
  else 0. Clipped to +-10.
- *Context (B and C):* the 1-, 5- and 15-minute returns (over sd60 x sqrt(w)), the distance from
  the session VWAP (closes x volume from 18:00 ET, over sd60 x sqrt(15)), `rv15` and `rv60`
  (realised variance over usual for the time of day), `vol60` (relative volume), and the
  session phase as a categorical input.

**Model and tuning budget**, per horizon (5 and 15 minutes) and arm: scikit-learn's
`HistGradientBoostingRegressor`, squared error on the forward log return in the shared fan's
sigmas, winsorised at +-4; learning rate 0.05, at least 2,000 rows a leaf, L2 1.0. Grid: depth
{2, 3} x {50, 150} trees; shrinkage lambda {0.25, 0.5, 1.0}. That makes 12 options, plus exactly zero
shift. Training origins every 5 minutes.

**Selection and activation.** The last 20 % of a block's training sessions validate; models and
width are fitted on the sessions before them. Every option and zero are scored there by the
shifted fan's CRPS (the mean over sessions of each session's mean). The best is activated only
if its session-level paired interval against zero lies wholly below zero, at **99.58 %** (95 %
Bonferroni-corrected over the 12 options searched). Otherwise the arm is zero. An activated
option is refitted on every training session. No minimum shift.

Why 99.58 % and not 95 %: the no-signal control (below) showed that taking the best of 12
options and then testing it on the same validation sessions is optimistic. On synthetic
sessions with no signal and 30 validation sessions, 95 % activated in 2 of 160 fits and 99.58 %
in none. With planted signals strong enough to see, both activate. This was decided on
synthetic data only, before any real-data run.

**Labels.** Every outcome ends inside its own session (t + h before the day's end) and sessions
are split whole, so no training label reaches into validation or evaluation.

**Evaluation.** Rolling origin over the intermarket experiment's three checks and its spent
holdout in two blocks of 30 (2026-03-03 to 2026-10-05, 150 sessions). Every origin is scored, and
every session is development: all were examined before. Primary: CRPS at 15 minutes; secondary: 5
minutes. Reported: paired CRPS differences C - A, C - B and B - A (session level, 5-session moving
blocks); P(up) Brier score and reliability; 50 % and 90 % coverage; per block; the share and size
of nonzero shifts.

**Decision.**

- **Pass** at a horizon: C - A and C - B both have their 97.5 % interval wholly below zero
  (Bonferroni over the two horizons, either of which could qualify). The experiment passes if
  either horizon does.
- **Fail:** at both horizons C's mean CRPS is not below both A's and B's: no incremental EMA
  benefit.
- **Inconclusive:** otherwise.
- After a pass: freeze the full specification and evaluate it on fresh, timely forward data
  before any directional overlay.

**Diagnostics** (explanation only, never a deployment rule): by session phase, by `tf_agree`
(-1 / 0 / +1 / missing), and by price extension, in terciles of |distance from the 1-minute
EMA 100| with boundaries from each block's training rows.

**Verification** (tests): no feature at a minute changes when later bars change, including
inside a forming 5-minute bar; the 5-minute EMA moves only when a bar completes; warm-up is
exact; a fitted arm's forecast at an origin ignores everything after it; a planted conditional
relationship (reversal overnight, continuation midday and afternoon, through a persistent input
stored as the 5-minute gap) is recovered, activates C, and passes; two no-signal controls
activate nothing. Synthetic success verifies the implementation, not market predictability.

## Result (run once, 2026-10-07): **FAIL** - no incremental EMA benefit

[Report](reports/fan_cond_ema_v1_fan_intermarket_v2_NQ.md). **No arm activated in any block,
at either horizon.** On every one of the 150 evaluation sessions all three arms issued the same
centred fan: 0 % of origins shifted, every paired difference exactly zero. By the declared rule C
is not below A and B at either horizon: no incremental EMA benefit, and no directional overlay.

What the validation sessions showed (each block's last 20 % of training, 31-55 sessions), on the
best of 12 options per arm and horizon against zero shift:

| | 5 min, context (B) | 5 min, EMA (C) | 15 min, context (B) | 15 min, EMA (C) |
|---|---|---|---|---|
| check 1 | -0.070 % | -0.039 % | zero best | -0.004 % |
| check 2 | -0.042 % | -0.035 % | -0.005 % | -0.027 % |
| check 3 | -0.002 % | -0.005 % | zero best | -0.001 % |
| holdout 1 | -0.004 % | -0.011 % | -0.010 % | -0.039 % |
| holdout 2 | -0.024 % | -0.019 % | -0.004 % | -0.020 % |

- Every best option improved on zero by at most 0.07 % of CRPS, and none had its corrected
  interval below zero.
- **The correction did not decide it.** At an uncorrected 95 %, one of the 20 fits would have
  activated: the *context* arm at 5 minutes in check 2. The EMA arm would never have activated.
- **At 5 minutes the EMA inputs add nothing:** C's best is no better than B's in three of five
  blocks.
- **At 15 minutes C's best beat B's in all five blocks, by 0.004-0.03 % of CRPS.** This is not
  evidence: each is the best of 12 options, the gains are a fraction of their own uncertainty, and
  the validation windows overlap (each block's validation reaches into the next block's). It is
  recorded as the only pattern seen, and does not open a new search.

## What follows

- `fan_direction_v1` and this experiment both find no directional skill at 5 and 15 minutes. The
  production fan stays centred; nothing is frozen, and no forward evaluation follows.
- The search is not expanded. Any further direction work would be a new version, reported as
  having seen both results. It would need information these inputs do not hold, not another
  arrangement of the same prices.

```bash
python scripts/fan.py cond-ema --definition   # write the fixed definition (docs/fan_cond_ema_v1_definition.json)
python scripts/fan.py cond-ema                # the run: refuses if the code's definition differs from it
```

## Power of the activation rule (measured 2026-10-07, after both runs)

Asked by the outside review: recovering a strong planted signal proves the pipeline works, but
can 31-55 validation sessions detect a *weak* useful one? [forecaster/fan_power.py](../forecaster/fan_power.py)
plants a conditional signal of known size (reversal overnight, continuation midday and
afternoon, through one persistent input) in synthetic sessions and runs the defined selection
and activation, 20 independent histories per cell. The effect size is the true conditional
mean's correlation with the move in sigmas, and its CRPS gain over zero shift. That gain is the
most any model could reach; a fitted model reaches less. Result in
`data/fan_cache/direction/activation_power.json`.

| Planted kappa | Oracle correlation 5 / 15 min | Oracle CRPS gain 5 / 15 min | Activated, 31 validation sessions (5 / 15 min) | Activated, 55 validation sessions (5 / 15 min) |
|---|---|---|---|---|
| 0 (no signal) | 0 / 0 | 0 / 0 | 0 % / 0 % | 0 % / 0 % |
| 0.005 | 0.01 / 0.02 | 0.01 / 0.02 % | 0 % / 0 % | 0 % / 0 % |
| 0.01 | 0.02 / 0.03 | 0.02 / 0.06 % | 0 % / 0 % | 0 % / 0 % |
| 0.015 | 0.03 / 0.05 | 0.05 / 0.12 % | 0 % / 0 % | 0 % / 20 % |
| 0.02 | 0.04 / 0.06 | 0.09 / 0.21 % | 10 % / 5 % | 20 % / 35 % |
| 0.03 | 0.06 / 0.09 | 0.19 / 0.46 % | 40 % / 50 % | 80 % / 85 % |
| 0.05 | 0.10 / 0.15 | 0.52 / 1.23 % | 100 % / 100 % | 100 % / 100 % |

The arm without the planted input activated in none of its 560 fits, and the no-signal histories
in none of 80.

**What the rule can detect.** Roughly half the time, a signal worth about 0.2 % of CRPS at 5
minutes or 0.45 % at 15 (correlation 0.06 / 0.09) on the smallest validation set. Most of the
time, the same on the largest. It is blind below about 0.05 % / 0.1 % (correlation 0.03 / 0.05).
For scale:

- `fan_direction_v1`'s ridge reached out-of-sample correlations of about 0.01-0.02;
- the best validation gains in `fan_cond_ema_v1` were at most 0.07 %;
- the one intermarket activation gained 0.057 % at 5 minutes.

All of these lie in the range where this rule rarely activated in the simulations.

**What this does and does not show** (corrected 2026-10-07 after an outside review). No
sufficiently reliable directional improvement was established; the simulations suggest limited
sensitivity to small effects. The failures do *not* rule out effects of 0.2 % CRPS or more:

- at 50 % power a signal of that size is missed half the time, and even 80 % power misses it one
  time in five;
- power depends on the particular signal simulated (here one persistent input with a phase-dependent
  sign) and on the rest of the setup;
- 20 histories per cell give rough estimates (a rate of 50 % has a standard error of about 11
  points);
- the synthetic sessions are Gaussian with constant volatility, and NQ's fat tails and volatility
  clustering widen real intervals, so real power is probably lower than this table.

Likewise, "no false activations were observed in these simulations" (0 of 640 fits) bounds the
false-activation rate only roughly: under 0.5 % at 95 % confidence, for this simulated setup.
