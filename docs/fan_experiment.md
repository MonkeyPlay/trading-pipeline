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
  before any model is frozen.
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
| 2 | `fan_rw_v2` by rules: a multiplier per release type, a fat-tailed shape, the open | next |
| 3 | Test harness: the three checks, training-row sampling (every 5 minutes), paired intervals per horizon, phase and slice | |
| 4 | Features, each tagged with its instrument and group | |
| 5 | Model: gradient boosting per horizon on how much wider or narrower than the baseline the fan should be; own instrument only, then all | |
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
```

`experiment-register` takes `--holdout-end`, `--holdout-sessions`, `--warm-up`,
`--min-development` and `--min-coverage`; a new version after a backfill is registered
with `--name` and `--supersedes` (the version it replaces).
