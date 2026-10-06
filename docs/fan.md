# The benchmark price fan (`fan_rw_v1`)

A forecast arm that is not tied to the 09:29 cutoff: from **any minute** of a trading
day - overnight, pre-open, mid-session - it issues the distribution of the price at
**every later minute** of the same day. It is the benchmark every later fan model
(gradient-boosted quantiles, a sequence network) has to beat, horizon by horizon, and
what the Session Explorer draws on the session in progress (below: the chart).

No machine learning: a zero-drift random walk in log price whose per-minute variance
is built from three measured parts. The P1 arms A-D (docs/nq_prompt_v2.md) stay as
they are; this arm answers a different question - how far the price may travel from
here, and when - and is scored separately.

| Piece | Where |
|---|---|
| Registered definition (`forecast_algorithm` `fan_rw_v1`): every parameter, hashed | [contracts/fan.py](../contracts/fan.py) |
| The model: grid, estimators, variances, the fan from an origin, CRPS | [forecaster/fan_benchmark.py](../forecaster/fan_benchmark.py) |
| Reading the days from the store (bars, completeness, releases, coverage) | [forecaster/fan_data.py](../forecaster/fan_data.py) |
| Scoring at every origin, the report | [forecaster/fan_scoring.py](../forecaster/fan_scoring.py) |
| CLI | [scripts/fan.py](../scripts/fan.py) |
| Tests | [tests/test_fan.py](../tests/test_fan.py) |

## The model

A trading day is a grid of 1440 one-minute slots from 18:00 ET the prior evening -
the day the store files bars under. The last price is the active contract's 1-minute
closes forward-filled: a minute without a trade keeps the price. Futures end at 17:00
(13:15 on an early close); a stock's day ends at 18:00 in this grid (17:00 on an early
close), its 18:00-20:00 after-hours bars belonging to the next session as stored.

From origin t (the close of the last completed bar), the log price at t + h is normal
around the origin's log price with variance

    sum over d = 1..h of  l(d) x S(t + d) x E(t + d)

- **S, the intraday pattern:** per minute of the day, the mean squared 1-minute log
  return over the last 40 complete full sessions before the target session. Release
  windows (below) are left out, so S is the pattern of a day without releases; a
  squared return above 50x its +-7-minute neighbourhood's median is capped (a bad
  print, not a market); a minute needs 10 valid sessions. S is smoothed over +-7
  minutes but never across 04:00, 09:30 or 16:00, and a minute nothing ever traded or
  moved in (the futures halt, a stock's closed night) stays exactly 0 - the fan does
  not widen while the market is shut.
- **E, the event bumps:** the economic calendar's scheduled releases of the target
  day (known in advance), grouped as FOMC decision, high tier or moderate tier (low
  tier ignored). Each group has minute buckets after the release minute (FOMC to 90
  minutes, with a bucket for the 14:30 press conference); each bucket's multiplier is
  the squared returns over S in its minutes across every earlier release within 250
  sessions, shrunk towards an a-priori fallback with three releases' weight (one
  extreme release cannot carry it), floored at 1. Overlapping releases take the
  largest multiplier. A session outside every `economic_event_coverage` row has no
  known releases: E = 1, flagged on the fan.
- **l, the level:** today is not an average day. The long level is realised over
  expected (S x E) variance across the last 5 sessions; the short level the same over
  the 60 minutes up to the origin, shrunk towards the long level with 30 minutes'
  weight. The short level's excess decays as exp(-d / 90) with the distance d ahead,
  so the near fan follows the last hour and the far fan the last week. Both are
  clipped to [0.25, 6].

The fan issues 13 quantiles per minute (1, 2.5, 5, 10, 15.87, 25, 50, 75, 84.13, 90,
95, 97.5, 99 %): the gradient stops of the chart, +-1 sigma among them. With zero
drift the median stays at the origin price; the arm makes no claim about direction.

Everything a forecast for session D at minute t reads is known at t: sessions before
D, D's own bars up to t (tested: changing D's later bars changes nothing), and D's
scheduled releases.

## Scoring at every origin

`score` walks a range of sessions forward. Each complete full session is fitted on the
sessions before it and scored at **every minute with a last price**, for each horizon
of 1, 5, 15, 30, 60, 120 and 240 minutes that ends inside the trading day (a zero
variance - a closed market - is not scored). Four variants are scored on the same
origins:

| Variant | Per-minute variance |
|---|---|
| flat | one variance for every trading minute (the sessions' mean) |
| seasonal | S |
| seasonal + events | S x E |
| full | l x S x E - the fan as issued |

Per horizon the report gives the CRPS of the log price in basis points (lower is
better), z RMS (1 when the spread is right; above 1 too narrow), the coverage of the
50 / 80 / 90 / 98 % central intervals, PIT deciles, the skill against flat, and each
part's contribution as paired session-mean differences with moving-block bootstrap
intervals (5-session blocks). Two breakdowns: by origin phase (overnight, pre-open,
opening hour, midday, afternoon, after the close) and for origins with a release
inside the horizon - where E has to earn its place.

The skill and calibration per horizon are what the chart's accuracy fade will be built
from, and what a learned fan must improve on.

## The chart

**Since 2026-10-06 the explorer draws `fan_rw_v2`** - the intermarket experiment's baseline,
once its holdout opened - with v2's own shape and its accuracy measured on v2
([forecaster/fan_live.py](../forecaster/fan_live.py)); on NQ the experiment's frozen learned
fan adds brackets at the horizons it passed on the holdout (5 and 15 minutes;
[docs/fan_experiment.md](fan_experiment.md), "The dashboard").

The Session Explorer draws the fan on the session in progress only, to the right of its
newest candle ([dashboard/components/fan.py](../dashboard/components/fan.py), the
`DensityFan` primitive in
[dashboard/components/lightweight_chart.js](../dashboard/components/lightweight_chart.js)):

- **Columns:** one per candle ahead at the chart's timeframe (at most 120, to the day's end),
  the distribution of the candle's close - v2's fan at its last minute, **as issued**: no
  display adjustment, so the learned fan's brackets and the fog compare directly (a
  multiplier of x1.10 is a bracket 10 % wider than v2 at its horizon). Blank candles extend
  the time axis into the future.
- **The price fade:** each column is a vertical gradient with a stop at each issued quantile,
  its opacity proportional to the normal density there (exp(-z^2 / 2)): the median fully
  opaque, the 1 / 99 % quantiles nearly clear.
- **The accuracy fade:** each column's opacity is also scaled by its confidence: v2's CRPS
  skill at that horizon against a flat random walk with normal errors (one volatility for
  every trading minute), divided by its skill one minute ahead, interpolated in log minutes,
  never below 0.25. Where the fan knows no more than that random walk it stays a faint band.
  It is measured walk-forward over the last 30 complete sessions before the day
  (`fan_live.v2_accuracy`): each session fitted on the sessions before it and **drawn with
  the shape it was issued with** - the errors of the 120 sessions before it, never its own -
  and scored as drawn, the manifest's quantile-form CRPS on that shape. The line under the
  chart gives the 90 % band's coverage, measured the same way.
- **Playback** shows the fan from any earlier candle of the session - a **recomputed
  historical preview**, and the line under the chart says so: computed from the bars stored
  now, it reads only bars dated before the origin, but it cannot show what a revised or
  late-arriving bar would have changed. Where the forward record issued the learned fan from
  that origin (every 15 minutes, and 09:29 ET), the brackets are the **recorded forecast**,
  read from the journal with when it was recorded: only that says what the model showed at
  the time ([docs/fan_experiment.md](fan_experiment.md), "The dashboard").

**Measured on NQ** (the 30 sessions to 2026-10-05): v2's skill 8.45 % at 1 minute, 7.29 % at
15 and 2.65 % at 240; its 90 % band held 89.5 % at 1 minute, 89.2 % at 15 and 87.6 % at 240,
its 50 % band 45.9-50.4 %.

**Until 2026-10-06 (the seventh review)** the fade was measured with today's shape for every
measured session - partly in-sample, since those sessions had made the shape - and with
normal distributions where the fan draws a fat-tailed one; and a horizon whose band had held
less than 90 % was drawn widened to hold it, while the learned brackets were not, so their
distance from the fog mixed the model's multiplier with a display-only adjustment. Measured
that way the same sessions gave 89.8 % at 1 minute and 88.3 % at 240 - in-sample coverage
0.3-0.7 points too high - and skill 8.11 % and 2.59 %.

The model and accuracy take a few seconds to load (the 250 sessions behind the release
multipliers; minutes the first time, while v2's per-session errors are computed and cached);
the explorer does that off its event loop, once per instrument and day.

## Version 2 (`fan_rw_v2`)

Registered 2026-10-06 (hash `cf1c3910d23b9b0f`) for the intermarket fan experiment
([docs/fan_experiment.md](fan_experiment.md)): v1 with three changes, chosen on the
experiment's development sessions before its checks (2025-07-21 to 2026-03-02) -
[contracts/fan.py](../contracts/fan.py) `FAN_V2`, [forecaster/fan_v2.py](../forecaster/fan_v2.py).

- **Releases by name.** At the release minute CPI moved about 140x its usual minute
  variance, payrolls about 50x, PPI about 28x, ISM Manufacturing about 2x: v1's one "high"
  multiplier fitted none of them. v2 estimates each release's own multiplier per minute
  bucket from its earlier releases, shrunk towards its group's with three releases' weight.
- **Earnings at the close.** The store dates an earnings release by its 8-K filing, which
  follows the market's reaction and is not known in advance. v2 gives earnings their own
  group, placed at 16:00 ET of their trading day with windows to the 17:00 halt; a filing
  before 15:00 or after the day's end is not placed.
- **A fat-tailed shape.** The issued distribution at h minutes is sigma_h x Q_h: Q_h the
  empirical quantiles (200 levels) of the standardised errors of the last 120 sessions,
  made symmetric so the fan keeps no drift; the normal's with fewer than 20 sessions.
- **The open: no change.** The 09:30-10:30 minutes were forecast at 0.9-1.0 of their
  realised variance; the pre-open origins' shortfall came from the 08:30 releases, which the
  first change addresses.

**The gate** (the manifest's rule, decided once on the checks' 90 sessions, 2026-03-03 to
2026-07-10, result `72b66bb7`): at NQ 15 minutes v2 lowered CRPS by 0.32 %, interval
[-0.0385, -0.0075] bps - **fan_rw_v2 is the experiment's baseline**. v2 was also better at
1, 5, 20, 30 and 120 minutes; at 60 and 240 minutes and in the pre-open slice (one origin
a session) the intervals include zero. Report:
[docs/reports/fan_rw_v2_gate_fan_intermarket_v2.md](reports/fan_rw_v2_gate_fan_intermarket_v2.md).

The Session Explorer kept drawing `fan_rw_v1` until the experiment's holdout opened (2026-10-06;
since then it draws v2, "The chart" above) (its
manifest): no candidate is shown on the holdout's sessions before then.

## Running it

```bash
python scripts/fan.py register                                      # the definition (also done by every command)
python scripts/fan.py now --symbol NQ                               # from the latest closed bar
python scripts/fan.py now --symbol NQ --as-of "2026-10-02 10:15"    # replayed from a past minute (ET)
python scripts/fan.py now --symbol ES --minutes 120 --json logs/fan_es.json
python scripts/fan.py score --symbol NQ --start 2025-09-02 --end 2026-07-10
python scripts/fan.py score --symbol NQ --symbol ES --symbol RTY --start 2025-09-02 --end 2026-07-10 --no-report
```

`score` refuses a range that reaches into the sealed holdout of a registered fan experiment
(2026-07-13 to 2026-10-05 for `fan_intermarket_v2`, [docs/fan_experiment.md](fan_experiment.md))
until a model frozen against it opens the holdout.

`now` prints the origin, the levels, the releases ahead and the 5 / 25 / 50 / 75 / 95 %
prices at 1, 5, 15, 30, 60, 120, 240 minutes and the day's end; `--json` writes the
whole fan (every minute's closing instant, sigma and 13 quantiles, the releases ahead
with their multipliers) - the chart's input. `score` writes
`docs/reports/fan_rw_v1_<symbol>_<start>_<end>.md` and `.csv`. Futures and stocks
(NQ, ES, RTY, QQQ, SPY, IWM, SMH) are supported; cash indices are not forecast.

Nothing is stored but the definition: a fan is recomputed from the bars on demand and
the score is a replay. The history it needs - 40 complete sessions for S, up to 250
for the release multipliers - is read from the store; a session with fewer than 10
earlier complete sessions is skipped as `insufficient_history`.

## Known limits (v1)

- **Normal in log price.** One-minute returns have fat tails, so at the shortest
  horizons the issued distribution is a little too wide in the middle and a little too
  narrow in the tails; the PIT deciles show it. Longer horizons are close to normal.
- **The day's end is the horizon.** A fan stops at 17:00 ET for futures (the halt);
  the next session is not forecast.
- **The calendar's coverage.** The economic calendar starts in 2025; before it, and on
  any session outside its coverage, there are no event bumps.
- **Early-close and holiday sessions** are not scored (a fan is still issued on an
  early close, ending at its close).
- **What the chart draws is not stored.** On NQ the forward record logs v2 and the learned fan
  every 15 minutes and at 09:29 ET while they are issued, and scores them once the session is
  final ([docs/fan_experiment.md](fan_experiment.md), chunk 8); every other fan drawn is
  computed on demand.
