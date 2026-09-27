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

The range has three estimators - the matches' median, a ridge regression of
the log range on the pre-open volatility inputs (models_v2.VOL_FEATURES), and
the usual range - and ``backtest_records`` replays all three walk-forward on
every stored NQ session, with the fan and the break probabilities. For a day,
``calibration`` reads only the records of earlier sessions: the range
estimator with the smallest error so far, its likely band from its own past
errors (so the band holds what it says), and how much to widen the fan so its
10-90 % and 25-75 % bands held 80 % and 50 % of the 10:29 closes. Which side
breaks has shown no skill, so the dashboard shows the base rates for it.

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


MIN_CALIBRATION = 30     # earlier scored sessions before the calibration replaces the defaults
REFIT_EVERY = 5
SHOWN_MATCHES = 10       # pre-open matches offered for overlay on the chart
RANGE_METHODS = ("matches", "regression", "usual")
RANGE_LABELS = {"matches": "median of the pre-open matches",
                "regression": "ridge regression on the pre-open volatility inputs",
                "usual": "median of the last 40 sessions"}


# --------------------------------------------------------------------------
# Walk-forward records
# --------------------------------------------------------------------------

def _vol_row(features: Dict[str, Any]) -> List[float]:
    from forecaster.models_v2 import VOL_FEATURES
    return [np.nan if (v := finite(features.get(k))) is None else v for k in VOL_FEATURES]


def _ridge():
    from sklearn.linear_model import RidgeCV
    from forecaster.metric_study import RIDGE_ALPHAS, _vol_pipeline
    return _vol_pipeline(RidgeCV(alphas=RIDGE_ALPHAS))


def _fit_ridge(rows: List[List[float]], widths: List[float]):
    import warnings
    pipe = _ridge()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pipe.fit(np.asarray(rows, dtype=float), np.log(np.asarray(widths, dtype=float)))
    return pipe


def _predict_ridge(pipe, row: List[float]) -> float:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return float(np.exp(pipe.predict(np.asarray([row], dtype=float))[0]))


def load_history(conn, symbol: str = "NQ", k: int = K, min_history: int = 60) -> Dict[str, Any]:
    """
    Everything the first-hour forecast needs for ``symbol``, loaded once:
    ``{'hours', 'snaps', 'records'}`` - every session's first hour, the stored
    point-in-time snapshots, and the walk-forward records (``backtest_records``).
    """
    hours = load_first_hours(conn, symbol)
    snaps = store.snapshots_before(conn, "9999-12-31", catv2.FEATURE_VERSION, symbol)
    return {"hours": hours, "snaps": snaps, "records": backtest_records(hours, snaps, k, min_history)}


def _z(end: float, fan_end: Dict[int, float], lo: int, hi: int) -> float:
    """How far ``end`` lies from the fan's median, in units of the band's half on
    its side (<= 1: inside the lo-hi band) - the unit ``calibrated_fan`` scales."""
    med = fan_end[50]
    half = fan_end[hi] - med if end >= med else med - fan_end[lo]
    if half > 0:
        return abs(end - med) / half
    return 0.0 if end == med else math.inf


def backtest_records(hours: Dict[str, FirstHour], snaps: List[Dict[str, Any]], k: int = K,
                     min_history: int = 60) -> List[Dict[str, Any]]:
    """
    The forecast replayed on every snapshot whose session has a first hour and
    at least ``min_history`` earlier such sessions, from earlier sessions only.
    Each record: 'day', 'actual' (first-hour width, % of the open), the three
    range estimates, 'z80' / 'z50' (the 10:29 close's distance from the fan's
    median in units of its 10-90 % / 25-75 % band's half on that side), and the break outcome
    with its matched and base-rate probabilities.
    """
    from forecaster.analogue import MATCH_FEATURES, MIN_V2_DIMS, _v2_vector
    from matching.normalizer import rank_analogues

    usable = [s for s in snaps if s["session_date"] in hours]
    records: List[Dict[str, Any]] = []
    pipe, since_fit = None, REFIT_EVERY
    for i, snap in enumerate(usable):
        day = snap["session_date"]
        earlier = usable[:i]
        if len(earlier) < min_history:
            continue
        actual = hours[day]
        cands = [{"session_date": s["session_date"], "vector": _v2_vector(s["features"])} for s in earlier]
        ranked = rank_analogues(_v2_vector(snap["features"]), cands, MATCH_FEATURES, MIN_V2_DIMS)
        fc = forecast(day, [r["session_date"] for r in ranked], hours, None, k)
        if fc is None or not fc["naive_width_pct"] or not fc["width_pct"][50] > 0:
            continue
        if pipe is None or since_fit >= REFIT_EVERY:
            pipe = _fit_ridge([_vol_row(s["features"]) for s in earlier],
                              [hours[s["session_date"]].width for s in earlier])
            since_fit = 0
        since_fit += 1
        end = actual.path[-1]
        fan_end = {q: v[-1] for q, v in fc["fan_pct"].items()}
        records.append({
            "day": day, "actual": actual.width,
            "estimates": {"matches": fc["width_pct"][50], "regression": _predict_ridge(pipe, _vol_row(snap["features"])),
                          "usual": fc["naive_width_pct"]},
            "z80": _z(end, fan_end, 10, 90), "z50": _z(end, fan_end, 25, 75),
            "first_break": actual.first_break, "p_matches": fc["first_break"], "p_base": fc["base_rates"],
        })
    return records


def calibration(records: List[Dict[str, Any]], day: str) -> Dict[str, Any]:
    """
    From the records of sessions before ``day`` only: the range estimator with
    the smallest mean |log error| ('method'), its 50 % and 80 % error quantiles
    ('band50', 'band80': the likely band is estimate x exp(+/- band)), each
    method's mean error ('errors'), and the fan widening factors ('fan80',
    'fan50'). Defaults (the matches, no widening, bands None) below
    MIN_CALIBRATION earlier records.
    """
    earlier = [r for r in records if r["day"] < str(day)]
    out = {"n": len(earlier), "method": "matches", "errors": {}, "band50": None, "band80": None,
           "fan80": 1.0, "fan50": 1.0}
    if len(earlier) < MIN_CALIBRATION:
        return out
    errs = {m: np.array([abs(math.log(r["actual"] / r["estimates"][m])) for r in earlier]) for m in RANGE_METHODS}
    out["errors"] = {m: float(e.mean()) for m, e in errs.items()}
    out["method"] = min(RANGE_METHODS, key=lambda m: (out["errors"][m], RANGE_METHODS.index(m)))
    chosen = errs[out["method"]]
    out["band50"], out["band80"] = float(np.quantile(chosen, 0.5)), float(np.quantile(chosen, 0.8))
    z80 = np.array([r["z80"] for r in earlier])
    z50 = np.array([r["z50"] for r in earlier])
    out["fan80"] = float(np.quantile(z80[np.isfinite(z80)], 0.8)) if np.isfinite(z80).any() else 1.0
    out["fan50"] = float(np.quantile(z50[np.isfinite(z50)], 0.5)) if np.isfinite(z50).any() else 1.0
    return out


def calibrated_fan(fan: Dict[int, np.ndarray], fan80: float, fan50: float) -> Dict[int, np.ndarray]:
    """The fan's bands widened (or narrowed) around its median by the calibration factors."""
    med = fan[50]
    return {10: med + fan80 * (fan[10] - med), 25: med + fan50 * (fan[25] - med), 50: med,
            75: med + fan50 * (fan[75] - med), 90: med + fan80 * (fan[90] - med)}


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

def matches_with_first_hour(ranked: List[Dict[str, Any]], day: str, hours: Dict[str, FirstHour],
                            n: int = SHOWN_MATCHES) -> List[Dict[str, Any]]:
    """
    The ``n`` closest of ``ranked`` (``analogue.rank_preopen``, closest first)
    before ``day`` with a stored first hour - the matches offered for overlay -
    as {'match_date', 'similarity_score', 'distance', 'ranking'}.
    """
    kept = [r for r in ranked if r["session_date"] < str(day) and r["session_date"] in hours][:n]
    return [{"match_date": r["session_date"], "similarity_score": r["similarity_score"],
             "distance": r["distance"], "ranking": i + 1} for i, r in enumerate(kept)]


def break_rates(hours: Dict[str, FirstHour], day: str) -> Dict[str, float]:
    """
    How the opening range broke by 10:30 over the sessions before ``day``:
    'any', and the first break 'above' / 'below' / 'none' (add-one smoothed),
    with 'n' sessions - the usual rates, as the side has not been predictable.
    """
    earlier = [h.first_break for d, h in hours.items() if d < str(day)]
    base = _freq(earlier, alpha=1.0)
    return {"any": 1 - base["none"], **base, "n": len(earlier)}


def outlook(conn, symbol: str, day: str, target_v1: Dict[str, Any], v1_history=None,
            history: Optional[Dict[str, Any]] = None, k: int = K) -> Dict[str, Any]:
    """
    The first-hour forecast for the dashboard: ``{'forecast', 'range', 'fan',
    'breaks', 'calibration', 'actual', 'method', 'pool', 'matches'}`` - the last
    the SHOWN_MATCHES closest pre-open matches with a first hour, closest first. ``history`` is
    ``load_history(conn, symbol)``, loaded once per symbol. ``forecast`` is None
    when too few matches have a first hour.
    """
    from forecaster.analogue import rank_preopen

    day = str(day)
    history = history or {"hours": load_first_hours(conn, symbol), "snaps": [], "records": []}
    hours = history["hours"]
    found = rank_preopen(conn, symbol, day, target_v1, v1_history)
    anchor = None
    if found["target"] is not None:
        anchor = finite(found["target"]["reference_values"].get("P"))
    if anchor is None and target_v1:
        prev, gap = finite(target_v1.get("previous_rth_close")), finite(target_v1.get("gap"))
        anchor = prev + gap if prev is not None and gap is not None else prev
    fc = forecast(day, [r["session_date"] for r in found["ranked"]], hours, anchor, k)
    matches = matches_with_first_hour(found["ranked"], day, hours)
    out = {"forecast": fc, "actual": hours.get(day), "method": found["method"], "pool": found["pool"],
           "calibration": None, "range": None, "fan": None, "breaks": None, "matches": matches}
    if fc is None:
        return out
    cal_ = calibration(history["records"], day)
    out["calibration"] = cal_

    # The range: the estimator that has done best on earlier sessions.
    estimate = fc["width_pct"][50]
    if cal_["method"] == "usual":
        estimate = fc["naive_width_pct"]
    elif cal_["method"] == "regression" and found["target"] is not None:
        earlier = [s for s in history["snaps"] if s["session_date"] < day and s["session_date"] in hours]
        if len(earlier) >= MIN_CALIBRATION:
            pipe = _fit_ridge([_vol_row(s["features"]) for s in earlier], [hours[s["session_date"]].width for s in earlier])
            estimate = _predict_ridge(pipe, _vol_row(found["target"]["features"]))
    band = (lambda b, sign: estimate * math.exp(sign * b)) if cal_["band50"] is not None else None
    to_pts = (lambda pct: pct / 100.0 * anchor) if anchor else (lambda pct: None)
    out["range"] = {
        "method": cal_["method"], "label": RANGE_LABELS[cal_["method"]], "estimate_pct": estimate,
        "estimate_pts": to_pts(estimate),
        "band50_pts": (to_pts(band(cal_["band50"], -1)), to_pts(band(cal_["band50"], 1))) if band else
        (fc["width_pts"][25], fc["width_pts"][75]),
        "band80_pts": (to_pts(band(cal_["band80"], -1)), to_pts(band(cal_["band80"], 1))) if band else
        (fc["width_pts"][10], fc["width_pts"][90]),
        "usual_pts": fc["naive_width_pts"], "errors": cal_["errors"], "calibrated": band is not None,
    }
    widened = calibrated_fan(fc["fan_pct"], cal_["fan80"], cal_["fan50"])
    out["fan"] = {"pct": widened, "price": {q: anchor * (1 + v / 100.0) for q, v in widened.items()} if anchor else None,
                  "fan80": cal_["fan80"], "fan50": cal_["fan50"], "calibrated": cal_["n"] >= MIN_CALIBRATION}
    base = fc["base_rates"]
    out["breaks"] = {"any": 1 - base["none"], "above": base["above"], "below": base["below"], "none": base["none"]}
    return out


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


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    The backtest from the walk-forward records:

      range        |log(actual / estimate)| of the matches' median and of the
                   regression, each against the usual range (lower is better)
      first break  3-class log loss, matches (shrunk toward the base rates) vs the base rates
      breakout     2-class log loss of 'any break' vs 'none', same
      fan          share of sessions whose 10:29 close lies inside the 10-90 % and
                   25-75 % bands, as built and after the walk-forward calibration
                   (factors from earlier sessions only, from MIN_CALIBRATION on)

    Gains are base minus model: > 0 means the forecast beat the simple guess.
    """
    def err(r, m):
        return abs(math.log(r["actual"] / r["estimates"][m]))

    brk_m = [-math.log(r["p_matches"][r["first_break"]]) for r in records]
    brk_b = [-math.log(r["p_base"][r["first_break"]]) for r in records]
    any_m, any_b = [], []
    for r in records:
        broke = r["first_break"] != "none"
        for p, out in ((1 - r["p_matches"]["none"], any_m), (1 - r["p_base"]["none"], any_b)):
            out.append(-math.log(p if broke else 1 - p))
    cal80 = cal50 = n_cal = 0
    for r in records:
        c = calibration(records, r["day"])
        if c["n"] < MIN_CALIBRATION:
            continue
        n_cal += 1
        cal80 += r["z80"] <= c["fan80"]
        cal50 += r["z50"] <= c["fan50"]
    n = len(records)
    return {
        "sessions": n,
        "range_matches": _summary([err(r, "matches") for r in records], [err(r, "usual") for r in records]),
        "range_regression": _summary([err(r, "regression") for r in records], [err(r, "usual") for r in records]),
        "first_break": _summary(brk_m, brk_b), "breakout": _summary(any_m, any_b),
        "fan_80": sum(r["z80"] <= 1 for r in records) / n if n else math.nan,
        "fan_50": sum(r["z50"] <= 1 for r in records) / n if n else math.nan,
        "fan_80_calibrated": cal80 / n_cal if n_cal else math.nan,
        "fan_50_calibrated": cal50 / n_cal if n_cal else math.nan, "calibrated_sessions": n_cal,
    }


def backtest(conn, symbol: str = "NQ", k: int = K, min_history: int = 60) -> Dict[str, Any]:
    """``summarize`` of ``backtest_records`` for ``symbol``'s stored sessions."""
    history = load_history(conn, symbol, k, min_history)
    return {**summarize(history["records"]), "k": k}


def format_backtest(r: Dict[str, Any]) -> str:
    lines = [f"First-hour forecast backtest: {r['sessions']} session(s), {r['k']} pre-open matches each, "
             f"only earlier sessions used. Gain = simple guess minus forecast (> 0: the forecast is better).", "",
             f"{'':42} {'forecast':>9} {'simple':>9} {'gain ± SE':>20}"]
    for key, label in (("range_matches", "range, |log error|: matches' median"),
                       ("range_regression", "range, |log error|: regression"),
                       ("first_break", "first break side, log loss (3-way)"),
                       ("breakout", "break vs none, log loss")):
        s = r[key]
        verdict = ("better" if s["gain"] > 2 * s["gain_se"] else "worse" if s["gain"] < -2 * s["gain_se"]
                   else "no difference")
        lines.append(f"{label:42} {s['model']:9.4f} {s['base']:9.4f} {s['gain']:+10.4f} ± {s['gain_se']:.4f}  {verdict}")
    lines += ["", "10:29 close inside the fan (ideal 80 % / 50 %):",
              f"  as built:   10-90 % band {r['fan_80'] * 100:.0f} %, 25-75 % band {r['fan_50'] * 100:.0f} %",
              f"  calibrated: 10-90 % band {r['fan_80_calibrated'] * 100:.0f} %, 25-75 % band "
              f"{r['fan_50_calibrated'] * 100:.0f} % (walk-forward, {r['calibrated_sessions']} sessions)"]
    return "\n".join(lines)
