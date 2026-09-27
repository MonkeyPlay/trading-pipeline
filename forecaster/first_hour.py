# forecaster/first_hour.py
"""
First-hour forecast (09:30-10:30) from pre-open data only.

At 09:29 the only information is the pre-open state, so the forecast is built
from the sessions whose pre-open state was closest (forecaster/analogue.py
``rank_preopen``: for NQ the point-in-time snapshots on the pre-open volatility
inputs and the gap) - specifically from how *their* first hour went:

  first-hour range   high - low of 09:30-10:29, as a median and bands, in points
                     (percent of the open, applied to today's pre-open price);
                     compared with the naive guess, the median of the last 40
                     sessions
  opening range      the same for the first 15 minutes (ORH - ORL)
  first break        which side of the opening range price crossed first
                     between 09:45 and 10:30 - 'above' ORH, 'below' ORL or 'none':
                     the matches' frequencies shrunk toward the base rates of all
                     earlier sessions (worth PRIOR_WEIGHT sessions), so matches
                     that say nothing leave the base rates, next to which they
                     are shown
  fan                the 10/25/50/75/90th percentiles of the matches' paths
                     (percent from their open), minute by minute, anchored at
                     today's pre-open price

``backtest`` replays this on every stored NQ session with only earlier sessions
and scores the range against the naive guess and the break probabilities
against the base rates.

Every session's first hour comes from one query (``opening_window_bars``), on
the contract that was trading that day.
"""

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from database import forecast_store as store
from database.queries import _expires_on_or_after, opening_window_bars
from features import calendar as cal
from features import catalogue as catv2
from features.indicators import finite

MINUTES = 60
OR_MINUTES = 15
MIN_BARS = 55            # of the 60 minutes; a missing minute repeats the previous close
K = 20
MIN_MATCHES = 10
NAIVE_WINDOW = 40
PRIOR_WEIGHT = 10.0      # the base rates count as this many sessions next to the matches
QUANTILES = (10, 25, 50, 75, 90)
SIDES = ("above", "none", "below")


@dataclass
class FirstHour:
    session_date: str
    open: float
    or_high: float
    or_low: float
    high: float                 # 09:30-10:29
    low: float
    close: float                # the 10:29 close
    path: np.ndarray            # MINUTES closes, percent from the open
    or_width: float             # percent of the open
    width: float                # percent of the open
    first_break: str            # above / below / none
    break_minute: Optional[int]
    contract_id: Optional[int] = None


def _open_at(day: str) -> pd.Timestamp:
    try:
        return pd.Timestamp(cal.session(day).rth_open_at)
    except cal.CalendarCoverageError:
        return pd.Timestamp(f"{day} 09:30").tz_localize("America/New_York").tz_convert("UTC")


def first_hour(bars: pd.DataFrame, open_at, session_date: str,
               contract_id: Optional[int] = None) -> Optional[FirstHour]:
    """
    One session's first hour from its 1-minute bars (``timestamp_utc``, OHLC).
    None without the 09:30 bar, with fewer than MIN_BARS of the 60 minutes, or
    when the price never moved in the hour (high == low: placeholder or stale
    bars, not a real session).
    """
    if bars is None or bars.empty:
        return None
    ts = pd.to_datetime(bars["timestamp_utc"], utc=True)
    minute = ((ts - pd.Timestamp(open_at).tz_convert("UTC")).dt.total_seconds() // 60).astype(int)
    w = bars.assign(minute=minute.to_numpy())
    w = w[(w["minute"] >= 0) & (w["minute"] < MINUTES)].drop_duplicates("minute").set_index("minute")
    if 0 not in w.index or len(w) < MIN_BARS:
        return None
    w = w.reindex(range(MINUTES))
    w["close"] = w["close"].ffill()
    w["high"] = w["high"].fillna(w["close"])
    w["low"] = w["low"].fillna(w["close"])
    o = float(w.at[0, "open"])
    if not o or not float(w["high"].max()) > float(w["low"].min()):
        return None
    orh, orl = float(w["high"].iloc[:OR_MINUTES].max()), float(w["low"].iloc[:OR_MINUTES].min())
    side, minute_of = "none", None
    for t in range(OR_MINUTES, MINUTES):
        up, down = w.at[t, "high"] > orh, w.at[t, "low"] < orl
        if up or down:
            if up and down:          # both in one minute: its close decides
                side = "above" if w.at[t, "close"] >= (orh + orl) / 2 else "below"
            else:
                side = "above" if up else "below"
            minute_of = t
            break
    closes = w["close"].to_numpy(dtype=float)
    hi, lo = float(w["high"].max()), float(w["low"].min())
    return FirstHour(session_date=str(session_date), open=o, or_high=orh, or_low=orl, high=hi, low=lo,
                     close=float(closes[-1]), path=(closes - o) / o * 100.0,
                     or_width=(orh - orl) / o * 100.0, width=(hi - lo) / o * 100.0,
                     first_break=side, break_minute=minute_of, contract_id=contract_id)


def load_first_hours(conn, symbol: str) -> Dict[str, FirstHour]:
    """``{trading_day: FirstHour}`` of every stored session of ``symbol``, oldest first."""
    rows = opening_window_bars(conn, symbol, MINUTES)
    if not rows:
        return {}
    df = pd.DataFrame([dict(r) for r in rows])
    df["trading_day"] = df["trading_day"].astype(str)
    out: Dict[str, FirstHour] = {}
    for day, g in df.groupby("trading_day", sort=True):
        held = g[["contract_id", "expiry"]].drop_duplicates().sort_values("expiry")
        later = [r for r in held.itertuples() if _expires_on_or_after(r.expiry, day)]
        earlier = [r for r in held.itertuples() if not _expires_on_or_after(r.expiry, day)]
        for r in later + earlier[::-1]:          # the contract trading that day first
            fh = first_hour(g[g["contract_id"] == r.contract_id], _open_at(day), day, int(r.contract_id))
            if fh is not None:
                out[day] = fh
                break
    return out


# --------------------------------------------------------------------------
# Forecast
# --------------------------------------------------------------------------

def _freq(sides: List[str], alpha: float = 0.0) -> Dict[str, float]:
    n = len(sides)
    return {s: (sides.count(s) + alpha) / (n + alpha * len(SIDES)) for s in SIDES} if n or alpha else \
        {s: math.nan for s in SIDES}


def _shrunk(sides: List[str], base: Dict[str, float]) -> Dict[str, float]:
    """The frequencies of ``sides`` shrunk toward ``base`` (worth PRIOR_WEIGHT observations)."""
    n = len(sides)
    return {s: (sides.count(s) + PRIOR_WEIGHT * base[s]) / (n + PRIOR_WEIGHT) for s in SIDES}


def forecast(day: str, ranked_dates: List[str], hours: Dict[str, FirstHour], anchor: Optional[float],
             k: int = K) -> Optional[Dict[str, Any]]:
    """
    The first-hour forecast for ``day`` from the first ``k`` of ``ranked_dates``
    (earlier sessions, closest pre-open state first) that have a first hour.
    Widths and the fan are in percent of the open, and in points when
    ``anchor`` (today's pre-open price) is given. None with fewer than
    MIN_MATCHES matches.
    """
    day = str(day)
    matches = [hours[d] for d in ranked_dates if d < day and d in hours][:k]
    if len(matches) < MIN_MATCHES:
        return None
    earlier = [h for d, h in hours.items() if d < day]
    naive = [h.width for h in earlier[-NAIVE_WINDOW:]]
    to_pts = (lambda pct: pct / 100.0 * anchor) if anchor else (lambda pct: None)
    width_q = np.percentile([m.width for m in matches], QUANTILES)
    or_q = np.percentile([m.or_width for m in matches], QUANTILES)
    fan = np.percentile(np.vstack([m.path for m in matches]), QUANTILES, axis=0)
    naive_width = float(np.median(naive)) if naive else None
    base = _freq([h.first_break for h in earlier], alpha=1.0)
    return {
        "day": day, "n": len(matches), "dates": [m.session_date for m in matches], "anchor": anchor,
        "width_pct": dict(zip(QUANTILES, map(float, width_q))),
        "width_pts": {q: to_pts(v) for q, v in zip(QUANTILES, width_q)},
        "or_width_pct": dict(zip(QUANTILES, map(float, or_q))),
        "or_width_pts": {q: to_pts(v) for q, v in zip(QUANTILES, or_q)},
        "naive_width_pct": naive_width, "naive_width_pts": to_pts(naive_width) if naive_width else None,
        "first_break_counts": {s: sum(m.first_break == s for m in matches) for s in SIDES},
        "first_break": _shrunk([m.first_break for m in matches], base),
        "base_rates": base,
        "fan_pct": {q: fan[i] for i, q in enumerate(QUANTILES)},
        "fan_price": {q: anchor * (1 + fan[i] / 100.0) for i, q in enumerate(QUANTILES)} if anchor else None,
    }


def outlook(conn, symbol: str, day: str, target_v1: Dict[str, Any], v1_history=None,
            hours: Optional[Dict[str, FirstHour]] = None, k: int = K) -> Dict[str, Any]:
    """
    The first-hour forecast for the dashboard: ``{'forecast', 'actual', 'method',
    'pool'}``. ``forecast`` is None when too few matches have a first hour;
    ``actual`` is the day's own first hour once it is stored.
    """
    from forecaster.analogue import rank_preopen

    day = str(day)
    hours = load_first_hours(conn, symbol) if hours is None else hours
    found = rank_preopen(conn, symbol, day, target_v1, v1_history)
    anchor = None
    if found["target"] is not None:
        anchor = finite(found["target"]["reference_values"].get("P"))
    if anchor is None and target_v1:
        prev, gap = finite(target_v1.get("previous_rth_close")), finite(target_v1.get("gap"))
        anchor = prev + gap if prev is not None and gap is not None else prev
    fc = forecast(day, [r["session_date"] for r in found["ranked"]], hours, anchor, k)
    return {"forecast": fc, "actual": hours.get(day), "method": found["method"], "pool": found["pool"]}


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------

def _summary(model: List[float], base: List[float]) -> Dict[str, float]:
    m, b = np.asarray(model, float), np.asarray(base, float)
    g = b - m
    n = len(g)
    return {"n": n, "model": float(m.mean()) if n else math.nan, "base": float(b.mean()) if n else math.nan,
            "gain": float(g.mean()) if n else math.nan,
            "gain_se": float(g.std(ddof=1) / math.sqrt(n)) if n > 1 else math.nan}


def backtest(conn, symbol: str = "NQ", k: int = K, min_history: int = 60) -> Dict[str, Any]:
    """
    Walk-forward over every stored point-in-time snapshot of ``symbol`` whose
    session has a first hour and at least ``min_history`` earlier such sessions:
    the forecast from earlier sessions only, scored against what happened.

      range        |log(actual / forecast median)| vs the naive last-40 median (lower is better)
      first break  3-class log loss, matches (shrunk toward the base rates) vs the base rates
      breakout     2-class log loss of 'any break' vs 'none', same
      fan          share of sessions whose 10:29 close lies inside the 10-90 % and 25-75 % bands

    Gains are base minus model: > 0 means the forecast beat the simple guess.
    """
    from forecaster.analogue import MATCH_FEATURES, MIN_V2_DIMS, _v2_vector
    from matching.normalizer import rank_analogues

    hours = load_first_hours(conn, symbol)
    snaps = store.snapshots_before(conn, "9999-12-31", catv2.FEATURE_VERSION, symbol)
    width_m, width_b, brk_m, brk_b, any_m, any_b = [], [], [], [], [], []
    in80 = in50 = 0
    for i, snap in enumerate(snaps):
        day = snap["session_date"]
        actual = hours.get(day)
        earlier = [s for s in snaps[:i] if s["session_date"] in hours]
        if actual is None or len(earlier) < min_history:
            continue
        cands = [{"session_date": s["session_date"], "vector": _v2_vector(s["features"])} for s in earlier]
        ranked = rank_analogues(_v2_vector(snap["features"]), cands, MATCH_FEATURES, MIN_V2_DIMS)
        fc = forecast(day, [r["session_date"] for r in ranked], hours, None, k)
        if fc is None or not fc["naive_width_pct"] or not fc["width_pct"][50] > 0:
            continue
        width_m.append(abs(math.log(actual.width / fc["width_pct"][50])))
        width_b.append(abs(math.log(actual.width / fc["naive_width_pct"])))
        brk_m.append(-math.log(fc["first_break"][actual.first_break]))
        brk_b.append(-math.log(fc["base_rates"][actual.first_break]))
        broke = actual.first_break != "none"
        p_m = 1 - fc["first_break"]["none"]
        p_b = 1 - fc["base_rates"]["none"]
        any_m.append(-math.log(p_m if broke else 1 - p_m))
        any_b.append(-math.log(p_b if broke else 1 - p_b))
        end = actual.path[-1]
        in80 += fc["fan_pct"][10][-1] <= end <= fc["fan_pct"][90][-1]
        in50 += fc["fan_pct"][25][-1] <= end <= fc["fan_pct"][75][-1]
    n = len(width_m)
    return {"sessions": n, "k": k, "range": _summary(width_m, width_b),
            "first_break": _summary(brk_m, brk_b), "breakout": _summary(any_m, any_b),
            "fan_80": in80 / n if n else math.nan, "fan_50": in50 / n if n else math.nan}


def format_backtest(r: Dict[str, Any]) -> str:
    lines = [f"First-hour forecast backtest: {r['sessions']} session(s), {r['k']} pre-open matches each, "
             f"only earlier sessions used. Gain = simple guess minus forecast (> 0: the forecast is better).", "",
             f"{'':34} {'forecast':>9} {'simple':>9} {'gain ± SE':>20}"]
    for key, label in (("range", "first-hour range, |log error|"), ("first_break", "first break, log loss (3-way)"),
                       ("breakout", "break vs none, log loss")):
        s = r[key]
        verdict = "better" if s["gain"] > 2 * s["gain_se"] else "worse" if s["gain"] < -2 * s["gain_se"] else "no difference"
        lines.append(f"{label:34} {s['model']:9.4f} {s['base']:9.4f} {s['gain']:+10.4f} ± {s['gain_se']:.4f}  {verdict}")
    lines += ["", f"10:29 close inside the fan: 10-90 % band {r['fan_80'] * 100:.0f} % of sessions (ideal 80), "
                  f"25-75 % band {r['fan_50'] * 100:.0f} % (ideal 50)."]
    return "\n".join(lines)
