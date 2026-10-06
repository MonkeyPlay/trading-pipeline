# The intermarket fan experiment (`fan_intermarket_v2`)

Step 3 of the fan ([docs/fan.md](fan.md)): a learned fan that reads every collected
instrument, accepted only where it forecasts NQ's price distribution better than the
benchmark fan, and a measure of how much each instrument contributes. Every choice
below is fixed in a registered manifest (kind `fan_experiment`,
[forecaster/fan_experiment.py](../forecaster/fan_experiment.py)) before any model
exists; a changed choice is a new experiment version.

**Registered** 2026-10-06 10:45 UTC, definition hash `5adc22ef9b82116d`. It supersedes
`fan_intermarket_v1` (10:33 UTC, `b82d81855ad21b06`), replaced before any result: v1's
window waited for the instrument with the latest start (RTY's active series, 2025-09-18,
which IB cannot extend); v2 starts the window with NQ's history and reads every other
instrument from its own start - 243 development sessions instead of 181. The holdout is
unchanged and sealed: no model has been frozen. The seal follows the newest version, so
the superseded v1 never holds it shut.

## Decisions (2026-10-06)

| Question | Decision |
|---|---|
| Which forecast | The anytime fan: every minute is an origin. The pre-open forecast is a declared slice (the 09:29 origin) |
| Holdout | The last 60 full sessions to 2026-10-05, fixed by date |
| Instruments | Every collected instrument, in groups; 2YY dropped entirely (its data removed, migration 0018). The window starts with NQ's history; an instrument that starts later is read from its own start, its values missing before it. The window follows what is stored and adjusts with backfills |
| Benchmark first | Yes: a rule-based `fan_rw_v2` before any learned model |
| Primary | NQ at 15 minutes; ES and RTY are secondary, reported only |

Why the anytime fan: measured on the benchmark's own scores, a test on 65 held-out
sessions can confirm a gain of about 2 % of CRPS when every minute is scored, but only
about 9 % from the 09:29 origin alone. A model that reads the other instruments will
realistically change the fan's width by a few percent.

## The split

Resolved from the stored data at registration (`scripts/fan.py experiment-register`);
`scripts/fan.py experiment-show` prints the registered values.

- **Holdout:** the last 60 full sessions to 2026-10-05 (2026-07-13 to 2026-10-05). Fixed
  by date, so no backfill moves it.
- **Window:** starts at NQ's first complete session (2025-06-18; IB serves no older
  expired NQ contracts). Every other instrument is read from its own first complete
  session - the active contract's series where the symbol rolls - and its values are
  missing before it. An instrument is included when it is complete from at least 75 % of
  the development sessions on; one that starts later is deferred until its history is
  backfilled, which a new version takes up.
- **Late starters on 2026-10-06:** RTY from 2025-09-18 (83 % of development), DX from
  2025-09-12 (84 %), 10Y from 2025-09-03 (87 %), IWM from 2025-08-11 (94 %). Every other
  instrument covers all of development.
- **Development:** full sessions from 20 sessions into the window (feature warm-up) to the
  last one before the holdout: 2025-07-21 to 2026-07-10, 243 sessions.
- **Checks:** the last 3 x 30 development sessions in consecutive blocks, each trained on
  every development session before it (153, 183 and 213 sessions):
  2026-03-03 to 04-14, 04-15 to 05-27, 05-28 to 07-10. Every instrument is present
  throughout them.
- Full sessions only: early closes are not scored, as in the benchmark's scoring.

## Horizons and their roles

| Role | What |
|---|---|
| Primary | NQ, 15 minutes ahead, every origin: the verdict |
| Secondary | 5, 30 and 60 minutes; ES and RTY at 15 minutes; the pre-open slice from the last bar completed by 09:29 ET to 09:45, 10:00 and 10:30 (16, 31 and 61 minutes) |
| Exploratory | 1, 20, 120 and 240 minutes, and to the regular close; by origin phase; origins with a release ahead |

The primary is never switched to whichever horizon scores best.

## Score and gates

- **Score:** CRPS of the log price in basis points, computed the same way for every
  version on both sides of a comparison: 2/K x the summed pinball loss of the issued
  quantiles at tau = (k - 1/2)/K, K = 200.
- **Origins scored:** every origin with a last price at the origin and at the endpoint,
  inside the trading day; an origin where the baseline's spread is zero (the market
  closed) is not scored for either side.
- **Comparison:** model minus baseline per session (the mean over its origins), paired on
  identical sessions and origins; 95 % interval by a moving-block bootstrap of the
  session differences in date order (5-session blocks, 2000 resamples).
- **Baseline:** `fan_rw_v1`, replaced by `fan_rw_v2` if v2's difference against v1 (NQ,
  15 minutes, the three checks' sessions) has its whole interval below zero - decided
  before any model is frozen. **Decided 2026-10-06: `fan_rw_v2`** (-0.32 %, interval
  [-0.0385, -0.0075] bps; journal result `72b66bb7`,
  [report](reports/fan_rw_v2_gate_fan_intermarket_v2.md), [docs/fan.md](fan.md#version-2-fan_rw_v2)).
- **Verdict on the holdout:** pass when the whole interval lies below zero; inconclusive
  when it includes zero (not promoted on this evidence); fail when it lies above.
- **Drawing:** the model draws a target's horizon only where its own holdout interval
  lies below zero; the baseline draws the rest.
- A pass says the fan forecasts the size of moves better - nothing about direction or
  profit.

## Data rules

- A session is scored for a target when it is full in the calendar and the target's
  active-contract day was complete in the store at registration; the manifest lists the
  exclusions: none for NQ and ES, and RTY's 42 development sessions before its own start.
- A context instrument's value at a minute is the close of its last bar at or before
  that minute, with that bar's age; missing before its first bar of the trading day; a
  context gap never excludes a session.
- A forecast from minute t reads only what is known at t.

## The point-in-time panel (chunk 1)

[forecaster/fan_panel.py](../forecaster/fan_panel.py) puts every instrument on the
target's minute grid (the 1440 slots from 18:00 ET the evening before): at each minute,
the close of its last bar closed by then, that value's age in minutes, and the minute's
volume. A day is read on its active contract, carry included, so a roll never mixes
contracts; tests show nothing after a minute is read and that NQ's values match the
fan's own grid. 181 sessions x 12 instruments load in about 4.5 s.

The audit of v2's development sessions
([docs/reports/fan_panel_audit_fan_intermarket_v2_2025-07-21_2026-07-10.md](reports/fan_panel_audit_fan_intermarket_v2_2025-07-21_2026-07-10.md))
found every instrument complete from its own start, with one gap - SPY has no day for
2025-08-13, carried from the day before like any context gap - and hours that features
must respect:

| Instrument | Trades (ET) | Its value's age at 08:00 / 09:29 |
|---|---|---|
| NQ, ES, RTY, 10Y | 18:00-17:00 | current / current |
| DX | 20:00-17:00 | current / current |
| QQQ, SPY, IWM, SMH | 04:00-20:00 | current / current (7 h old at 03:00) |
| VIX | 03:15-09:15, 09:30-17:00 | current / 15 min (the pre-open pause) |
| TNX | 08:15-15:00 | 17 h / current |
| VXN | 09:30-16:00 | 16 h / 17.5 h: yesterday's close |

Overnight, rates come only from the 10Y future and the volatility indices only from VIX
(from 03:15); before the open VXN is the previous day's close. A feature reads a value's
age beside it, and a stale value is never filled in.

## Measuring what each instrument contributes

On the three checks only, for NQ: by group (other index futures, volatility indices,
rates, dollar, equity ETFs) and by single instrument -

- **drop one:** retrained without it - what is lost;
- **add one:** the own-instrument model plus it only - what it adds;
- **Shapley share:** each group's average contribution over every order of adding groups;
- **precision:** band width and band coverage, with and without it.

Each with a paired interval; one that includes zero is reported as no detectable
contribution.

## The learned model: where it fits and when it is trained

Noted 2026-10-06, before chunk 3.

**Where it fits.** The model does not replace the benchmark: it sits on top of
`fan_rw_v2` and scales its width. v2 gives, from any minute, the spread to each horizon
(sigma_h) and the fat-tailed shape (Q_h); the model gives one number per origin and
horizon - how much wider or narrower than v2 the fan should be:

```
fan_rw_v2 (rules)  ->  sigma_h, shape Q_h
                              |
panel (every instrument,      v
point in time) -> features -> model -> multiplier m_h -> fan = sigma_h x m_h x Q_h
```

- It learns the size of moves only: v2's median (zero drift) is kept, so it makes no
  claim about direction - the size of NQ's moves is predictable, their direction was
  found not to be.
- It reads the features of chunk 4, built on the point-in-time panel
  ([forecaster/fan_panel.py](../forecaster/fan_panel.py)): each instrument's last value
  and that value's age at the origin, never anything after it.
- In the code: a module beside [forecaster/fan_v2.py](../forecaster/fan_v2.py) that
  wraps v2; registered as kind `fan_model` naming this experiment and its hash; scored
  by the harness against v2 on identical origins.
- On the chart (chunk 9) it draws only the horizons whose holdout interval lies below
  zero; v2 draws the rest. The P1 arms A-D are untouched - they answer another question.

**When it is trained.**

1. Not before the harness (chunk 3) and the features (chunk 4) are fixed. Trained
   first, the evaluation or the features could be picked to flatter it, and its verdict
   would mean nothing.
2. Development: on each check in turn, from the development sessions before it only
   (153, 183 and 213 sessions). Own instrument first, then every instrument, then each
   instrument's contribution (chunk 6).
3. Freeze: trained once on all 243 development sessions and registered (chunk 7). Only
   that frozen model opens the holdout, and it is scored there once.
4. After a pass it stays frozen through the forward record (chunk 8); refitting it on
   newer sessions is a new registered version. The rule-based baseline refits itself
   every session; the learned model does not.

Training reads only the stored data, offline - no market hours, no IB connection; what
constrains it is the order above, not the clock.

**What to expect.** 243 sessions are enough for a small, heavily regularised gradient
boosting model, not a large one: origins within a session are strongly correlated, so
the real sample is nearer 243 than the ~68,000 rows of a 5-minute sampling. The
instruments are expected to change the fan's width by a few percent; scored at every
origin, the 60-session holdout can confirm a gain of about 2 %. A modest real effect is
detectable, and an inconclusive verdict is entirely possible.

## The checks harness (chunk 3)

[forecaster/fan_harness.py](../forecaster/fan_harness.py) runs a candidate - a learned
fan - against the decided baseline on the three checks, as the manifest fixes them:

- **The baseline as issued.** Every development session's `fan_rw_v2`, walk-forward:
  from every origin its variance to each horizon, its shape and the releases ahead (a
  *frame*). Computed once from the stored bars (about 30 s for NQ) and cached in
  `data/fan_cache/` (not in git), keyed by the experiment, the baseline's definition and
  the estimator code; `--refresh` recomputes after a backfill. Its CRPS reproduces the
  gate's v2 column exactly.
- **Rows.** One per origin and horizon: the baseline's variance, the realised move, the
  origin's phase, a release ahead. Training rows sample the origins on the 5-minute marks
  (153 sessions give about 323,000 rows); the checks score every origin. Both add the
  pre-open slice's origin at 16, 31 and 61 minutes.
- **A candidate** is fitted on the training rows of the development sessions before its
  check - never the check's own or later sessions (tested) - and gives each row a
  multiplier of the baseline's sigma; its fan keeps the baseline's shape. A fresh
  candidate per check. Features (chunk 4) are the candidate's own: a row names its session
  and origin.
- **Comparisons.** Candidate minus baseline per session on identical origins, paired
  intervals per check (30 sessions) and pooled (90), for every report horizon, every
  horizon x origin phase, origins with a release ahead, and the pre-open slice. Below 10
  sessions (two bootstrap blocks) there is no interval.
- **Calibration and concentration** (added 2026-10-06): every report also gives, for both
  sides, the coverage of the central 50 / 80 / 90 / 95 % bands, the misses below and above
  each band separately, the bands' widths and the PIT deciles - per horizon and, at the
  primary horizon, per phase - and how the primary gain spreads over the sessions (the
  share improved, the five best sessions' share of the gain, each calendar week left out
  in turn).
- **Sampling offset:** training origins can start at any minute of the five
  (`run_checks(..., offset=k)`); the check origins are always every minute.
- **Stored runs:** every run's per-session scores and its feature list with their hash go
  to `data/fan_cache/checks/` (not in git); `scripts/fan.py compare A B` pairs two runs on
  identical sessions and origins - B minus A, with the manifest's interval - and refuses
  runs whose baselines differ.
- **Nothing is stored** in the journal: a check's result is development. The report goes
  to `docs/reports/fan_checks_<experiment>_<target>_<candidate>.md`.

Two reference candidates: `identity` (the baseline itself - every difference exactly
zero, the harness's self-check) and `phase_scale` (one constant width per horizon and
origin phase, learned from the training rows). On 2026-10-06 `phase_scale` beat v2
nowhere that matters: inconclusive at 15 minutes (+0.03 %), worse at 60-240 minutes and
in the pre-open slice
([report](reports/fan_checks_fan_intermarket_v2_NQ_phase_scale.md)). v2's width by time
of day is already right, so a learned model has to read something besides the clock to
gain.

Not built yet: the exploratory horizon to the regular close (a horizon that varies with
the origin), and the attribution's precision measures (band width and coverage, chunk 6).

## The features (chunk 4)

[forecaster/fan_features.py](../forecaster/fan_features.py) computes, from the
point-in-time panel, what a candidate reads at an origin - 133 instrument features over
the 12 instruments plus 4 base columns, each tagged with its instrument and group (the
target's own instrument is `own`; the base columns are in every model). Format 3 (`iv_rv`
and `rv5d` added on 2026-10-06, below):

| Family | What, at origin t | Instruments |
|---|---|---|
| `rv5`, `rv15`, `rv60`, `rv240` | realised variance of the last w minutes over the usual for those minutes (log ratio): moving more than usual for the time of day | all |
| `ret15`, `ret60` | the return over the last w minutes, in the usual sigma | all |
| `chg` | the change since the previous session's regular close (13:00 after an early close), in the usual sigma | all |
| `vol60` | volume over the last 60 minutes over the usual (log ratio) | futures and ETFs (an index has no volume) |
| `age` | log(1 + minutes since its last bar) | all |
| `day_rv` | the previous session's realised variance over the usual | all |
| `rv5d` | the log of its mean realised daily variance over the 5 sessions before (all 5 with data) | all |
| `level` | the log of its value | VIX, VXN (implied volatilities) |
| `iv_rv` | the daily variance the index implies, (value / 100)^2 / 252, over the target's mean realised daily variance of the 5 sessions before (log ratio): what the options market expects against what the target has done | VIX, VXN |
| `base.*` | the baseline's sigma to the row's horizon, a release ahead, the minute of the day, the weekday | - |

"Usual" is the mean over the previous 20 full sessions with data - missing with fewer
than 10, so a feature needs history before it has a value. An instrument's features are
missing before its first complete session; a value with no new bar keeps its age beside
it and is never filled in; a ratio or standardised move whose usual is zero (the
instrument is shut at that minute, e.g. VXN overnight) is missing, not zero. Missing is
NaN, which the gradient boosting of chunk 5 reads as missing. Tests show nothing after
the origin and nothing from a later session is read.

`scripts/fan.py features` builds the table for every open session from the window start
to the end of development (266 sessions, 8 s - no cache needed) and screens each feature
on the 153 development sessions before the first check, origins every 5 minutes, 15
minutes ahead: its coverage and its rank correlation with |z|, the realised move in the
baseline's sigmas ([report](reports/fan_features_fan_intermarket_v2_NQ.md)). On
2026-10-06 the strongest were "moving more than usual" - VXN, RTY, ES, NQ itself and VIX
(+0.10 to +0.12): when they move more than usual, v2 is too narrow. Recent falls go with
larger errors (`ret60` -0.08) - the size of the next move, not its direction. The rates
and the dollar showed little (at most +0.05). A univariate screen only; chunk 5's model
decides what matters together.

## The learned fan (chunk 5)

[forecaster/fan_model.py](../forecaster/fan_model.py), a checks candidate wrapping
`fan_rw_v2` as the note above describes:

- **Per horizon** (1, 5, 15, 20, 30, 60, 120, 240 minutes), gradient boosting
  (scikit-learn's `HistGradientBoostingRegressor`, missing values read as missing) of
  z^2 - the squared realised move in v2's sigmas - with Poisson deviance: it estimates
  E[z^2 | features] multiplicatively, and an extreme move pulls a leaf linearly, not
  quadratically.
- **The multiplier** of v2's sigma is sqrt(prediction / its mean over the training rows),
  clipped to [0.5, 2]: the model moves width between origins, and over the training rows
  the mean *squared* multiplier is 1 (before clipping). That fixes neither the average
  variance nor the average width - v2's variance changes from row to row, and the
  multiplier is larger where v2 is already wide. Measured for `lin_pois_ivx` on the
  checks' origins (2026-10-06, every origin, against v2): the mean width (sigma times the
  multiplier) +0.4 % at 15 minutes, within +-0.6 % from 1 to 120 minutes, -4.6 % at 240;
  the mean variance +6 to +8 % from 1 to 120 minutes, -3.5 % at 240; the mean squared
  multiplier 1.00-1.03 and the median multiplier 0.95-0.99. v2's median and shape are
  kept.
- The pre-open slice (16, 31, 61 minutes from 09:28) uses the nearest trained horizon.
- **Settings:** learning rate 0.05, 150 trees of depth 3, at least 4,000 rows a leaf
  (about 15 sessions of 5-minute origins), L2 1.0, 30 % of the features at each split.
- Candidates, each with an explicit feature list fixed when named (`FEATURE_SETS`):
  `gbm_own` (the base columns and NQ's own 10 features of format 2), `gbm_own_iv` (those
  and `iv_rv` of the target's own volatility index - VXN for NQ, the implied volatility of
  Nasdaq-100 options; VIX for ES and RTY), `gbm_own_ivx` (NQ's own with `rv5d`, and VXN's
  `level` and `iv_rv`: both sides of the comparison and their ratio - the ablation's E),
  `gbm_all` (every feature); from the second review round `lin_pois_ivx`, `gam_ivx`,
  `crps_ivx` and `crps_scale` (below). A gradient-boosting fit per horizon and check takes
  about 1 s, a check run 20-60 s; `crps_ivx` about 8 minutes.

**Attempts on the checks** (development: any number allowed, every one listed; NQ, CRPS
change against v2, the checks' 90 sessions; * = whole interval below zero, ! = above):

| # | Settings | Candidate | 1 min | 5 min | 15 min (primary) | 30 min | 60 min | Pre-open 16 / 61 |
|---|---|---|---|---|---|---|---|---|
| 1 | leaf 2,000, every feature at each split | gbm_own | -0.35 % * | -0.32 % * | -0.23 % * | -0.13 % | -0.16 % | +1.04 % ! / +0.26 % |
| 1 | same | gbm_all | -0.32 % * | -0.24 % * | -0.06 % | +0.20 % | +0.27 % | +1.74 % ! / +2.22 % ! |
| 2 | leaf 4,000, 30 % of features at each split | gbm_own | -0.35 % * | -0.34 % * | **-0.24 % *** | -0.22 % * | -0.23 % | +1.04 % ! / +0.42 % |
| 2 | same | gbm_all | -0.33 % * | -0.28 % * | -0.09 % | +0.07 % | +0.12 % | +1.43 % / +1.43 % ! |
| 2b | 2 with learning rate 0.03, 100 trees | gbm_all | -0.33 % * | -0.28 % * | -0.14 % | -0.01 % | -0.04 % | +1.00 % / +0.88 % ! |
| 3 | 2, features format 2 (`iv_rv`, below) | gbm_own_iv | -0.36 % * | -0.37 % * | **-0.32 % *** | -0.31 % * | -0.42 % * | +0.36 % / -0.11 % |
| 3 | same | gbm_all | -0.35 % * | -0.33 % * | -0.23 % * | -0.09 % | -0.08 % | +1.02 % / +0.88 % |
| 4 | 2, features format 3 (`rv5d`) | gbm_own_ivx | -0.37 % * | -0.41 % * | **-0.40 % *** | -0.44 % * | -0.53 % * | +0.20 % / -0.15 % |
| 4 | same | gbm_all | -0.35 % * | -0.33 % * | -0.22 % * | -0.10 % | -0.09 % | +0.98 % / +0.84 % |

Attempt 2's settings are kept
([gbm_own](reports/fan_checks_fan_intermarket_v2_NQ_gbm_own.md),
[gbm_own_iv](reports/fan_checks_fan_intermarket_v2_NQ_gbm_own_iv.md),
[gbm_own_ivx](reports/fan_checks_fan_intermarket_v2_NQ_gbm_own_ivx.md),
[gbm_all](reports/fan_checks_fan_intermarket_v2_NQ_gbm_all.md)). What the checks show,
2026-10-06:

- **NQ's own features beat v2.** `gbm_own` lowers CRPS by 0.24 % at 15 minutes (interval
  [-0.027, -0.006] bps), better at 1, 5, 15, 20 and 30 minutes, the same sign to 240.
  Its gain is largest overnight (-0.33 % to -0.45 % at every horizon to 60 minutes, each
  interval below zero), then midday and afternoon. The multiplier stays modest: 5 / 50 /
  95 % of 0.79 / 0.95 / 1.20 at 15 minutes.
- **Every instrument does not beat NQ's own; one cross-market quantity does.** `gbm_all`
  gains at 1 to 15 minutes only: its 121 instrument features mostly restate NQ's own, and
  on 153-213 training sessions it spends splits on them or overfits them. `gbm_own_iv` -
  NQ's own and VXN's implied over NQ's realised variance - is better than v2 at every
  horizon from 1 to 120 minutes (-0.32 % at 15, -0.42 % at 60, -0.55 % at 120) and better
  than `gbm_own` itself (next section).
- **The open is not learned.** With `gbm_own`, forecasts from before 09:30 that reach past
  it are worse (pre-open phase +0.2 % to +0.5 % from 15 minutes; the slice +1.0 % at 16
  minutes; `gbm_own_iv` +0.4 %, inconclusive): the
  pre-open's activity says little about the size of the open, and with origins every 5
  minutes only about three training rows a session cross it - too few for a leaf. Not
  patched: by the manifest's drawing rule v2 keeps any horizon the model does not pass.
- **The gain is small but measurable.** About a quarter of a percent, against the "few
  percent" expected; but paired against a near copy of itself, v2's difference is tight -
  the 90 sessions' interval at 15 minutes spans about 0.3 % of CRPS, far narrower than the
  2 % the plan's power estimate assumed for the holdout. A gain this size can pass on 60
  holdout sessions, and it can as easily come out inconclusive.

## Why the other instruments added little - and what does (research, 2026-10-06)

Asked after chunk 5: why does reading every instrument not beat NQ's own, and can the
context be made to add something? Three questions, on the development sessions only
(the holdout stays sealed):

1. **Is their information already in NQ's own? Mostly, yes.** Out of sample on the
   checks (15 minutes, origins every 5 minutes, 24,570 rows), after `gbm_own` no existing
   context feature is related to what is left (|z| over `gbm_own`'s multiplier): every
   rank correlation is within +-0.04. ES's 15-minute realised variance correlates 0.93
   with NQ's own features, RTY's and QQQ's 0.83: on 1-minute bars these markets move
   together within the minute, so their recent volatility restates NQ's - there is no
   lead to exploit. The rates and the dollar move most around scheduled releases, which
   v2 already widens for by name.
2. **Does any group help when added alone? No - each costs.** NQ's own plus one group,
   CRPS against v2 at 15 / 30 / 60 minutes: own -0.24 / -0.22 / -0.23 %; + index futures
   -0.17 / -0.10 / -0.03; + volatility -0.21 / -0.16 / -0.16; + rates -0.12 / -0.02 / -0.06;
   + dollar -0.18 / -0.02 / +0.02; + equity ETFs -0.21 / -0.13 / -0.16. Redundant features
   take splits and fit noise.
3. **Were the features built to carry what only other markets know? No.** They measured
   each instrument's own recent volatility - the same information again. Three kinds of
   cross-market feature were prototyped and screened the same way (what is left after
   `gbm_own`, on the checks):

   | Prototype | What it carries | Rank correlation with what is left |
   |---|---|---|
   | VXN's implied over NQ's realised daily variance (5 sessions) | what Nasdaq-100 options expect against what NQ has done - forward-looking | **+0.074** |
   | the same with VIX (S&P 500 options) | the same, less specific to NQ | +0.046 |
   | QQQ / SPY / SMH volume since 04:00 over usual | premarket news intensity | -0.017 to +0.007 |
   | NQ's realised variance over ES's / SMH's (15, 60, 240 minutes) | NQ-specific against market-wide | -0.009 to +0.011 |

   VXN's `level` was already a feature, but NQ's 5-day realised variance was not - so no
   model had both sides of the comparison (see the ablation below). Given the ratio:

   | VXN `iv_rv` added to NQ's own, against `gbm_own` (paired) | 5 min | 15 min | 30 min | 60 min | 120 min | 240 min |
   |---|---|---|---|---|---|---|
   | the checks pooled (90 sessions) | -0.04 % | **-0.08 % *** | -0.09 % | **-0.19 % *** | -0.19 % | -0.34 % |
   | check 1 / 2 / 3 | -0.02 / -0.06 / -0.04 | -0.05 / -0.17 / -0.05 | -0.04 / -0.13 / -0.10 | -0.16 / -0.19 / -0.22 | -0.20 / -0.20 / -0.17 | -0.48 / -0.24 / -0.27 |

   Better in every check at every horizon, and more so further ahead - as forward-looking
   information should be. Adding the whole volatility group with it dilutes the gain
   (-0.08 % at 15 minutes, mixed beyond); the old volatility features without it add
   nothing (+0.03 % at 15, worse at 120).

**What follows.** The value of the other markets lies in information NQ's own prices do
not hold - here, the options market's expectation - not in their own recent volatility.
`iv_rv` is now a feature (format 2) and `gbm_own_iv` a candidate.

**Ablation: is it the ratio, or one of its parts?** (2026-10-06, after an outside review
asked whether the denominator alone explains the gain). Same training, settings and check
sessions; every candidate NQ's own features plus the named ones; CRPS change paired against
`gbm_own` (* = whole interval below zero, ! = above):

| Added to NQ's own | 5 min | 15 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|
| B: NQ's 5-day realised daily variance (the denominator) | +0.03 % ! | +0.04 % | +0.03 % | +0.01 % | +0.08 % | -0.06 % |
| C: VXN level | -0.01 % | -0.04 % | -0.06 % | -0.05 % | -0.02 % | -0.12 % |
| D: both (B and C), no ratio | -0.03 % * | -0.10 % * | -0.13 % * | -0.22 % * | -0.15 % | -0.37 % * |
| F: VXN `iv_rv` (`gbm_own_iv`) | -0.04 % | -0.08 % * | -0.09 % | -0.19 % * | -0.19 % | -0.34 % |
| E: B, C and `iv_rv` | -0.08 % * | -0.17 % * | -0.20 % * | -0.28 % * | -0.26 % | -0.41 % * |
| G: `iv_rv` from the previous session's VXN close | -0.03 % | -0.05 % | -0.04 % | -0.16 % | -0.21 % | -0.28 % |

Neither part alone helps (B, C); both together do (D), about as much as the ratio alone (D
against F: within +-0.05 %, every interval across zero). Both parts and the ratio (E) beat
the ratio alone at 5, 15 and 30 minutes (-0.04 / -0.09 / -0.11 %, each interval below
zero). The previous close's VXN (G) does slightly worse than the latest (+0.03 % at 15
minutes against F, inconclusive): most of the signal is the slower volatility regime, a
little is VXN's intraday moves. About 15 candidates have now been compared on the same 90
check sessions; their nominal intervals overstate, and only the holdout confirms.

**Review follow-up** (2026-10-06, an outside review's list; E = `gbm_own_ivx`, CRPS change
on the checks, * / ! as above):

| Test | Result | So |
|---|---|---|
| E as a named candidate | -0.40 % at 15 min, better than v2 at every horizon 1-120 min; better in 78 % of sessions, the five best carry 42 % of the gain, no week's removal undoes it | the leading candidate |
| Calibration of E (report) | 90 % bands cover 89.8-90.4 % at every horizon, misses even on both sides; v2's 240-minute bands were too wide (91.7 %), E's are not (90.4 %); E's 50 % band covers 47.6 % - its middle narrow | tails right; the middle significantly narrow (second round, with intervals) |
| Multiplier at its bounds [0.5, 2] | 0.00 % of origins, every phase | the clip decides nothing |
| Training origins at minute 1, 2, 3 or 4 of the five | -0.37 / -0.39 / -0.40 / -0.39 % at 15 min, every horizon to 120 better | does not depend on which minutes train |
| Early stopping, randomness | off (`early_stopping=False`); the feature sampling has a fixed seed | every fit deterministic, no random validation split |
| One CRPS-fitted scale per horizon | +0.05 % at 15 min (slightly worse than v2); E against it -0.45 % * | **withdrawn**: it chose its scale with the normal's quantiles and was scored with v2's real shape - no evidence either way; redone as `crps_scale` (second round) |
| A linear Poisson model on VXN level and NQ's 5-day realised variance | -0.23 % * at 15, -0.42 % * at 60; E against it -0.27 / -0.24 / -0.17 % * at 1 / 5 / 15 min, -0.11 % at 60 (inconclusive) | the long horizons' gain is nearly linear in the volatility regime; it had less information than E, so it says nothing about whether trees are needed (redone with E's inputs, second round) |
| A calibration and shrinkage layer a x m^b, chosen on the last 30 training sessions (nested) | chose b = 1.25, 0.75, 0.75; against E +0.05 % ! at 15, worse to 120 | this procedure did not help - its objective summed raw 15- and 60-minute CRPS (the 60 minutes weigh more) and applied one correction to every horizon, from 30 sessions; it does not show that E never over-reacts |
| E's shape refitted on its own standardised errors | against E -0.01 % at 15 (inconclusive), -0.02 % * at 5 | not a predictive test: the errors were the training rows' own (in sample) |
| HAR: NQ's 1-, 5- and 22-day realised variance | against E +0.03 % ! at 1 min, -0.01 % at 15 | no gain: v2's 5-session level and `rv5d` hold the memory |
| Semivariance: NQ's falling- and rising-price variance, 15 / 60 / 240 min | against E -0.01 % * / -0.02 % * at 1 / 5 min, nothing beyond | negligible: `ret60` and `chg` already carry falls |

Not done in that round: a CRPS-trained model (done in the second round, `crps_ivx`; the
nested layer above was not equivalent), NGBoost and quantile boosting (quantile boosting
can make the fan asymmetric, which this manifest's zero drift and symmetry exclude; NGBoost
need not - with a symmetric distribution and its location fixed at zero it would learn
only a scale, but it would replace v2's empirical shape with a parametric one), Hansen's
SPA over the candidates (before freezing, chunk 7), an opening-specific correction, the
feed-outage audit (done in the second round) and realised variance from 5-minute instead
of 1-minute returns.

**Second review round** (2026-10-06, a bounded round on a second outside review; every
candidate's features now an explicit list with its hash, every run stored per session so
any two can be paired - `scripts/fan.py compare`):

| Test | Result (CRPS on the checks; paired where stated) | So |
|---|---|---|
| Feature lists frozen | each candidate names its columns (`FEATURE_SETS` in forecaster/fan_model.py); a run records them and their hash (`gbm_own_ivx`: 17 columns, `7c0f6e1c82ddb538`); every earlier candidate reproduced to the last digit | no selector can take in a feature added later; since the third round `gbm_all` too reads a fixed list (`all_f3`, 133 instrument features) |
| `crps_scale`: one multiplier per horizon minimising the training rows' exact CRPS, each row with its own session's shape | -0.00 % at 15 min; within +-0.04 % at every horizon, all inconclusive | no constant rescaling improves the checks - narrower than "v2's width is right" |
| `lin_pois_ivx`: a regularised linear Poisson model with E's information (16 features without the redundant ratio, phase and weekday as dummies, L2 1e-3 fixed beforehand) | -0.37 % * at 15 min, better at 1-60 min; against E: within +-0.07 % at every horizon, all inconclusive | **trees are not shown to be needed** |
| `gam_ivx`: E's features and settings, no interactions (additive) | -0.35 % * at 15 min; against E: E better at 1 / 5 / 15 min by 0.07 / 0.06 / 0.05 % *; against the linear model: within +-0.07 %, worse only at 1 min | interactions add a little over an additive fit, nothing over the linear one |
| `crps_ivx`: E's features and tree size, boosted on the exact CRPS (log multiplier, line-searched steps, v2's zero median and shape kept) | -0.37 % * at 15 min, better at 1-120 min; against E: within +-0.12 %, all inconclusive | training on the score itself changes nothing measurable |
| Calibration at 15 min with session-block intervals (v2 / E / linear) | central 50 % band: 48.9 % [47.1, 51.0] / 47.6 % [46.7, 48.7] / 47.9 % [47.2, 48.7] - E and the linear model narrow in the middle in most phases; 90 % misses 4.4-5.9 % a side, balanced overall; after the close the middle is too wide for all (57-61 %: that hour often does not move at all); E's misses lean to the downside at midday (+1.4 % [0.2, 2.9]) and, borderline, in the opening hour (+2.3 % [-0.0, 4.6]) - v2's with the same sign | the middle band is the concrete defect (one explanation: the scaled models keep v2's peaked shape after the scaling has made the errors less peaked - not shown); the downside lean is direction, which a symmetric width cannot fix; no opening-specific defect established - which says nothing about how well the fan serves an opening-trading objective |
| Feed outages: runs of 5+ minutes without a bar inside an instrument's usual hours (a bar in 99 % of its sessions), every development session | NQ, ES, RTY, VIX, VXN, QQQ, SPY, IWM, TNX: none; 10Y 2 (154 and 80 min from 19:40 ET), DX 1 (10 min), SMH 2 premarket; 0 of 66,338 rows read an outage in NQ's or VXN's window; SPY's missing 2025-08-13 has since been collected complete | no outage reads as a calm market in the leading candidates' inputs |

**What the round shows.** The gain is in the information - NQ's own intraday state and
VXN's implied against NQ's realised variance - not in the model: a regularised linear
model, an additive model, trees on Poisson deviance and trees on the CRPS itself all reach
-0.35 to -0.40 % at 15 minutes and cannot be told apart. Of indistinguishable candidates
the simplest is the safer one to freeze: `lin_pois_ivx` (about 30 coefficients a horizon
against 150 trees). That choice, and the multiple-comparison review it needs - some 25
candidates have now met the same 90 sessions - come before chunk 7.

**Open after this round:** a candidate's own symmetric shape fitted out of sample
(chronological predictions within the training period), for the narrow middle;
Hansen's SPA over every candidate run; realised variance from 5-minute returns.

**Third review round: nomination, replay, one shape test, the search under review**
(2026-10-06, on a third outside review):

- **Nominated, provisionally: `lin_pois_ivx`**, with `gbm_own_ivx` as its benchmark. The model
  search stops here: no new features or model classes on these 90 check sessions.
- **Every definition frozen and recorded.** `gbm_all` now reads a fixed list too (`all_f3`).
  Each stored run records the code revision, a fingerprint of the stored data it read
  (count and exact decimal sums of every included instrument's development bars - a
  backfill or a revised bar changes it; `873e55e084d746bf` on 2026-10-06), the feature
  and frame formats, the candidate's specification and settings, its feature list and
  hash, and the fitted state (the linear models' imputation, standardisation and
  coefficients per horizon and check). Fifteen earlier trials were rebuilt from explicit
  definitions (`TRIALS` in forecaster/fan_model.py) and reproduce their logged results.
- **Live-style replay** (`scripts/fan.py replay`,
  [report](reports/fan_replay_fan_intermarket_v2_NQ.md)): nine sessions picked by rule
  from what was known before each opened - three release days, three after the most
  volatile previous sessions, three ordinary - at 19:00, 03:00, 08:25, 09:28, 10:30 and
  15:30 ET. Everything rebuilt from the store as of the issuance (`load_panel(as_of=...)`,
  `load_days(as_of=...)`): all 7,182 features equal to the research table; an issuance 30 s
  into the next minute reads nothing of the forming bar; v2's variance from the incomplete
  session equal to the cached frames; both candidates' multipliers from the replayed inputs
  equal to the research batch (largest relative difference 3e-16), and one origin alone
  equal to the batch. **Passed.** Not replayable: a bar the collector stores as preliminary
  (`is_completed = 0`, within 2 hours of collection) can be revised later and history keeps
  only the revision - the forward record (chunk 8) must log live inputs.
- **One shape test** (`lin_pois_ivx_shape`: the nominee's multiplier with its own
  symmetric shape per horizon from chronological out-of-sample residuals inside each
  training period - blocks of 30 sessions from the 60th, each predicted by a model fitted on
  the sessions before it). It fixes the middle at 15 minutes (50 % band 49.5 % against
  47.9 %; 90 % interval score 54.55 against 55.53 bps for v2) but makes the long horizons'
  tails too wide (90 % bands cover 91.5-94.4 % at 30-240 minutes), and its CRPS against the
  nominee is -0.01 % at 15 minutes (inconclusive), worse at 1 minute (+0.07 %), at 240
  (+0.76 %) and in the 61-minute pre-open slice (+0.93 %). **Not adopted**: the nominee keeps
  v2's shape; the narrow middle stays a known, cheap defect. (The manifest does not fix a
  candidate's shape - the zero drift and v2's shape were this design's choice.)
- **The search under review** (`scripts/fan.py search`,
  [report](reports/fan_search_fan_intermarket_v2_NQ.md)): 25 stored runs - every candidate
  and every rebuildable trial; the trials that left no definition and the two invalidated
  runs are listed in the report - at 15 minutes, session losses, 5-session blocks:

  | Benchmark | SPA p (lower / consistent / upper) | StepM names (5 % family-wise) |
  |---|---|---|
  | fan_rw_v2 | 0.002 / **0.003** / 0.003 | 12 runs, the nominee among them (t 3.10, critical 2.38) |
  | gbm_own (runs that read another market) | 0.003 / **0.004** / 0.004 | `gbm_own_ivx` (t 3.59), `crps_ivx`, `gam_ivx`, `gbm_own_iv` - not the nominee (t 2.24, critical 2.59) |

  Beating v2 survives the search, the nominee's own gain included. That reading VXN beats
  NQ's own features also survives - but it is established individually for the tree models
  only; the nominee's -0.13 % against `gbm_own` has the right sign without clearing the
  search's bar (part of its difference is the model class: `gbm_own_ivx` against `gbm_own`
  is a like-for-like pair). Development only: the screens that chose features and the
  trials without a definition are not in the test.

**Before the freeze** (2026-10-06, a fourth outside review: freeze `lin_pois_ivx`, unchanged,
after three reproducibility fixes):

- **Data snapshot, row by row.** The count-and-sum fingerprint could miss offsetting
  changes. `fan_panel.data_snapshot` now hashes every stored row a computation can read, in
  a fixed order with timestamps as epoch seconds (no session time zone changes the text):
  every bar of the included instruments up to the end of development (all columns, every
  contract), their session days, contracts and active-contract mapping, the economic
  events and their coverage, and the calendar's version - each component hashed and one
  fingerprint of them all (`59bd1c5a0daa6fd1` on 2026-10-06; 6 s; stable across calls and
  session time zones).
- **Source snapshot.** `provenance.source_snapshot` hashes every file git tracks or would
  track (the outputs under docs/, data/ and logs/ aside) with the commit, whether the tree
  differs from it, the Python version and the numerical packages' versions; a freeze also
  archives the files (`data/fan_cache/snapshots/<snapshot>.tar.gz`), so a '+dirty' tree stays
  recoverable. Every stored run and the freeze record both snapshots.
- **SPA and StepM cross-checked** against the `arch` package (8.0.0, in a separate
  environment) on the same loss matrices. With arch's choices this implementation
  reproduces arch's moving-block results exactly (against v2: p 0.0020, the same 17 runs;
  against `gbm_own`: p 0.0080 / 0.0085 / 0.0085, the same 5 runs). The difference from the
  first report is two choices, now options of `fan_search`: arch ranks raw mean differences
  (its `studentize` flag changes nothing in 8.0.0) and recentres poor runs as Hansen's SPA
  does; the primary procedure studentises (Hansen's recommendation) with Romano and Wolf's
  recentring. Under every procedure: some run beats v2 (consistent p 0.002-0.007) and the
  nominee does individually; some run reading VXN beats `gbm_own` (p 0.0045-0.011). The
  nominee's own edge over `gbm_own` depends on the procedure - named with raw means, not
  when studentised (its session differences are the noisiest) - and stays borderline. The
  `search` report shows the three procedures side by side.
- **Average width and variance measured** (the learned fan's section): mean squared
  multiplier 1 fixes neither; the nominee's mean width is within +-0.6 % of v2's to 120
  minutes, its mean variance 6-8 % higher.
- **Freeze and holdout built as two commands.** `scripts/fan.py freeze` trains the chosen
  candidate once on every development session (243, 513,451 rows), writes its `fan_model`
  definition - features and hash, the complete fitted state (imputation, standardisation,
  coefficients per horizon: predictions come from it alone), the baseline, the issue rule,
  "no refit; a refit is a new version", the manifest's acceptance rule, the source and data
  snapshots - checks that the definition alone reproduces the fitted model (difference 0)
  and that it would open the holdout, and registers it only without `--dry-run` (a dirty
  tree only with `--allow-dirty`, its source archived). `scripts/fan.py holdout` scores the
  registered model once, from its definition, never refitted, stores the result in the
  journal and refuses a second run; `--rehearse` runs the identical pipeline on the last 60
  development sessions with a dry run's definition and stores nothing.
- **Dry run of `lin_pois_ivx`** (definition `e89160d219fab522`): reproduction exact; the
  15-minute coefficients' largest are VXN's level (+0.34) and NQ's 5-day realised variance
  (-0.34) - the implied-against-realised comparison, learned linearly - then the after-close
  phase, NQ's 60- and 15-minute realised variance and the pre-open phase. **Rehearsal**: the
  whole holdout path ran in 34 s (in sample, so its -0.58 % is no evidence); the frames it
  computes for a session range equal the cached development frames exactly. Nothing is
  registered and the holdout is sealed.

**The freeze (each step the user's decision).** The candidate is `lin_pois_ivx`, unchanged
(the fourth review: E's edge against `gbm_own` is not a comparison with the nominee, whose
direct paired comparison with E shows no difference). Remaining, in order: commit the code;
`freeze --candidate lin_pois_ivx` (registers it - the holdout opens for this model alone);
`holdout` (the one evaluation: pass when the whole 95 % interval of the model minus v2, NQ at
15 minutes over the 60 holdout sessions, lies below zero; inconclusive leaves v2 the
baseline and calls for more untouched sessions, never another candidate on the same
holdout); then the forward record (chunk 8: issuance time, the live inputs as read and
when, the model version, the issued quantiles - append only, scored once each horizon has
passed). A holdout result against v2 does not by itself show that reading other markets
adds value: that would need its own matched comparison. Deferred to a later version:
realised variance from 5-minute returns and any new features.

**How much to trust it.** `iv_rv` was picked from 11 prototypes by a screen on the checks'
own sessions, so the checks overstate it - the holdout is the honest test. In its favour:
it rests on a well-known relation (implied volatility above realised foretells higher
realised volatility), it was the one prototype with that prior, it gains in all three
checks separately, and on the 153 sessions before the checks - never used to choose it -
its rank correlation with |z| is +0.059, as strong as the best existing features.

**Not tried yet:** the open (the pre-open's activity relates *negatively* to the open's
surprise, -0.12 to -0.15 at 09:28 over 243 sessions - v2 already widens after a busy
pre-open, and the open does not follow; it needs its own rows, e.g. denser origins around
09:30, or a session-level model); implied-volatility term structure (VIX futures, VVIX -
not collected); option-implied moves around scheduled releases (not available).

## The sealed holdout

Until a model frozen against the experiment opens it, no candidate (`fan_rw_v2`, a model)
is scored or drawn on the holdout's sessions. `scripts/fan.py score` refuses a range that
reaches into it. A model opens it by being registered as kind `fan_model`, naming the
experiment and its definition hash and listing its training sessions - development only.
One model per experiment version is scored on the holdout; a second evaluation needs a
new version and is reported as having seen the first result.

Exempt: the Session Explorer keeps drawing `fan_rw_v1` with its recent accuracy - an
operational display of the registered benchmark; nothing is developed from it.

**Seen before registration:** `fan_rw_v1` was scored over every session to 2026-10-05,
the holdout's included, on 2026-10-06 (`docs/reports/fan_rw_v1_*`). Nothing was fitted to
those scores, but the lessons drawn - too narrow around releases, too thin in the tails,
NQ's pre-open hour slightly narrow - came partly from the holdout's period. The manifest
records this as a historical test.

## Development chunks

| # | Chunk | Status |
|---|---|---|
| 0 | Fix the experiment in advance: the manifest, the `fan_experiment` and `fan_model` kinds (migration 0019), the sealed holdout | done: v2 registered 2026-10-06 |
| 1 | Data: audit each instrument's hours and gaps; the point-in-time panel (every instrument on the target's minute grid with the age of its last value; no-look-ahead tests) | done: every group present through the checks |
| 2 | `fan_rw_v2` by rules: a multiplier per release type, a fat-tailed shape, the open | done: v2 passed the gate and is the baseline; the open needed no rule |
| 3 | Test harness: the three checks, training-row sampling (every 5 minutes), paired intervals per horizon, phase and slice | done: `scripts/fan.py checks`; the identity differs by exactly zero, `phase_scale` gains nothing (above) |
| 4 | Features, each tagged with its instrument and group | done: 119 instrument features + 4 base columns, `scripts/fan.py features` (above) |
| 5 | Model: gradient boosting per horizon on how much wider or narrower than the baseline the fan should be; own instrument only, then all | done: `gbm_own` -0.24 % at 15 min; `gbm_all` no better; with VXN's implied against NQ's realised variance -0.35 to -0.40 % whatever the model; nominee `lin_pois_ivx`; replay passed; the search reviewed (SPA p 0.003 against v2) - above |
| 6 | Contribution of each instrument (above) | next |
| 7 | Freeze one model (`fan_model`) and score it once on the holdout | ready: `freeze` and `holdout` built; `lin_pois_ivx` dry run and rehearsal passed; registering and scoring await the user |
| 8 | Forward record: benchmark and model fans logged every 15 minutes, scored once final | |
| 9 | Dashboard: the model draws the horizons it passed | |

## Commands

```bash
python scripts/fan.py experiment-register --dry-run   # resolve the manifest from the store, register nothing
python scripts/fan.py experiment-register             # register it (fixed in advance)
python scripts/fan.py experiment-show                 # the registered manifest; sealed or open
python scripts/fan.py panel-audit                     # the panel of the development sessions, audited
python scripts/fan.py baseline-gate                   # fan_rw_v2 against fan_rw_v1 on the checks (decided once)
python scripts/fan.py checks --candidate phase_scale  # a candidate against the baseline on the three checks
python scripts/fan.py checks --candidate identity --target ES --no-report   # the self-check, on a secondary target
python scripts/fan.py features                        # the features on development, screened before the checks
python scripts/fan.py checks --candidate gbm_own_ivx  # the learned fan: NQ's own + VXN implied vs realised (see MODELS)
python scripts/fan.py compare gbm_own_ivx lin_pois_ivx   # two stored runs paired: B minus A
python scripts/fan.py replay                          # live-style replay of the leading candidates
python scripts/fan.py search                          # SPA and StepM over every stored run
python scripts/fan.py freeze --candidate lin_pois_ivx --dry-run   # the frozen definition, registered without --dry-run
python scripts/fan.py holdout --rehearse --definition data/fan_cache/freeze/fan_intermarket_v2_lin_pois_ivx_frozen.json
python scripts/fan.py holdout                         # the frozen model on the holdout - once
```

`experiment-register` takes `--holdout-end`, `--holdout-sessions`, `--warm-up`,
`--min-development` and `--min-coverage`; a new version after a backfill is registered
with `--name` and `--supersedes` (the version it replaces).
