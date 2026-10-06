# contracts/fan.py
"""
The benchmark price fan (docs/fan.md): a forecast that can be issued from any
minute of any session - not only at the 09:29 cutoff - of the distribution of
the price at every later minute of the same trading day. No machine learning:
a zero-drift random walk in log price whose per-minute variance is

    l(d) x S(s) x E(s)

  S   the usual intraday volatility pattern: the mean squared 1-minute log return
      of each minute of the trading day over the instrument's earlier complete
      sessions, scheduled-release windows left out, smoothed within the session's
      phases
  E   event bumps: a multiplier for the minutes after each scheduled release of
      the target day, estimated per release group and minute bucket from the
      earlier releases of the same group (a fixed fallback when there are too few)
  l   the current volatility level against S x E: the last hour's (shrunk towards
      the last five sessions'), reverting to the five-session level as the
      distance d from the origin grows

The price at origin t + h is then log-normal around the origin price with
variance sum over the h minutes ahead. Everything a forecast for session D at
minute t uses is known at t: earlier sessions, D's own bars up to t and D's
scheduled releases (published in advance).

FAN is registered as a ``forecast_algorithm`` in journal.definition_versions;
changing anything in it means a new version name.
"""

from __future__ import annotations

from datetime import time
from typing import Any, Dict, Tuple

from contracts import nq_prompt_v2 as defs

FAN_VERSION = "fan_rw_v1"

# Quantiles of the issued distribution, in order: the fan's gradient stops. 0.1587 / 0.8413 are -1 / +1 sigma.
QUANTILES: Tuple[float, ...] = (0.01, 0.025, 0.05, 0.10, 0.1587, 0.25, 0.5, 0.75, 0.8413, 0.90, 0.95, 0.975, 0.99)
# Horizons (minutes after the origin's bar) the scoring reports; the fan itself covers every minute.
SCORE_HORIZONS: Tuple[int, ...] = (1, 5, 15, 30, 60, 120, 240)
# Central intervals whose coverage the scoring reports.
COVERAGE_LEVELS: Tuple[float, ...] = (0.50, 0.80, 0.90, 0.98)
# Paired differences between variants: a moving-block bootstrap over the sessions' mean CRPS, in session order
# (origins within one session are far from independent).
BOOTSTRAP = {"block_sessions": 5, "resamples": 2000, "seed": 7, "interval": 0.95}

SEASONAL_SESSIONS = 40          # earlier complete full sessions behind S
SEASONAL_MIN_SESSIONS = 10      # a minute needs this many valid returns, otherwise it is filled by smoothing
SEASONAL_HALF_WINDOW = 7        # S is smoothed over +-7 minutes, never across a phase boundary
OUTLIER_CAP = 50                # a squared return above 50x its neighbourhood's median is capped (bad prints)
EVENT_SESSIONS = 250            # earlier sessions searched for releases behind E
EVENT_PRIOR_RELEASES = 3        # each multiplier is shrunk towards its fallback with three releases' weight
LEVEL_SESSIONS = 5              # the long level: realised against expected variance over the last five sessions
LEVEL_WINDOW = 60               # the short level: the last 60 minutes up to the origin
LEVEL_PRIOR_MINUTES = 30        # ... shrunk towards the long level with 30 minutes' weight
LEVEL_REVERSION_MINUTES = 90    # the short level's excess decays as exp(-d / 90) with distance d
LEVEL_BOUNDS = (0.25, 6.0)      # both levels are clipped to this range

# Release groups: minute buckets after the release minute (offsets [start, end)) and their fallback
# multipliers (a priori: the whole estimate without earlier releases, the prior the estimate shrinks towards).
# FOMC's 30-45 bucket is the press conference (14:30 ET).
EVENT_GROUPS: Dict[str, Dict[str, Any]] = {
    "fomc": {"buckets": ((0, 1), (1, 5), (5, 15), (15, 30), (30, 45), (45, 90)),
             "fallback": (6.0, 4.0, 3.0, 2.0, 3.0, 1.8)},
    "high": {"buckets": ((0, 1), (1, 5), (5, 15), (15, 30)), "fallback": (6.0, 3.0, 1.8, 1.3)},
    "moderate": {"buckets": ((0, 1), (1, 5), (5, 15), (15, 30)), "fallback": (3.0, 1.8, 1.3, 1.1)},
}
FOMC_DECISION = "FOMC rate decision"


def event_group(name: str, tier: str):
    """The release group of an economic_events row, or None (a low-tier release has no window)."""
    if name.startswith(FOMC_DECISION):
        return "fomc"
    return tier if tier in ("high", "moderate") else None


# Where each instrument type's trading day ends (exclusive, ET on the session date), by schedule. Futures halt
# 17:00-18:00 and at 13:15 on an early close; stocks trade extended hours to 20:00 - but the store files every bar
# from 18:00 ET under the next session, so a stock's day in this grid ends at 18:00 (17:00 after an early close).
DAY_END_ET = {
    "FUT": {"full": time(17, 0), "early_close": time(13, 15)},
    "STK": {"full": time(18, 0), "early_close": time(17, 0)},
}
# Phase boundaries (ET) S is never smoothed across: the stock premarket open, the regular open and close.
PHASE_BOUNDARIES_ET = (time(4, 0), time(9, 30), time(16, 0))

FAN: Dict[str, Any] = {
    "kind": "benchmark price fan (no machine learning)",
    "origin": "any minute of a trading day (18:00 ET the prior evening to the day's end): the close of the last "
              "completed 1-minute bar of the instrument's active contract",
    "target": "the last traded price (close of the last bar at or before) at every later minute of the same "
              "trading day; a minute without a trade keeps the price",
    "model": "zero-drift random walk in log price; per-minute variance l(d) x S(s) x E(s); the price at t + h "
             "log-normal around the origin price with variance sum_{d=1..h} l(d) S(t+d) E(t+d)",
    "seasonal": {"sessions": SEASONAL_SESSIONS, "min_sessions": SEASONAL_MIN_SESSIONS,
                 "estimator": "mean over sessions of the squared 1-minute log return per minute of the trading "
                              "day; returns on a forward-filled last-price grid; the first minute of the day and "
                              "every minute inside a release window of a moderate / high / FOMC release excluded; "
                              f"a squared return above {OUTLIER_CAP}x the median of its +-{SEASONAL_HALF_WINDOW}-"
                              "minute neighbourhood capped there",
                 "smoothing": f"centred mean over +-{SEASONAL_HALF_WINDOW} minutes within phases split at "
                              + ", ".join(t.strftime("%H:%M") for t in PHASE_BOUNDARIES_ET) + " ET",
                 "sessions_used": "complete sessions of full schedule strictly before the target session"},
    "events": {"groups": {g: {"buckets": [list(b) for b in v["buckets"]], "fallback": list(v["fallback"])}
                          for g, v in EVENT_GROUPS.items()},
               "grouping": f"'{FOMC_DECISION}' -> fomc; otherwise the tier (high | moderate); low tier ignored",
               "estimator": "per group and bucket over every earlier release within "
                            f"{EVENT_SESSIONS} sessions: (sum of squared returns + {EVENT_PRIOR_RELEASES} x fallback "
                            "x mean S per release) / (sum of S + "
                            f"{EVENT_PRIOR_RELEASES} x mean S per release) over the bucket's minutes, floored at 1; "
                            "the fallback without earlier releases",
               "overlap": "the largest multiplier of the releases covering a minute",
               "unknown": "a session outside every economic_event_coverage row has no known releases: E = 1, "
                          "flagged"},
    "level": {"long": f"realised / expected (S x E) variance over the last {LEVEL_SESSIONS} sessions",
              "short": f"realised / expected over the last {LEVEL_WINDOW} minutes up to the origin, shrunk "
                       f"towards the long level with {LEVEL_PRIOR_MINUTES} minutes of expected variance",
              "reversion": f"l(d) = long + (short - long) x exp(-d / {LEVEL_REVERSION_MINUTES})",
              "bounds": list(LEVEL_BOUNDS)},
    "day_end_et": {k: {s: t.strftime("%H:%M") for s, t in v.items()} for k, v in DAY_END_ET.items()},
    "quantiles": list(QUANTILES),
    "score": {"horizons": list(SCORE_HORIZONS), "coverage_levels": list(COVERAGE_LEVELS),
              "metrics": "CRPS of the log price in basis points (closed form for the normal), central-interval "
                         "coverage, PIT deciles, z RMS (1 when calibrated)",
              "references": "flat (one variance for every minute), seasonal (S), seasonal + events (S x E), "
                            "full (l x S x E)",
              "origins": "every minute of every complete full session with a last price, for each horizon that "
                         "ends inside the trading day; a variance of zero (a closed market) is not scored",
              "uncertainty": dict(BOOTSTRAP)},
}


def fan_record() -> Dict[str, Any]:
    return defs._record(FAN_VERSION, "forecast_algorithm", FAN)


# --------------------------------------------------------------------------
# fan_rw_v2 (forecaster/fan_v2.py): v1 with three changes, chosen on the intermarket experiment's development
# sessions before its checks (docs/fan.md, "Version 2"). Everything not named here is v1's.
# --------------------------------------------------------------------------

FAN_V2_VERSION = "fan_rw_v2"
EARNINGS_SOURCE = "sec_earnings"           # economic_events rows of 8-K Item 2.02 earnings releases
EARNINGS_GROUP = "earnings"
EARNINGS_ANCHOR_ET = time(16, 0)          # an earnings release is placed at the regular close of its day ...
EARNINGS_AFTER_ET = time(15, 0)           # ... when filed from 15:00 ET to the day's end; any other is not placed
EVENT_GROUPS_V2: Dict[str, Dict[str, Any]] = {
    **EVENT_GROUPS,
    EARNINGS_GROUP: {"buckets": ((0, 5), (5, 15), (15, 30), (30, 60)), "fallback": (3.0, 2.0, 1.5, 1.3)},
}
SHAPE_SESSIONS = 120                      # the shape: standardised errors of the last 120 complete full sessions ...
SHAPE_MIN_SESSIONS = 20                   # ... at least 20 of them, else the normal shape
SHAPE_LEVELS = 200                        # quantile levels tau_k = (k - 1/2) / 200
SHAPE_HORIZONS: Tuple[int, ...] = (1, 2, 3, 5, 8, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 360, 480, 720, 1380)

FAN_V2: Dict[str, Any] = {
    "kind": "benchmark price fan (no machine learning), version 2",
    "base": FAN_VERSION,
    "unchanged": "origin, target, the intraday pattern S (its estimator, smoothing and outlier cap), the level l, "
                 "the reversion, the bounds, the zero drift and the day's end are fan_rw_v1's",
    "changes": {
        "releases_by_name": "the event bump E is estimated per release name (CPI, payrolls, ISM, each company's "
                            "earnings ...) and minute bucket from its own earlier releases within "
                            f"{EVENT_SESSIONS} sessions, shrunk towards its group's multiplier - itself v1's "
                            f"estimator - with {EVENT_PRIOR_RELEASES} releases' weight, floored at 1; a name "
                            "without earlier releases takes its group's multiplier",
        "earnings_at_the_close": f"economic_events rows of source {EARNINGS_SOURCE} (8-K Item 2.02 at its EDGAR "
                                 "acceptance time, which follows the market's reaction to the press release) form "
                                 f"their own group '{EARNINGS_GROUP}', placed at "
                                 f"{EARNINGS_ANCHOR_ET:%H:%M} ET of their trading day when filed from "
                                 f"{EARNINGS_AFTER_ET:%H:%M} ET to the day's end (a release's day and side of the "
                                 "close are known in advance, its minute is not); any other is not placed",
        "fat_tailed_shape": f"the issued distribution of the log price at t + h is sigma_h x Q_h, where Q_h are "
                            f"standardised quantiles at {SHAPE_LEVELS} levels tau_k = (k - 1/2)/{SHAPE_LEVELS}: the "
                            "empirical quantiles of the errors y / sigma at horizon h of the last "
                            f"{SHAPE_SESSIONS} complete full sessions before the target (each scored on its own "
                            "walk-forward fit), made symmetric (Q(tau) = (q(tau) - q(1 - tau)) / 2, so no drift and "
                            f"no skew); estimated at horizons {list(SHAPE_HORIZONS)} and interpolated in log "
                            f"minutes; the normal's quantiles with fewer than {SHAPE_MIN_SESSIONS} sessions",
    },
    "events": {"groups": {g: {"buckets": [list(b) for b in v["buckets"]], "fallback": list(v["fallback"])}
                          for g, v in EVENT_GROUPS_V2.items()},
               "seasonal_exclusion": "S leaves out the minutes of every v2 release window (earnings: 16:00-17:00)"},
    "open": "no change: on the development sessions before the checks the 09:30-10:30 minutes were forecast at "
            "0.9-1.0 of their realised variance; the pre-open origins' shortfall came from the 08:30 releases",
}


def fan_v2_record() -> Dict[str, Any]:
    return defs._record(FAN_V2_VERSION, "forecast_algorithm", FAN_V2)
