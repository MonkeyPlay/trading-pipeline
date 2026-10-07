# The direction experiment (`fan_direction_v1`)

Asked 2026-10-07, after an outside review of the fan: can a learned directional component
move the fan's centre, using the EMA trends as inputs rather than projecting them? One
bounded experiment, at the two horizons the learned fan draws (NQ, 5 and 15 minutes);
[forecaster/fan_direction.py](../forecaster/fan_direction.py), `scripts/fan.py direction`.

**Result (development, 2026-10-07): no directional improvement found.** Projecting the EMA trend is
clearly worse than no drift, and the learned shift shrinks itself to almost nothing and
still does not beat zero. The fan stays centred on the origin; nothing is drawn.
[Report](reports/fan_direction_fan_intermarket_v2_NQ.md).

## The forecast

```
forecast price quantile = P_t exp(mu_h(x_t) + s_h(x_t) z_q)
```

`s_h` is the existing scale (`lin_pois_ivx`'s multiplier of `fan_rw_v2`'s sigma), `z_q` v2's
fat-tailed shape; `mu_h = 0` is the fan as it is. Three arms share that scale exactly, so
every difference is the centre alone:

| Arm | Centre |
|---|---|
| `zero` | no drift: today's fan |
| `ema_damped` | 0.5 x h x the EMA 14's slope per minute over the last 5 minutes - the trend carried forward at half strength; nothing fitted, the damping fixed in advance |
| `ridge` | per horizon, a ridge regression of the realised move in the scale's sigmas (winsorised at +-4) on the inputs below; no intercept on standardised inputs, so average conditions give no shift; the penalty chosen on the last 20 % of the training sessions (chronological), and no shift at all unless some penalty beats predicting zero there; the prediction capped at +-1 sigma |

**Inputs** (point in time, 1-minute bars, NQ's own; tested to read nothing after the origin):
the EMA 14 and EMA 100 slopes (over 5 and 15 minutes), the price's distance from each, EMA 14
minus EMA 100, the 1-, 5- and 15-minute returns, the distance from the session VWAP (from
18:00 ET, on closes) - each divided by the trailing 60-minute volatility of 1-minute returns
and clipped to +-10 - with NQ's relative volume (`vol60`), its realised variance over usual
(`rv15`, `rv60`) and the origin's phase. The EMAs are on 1-minute closes; the chart's EMA lines
are drawn at the chart's timeframe, so on a 5-minute chart its EMA 100 spans 500 minutes.

## Evaluation

Rolling origin: the intermarket experiment's three checks (2026-03-03 to 07-10), then its
holdout (07-13 to 10-05) as two blocks of 30 - 150 sessions, every origin scored. Each block's
scale and arms are trained on the sessions before it only (origins every 5 minutes), so every
training label has matured. Per arm against zero, per session, with the experiment's 5-session
moving-block bootstrap:

- **CRPS** of the log price, as the experiment scores it;
- **coverage** of the central 50 / 80 / 90 % bands;
- **Brier score** of P(up) - moves of exactly zero left out - and its reliability by decile;
- **RPS** of below / within / above a neutral band of +-0.25 scale sigmas around the origin.

All of it is development. The holdout blocks were examined by the scale experiment before this
one existed: that they agree with the checks is consistency, not confirmation.

## Results (pooled, 150 sessions)

| Horizon | Arm | CRPS vs zero | Brier vs zero | Hit rate | 90 % coverage | Mean shift |
|---|---|---|---|---|---|---|
| 5 min | ema_damped | +4.17 %, worse | +5.66 %, worse | 49.8 % | 88.3 % (zero 89.4 %) | 0.20 sigma |
| 5 min | ridge | +0.01 %, inconclusive | +0.01 %, inconclusive | 50.2 % | 89.4 % | 0.015 sigma |
| 15 min | ema_damped | +12.01 %, worse | +13.83 %, worse | 49.8 % | 86.0 % (zero 89.5 %) | 0.35 sigma |
| 15 min | ridge | +0.02 %, worse | +0.02 %, inconclusive | 50.3 % | 89.5 % | 0.008 sigma |

- **The trend does not continue.** `ema_damped` is worse in every one of the five blocks, at
  both horizons, on every measure. Its up-probabilities range from 0.05 to 0.95, and every
  decile came true 46-53 % of the time, slightly *against* the trend at the extremes. Moving
  the centre also costs coverage, as a shifted band misses on the side it leaves.
- **The learned shift finds almost nothing.** The ridge's out-of-sample correlation with the
  realised move is +-0.01 to 0.02 and changes sign between blocks. What it learns is consistent
  but tiny: a slight pull back after a 5-minute move (`ret5` negative in every block), and
  towards the price's side of the VWAP. That shifts the centre by about 1 % of the fan's
  sigma, well under one NQ point at 5 minutes. At 15 minutes its whole interval lies above
  zero, a small but real loss.
- **The zero-shift guard is weak.** In every block some penalty beat zero on the training
  sessions' own validation by 0.003-0.07 % of the error, and on pure noise in the tests it does
  too, so the ridge was always used. That leaves the conclusion unchanged: the penalty it chose
  kept the shift negligible.

This agrees with the September walk-forward (in the git history: README "How a forecast is built"
at f00a0c1): the size of NQ's moves is predictable, while no reliable directional improvement has
been established, now including EMA trend, momentum and VWAP position at 5 and 15 minutes from any
minute of the day. That is not proof that direction is unpredictable: the activation rule's power
against small effects is limited ([docs/fan_cond_ema.md](fan_cond_ema.md), "Power").

## What follows

- **Nothing is drawn.** The review's chart (shifted brackets, a dashed "forecast median" from
  the origin to the 5- and 15-minute centres, P(above / within / below)) was not built: with no
  skill the learned centre would sit a fraction of a point from the origin, and the EMA
  projection would draw a trend the data contradict. The neutral fan stays as it is.
- **No freeze, no forward record.** A confirmation on fresh sessions (after 2026-10-05) would
  first need a model that beats zero in development; neither arm does.
- **Not tried** (each would be a new version reported as having seen this one): a small
  gradient-boosted challenger, quantile regression for asymmetric shapes, other markets' moves
  as leads (the intermarket research found they move with NQ within the minute), and the order
  book or trade flow - information this store does not hold.

```bash
python scripts/fan.py direction              # the rolling-origin evaluation; report to docs/reports/
```

The run (`data/fan_cache/direction/`, not in git) holds every block's fitted ridge and
per-session scores; the holdout's baseline frames are cached beside it.
