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
point-in-time panel, what a candidate reads at an origin - 119 instrument features over
the 12 instruments plus 4 base columns, each tagged with its instrument and group (the
target's own instrument is `own`; the base columns are in every model):

| Family | What, at origin t | Instruments |
|---|---|---|
| `rv5`, `rv15`, `rv60`, `rv240` | realised variance of the last w minutes over the usual for those minutes (log ratio): moving more than usual for the time of day | all |
| `ret15`, `ret60` | the return over the last w minutes, in the usual sigma | all |
| `chg` | the change since the previous session's regular close (13:00 after an early close), in the usual sigma | all |
| `vol60` | volume over the last 60 minutes over the usual (log ratio) | futures and ETFs (an index has no volume) |
| `age` | log(1 + minutes since its last bar) | all |
| `day_rv` | the previous session's realised variance over the usual | all |
| `level` | the log of its value | VIX, VXN (implied volatilities) |
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
| 5 | Model: gradient boosting per horizon on how much wider or narrower than the baseline the fan should be; own instrument only, then all | next |
| 6 | Contribution of each instrument (above) | |
| 7 | Freeze one model (`fan_model`) and score it once on the holdout | |
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
```

`experiment-register` takes `--holdout-end`, `--holdout-sessions`, `--warm-up`,
`--min-development` and `--min-coverage`; a new version after a backfill is registered
with `--name` and `--supersedes` (the version it replaces).
