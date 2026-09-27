# forecaster/labels_v2.py
"""
Realised outcomes for the NQ pre-open snapshots: the continuous outcome metrics
and the deterministic labels predictions are scored against (nq_schema_v2,
sections 8-11).

Everything is measured from NQ one-minute RTH bars of the snapshot's own
contract. O is the 09:30 bar's open; C5, C15 and C are the 09:34, 09:44 and
15:59 closes; every normalised value divides by the snapshot's frozen A (daily
ATR14 through the previous session), and the opening labels use the snapshot's
frozen overnight extremes - post-open bars never redefine them.

  METRIC_VERSION  nq_outcome_metrics_v4: section 10, first-move barrier 0.10 A,
                  plus the first hour's range and first opening-range break.
  LABEL_VERSION   nq_labels_v5_candidate: the five targets of section 8, with the
                  section 11 thresholds retuned on the 248 labelled NQ sessions
                  stored by 2026-09-25 (``nq_forecast_v2.py label-study``), two
                  range-regime targets and three first-hour targets. Each target's vocabulary is
                  registered in forecast.label_definitions; the database checks
                  predictions and realised labels against that one row.

Range regime (nq_labels_v4_candidate): ``range_15m_regime`` and
``range_rth_regime`` are 'wide' when the session's range_15m_atr /
range_rth_atr exceeds the median of the same metric over the previous 40
sessions that have it, else 'narrow'; fewer than 40 earlier values make the
label ineligible (insufficient_history). Those values are the stored metrics
of earlier sessions, so the threshold is known before the session opens. The
pre-open features say nothing about direction but relate robustly to the range
(``nq_forecast_v2.py metric-study``: Spearman 0.3-0.45 with the overnight
volume ratio, relative 1m ATR, overnight and prior ranges and VIX; for the
15-minute label a logistic fit on volatility inputs gained 0.066 nats per
session out of sample). The other five targets are as in nq_labels_v3_candidate.

First hour (nq_labels_v5_candidate), the window traded most - 09:30 to 10:30:
``direction_1h`` (10:29 close vs 09:30 open: up / down beyond 0.10 A, else
flat), ``range_1h_regime`` (the first hour's high - low in A, wide / narrow
against the previous 40 sessions' median, as the other range regimes), and
``first_break_1h`` (which side of the 15-minute opening range - its high and
low over 09:30-09:44 - price crossed first between 09:45 and 10:29: above /
below / none; both in one minute are decided by that minute's close against
the range's middle). All three need every minute of the hour.

The section 11 starting values (nq_labels_v2_candidate) labelled 69 % of
openings and 65 % of sessions 'mixed' and left 15 % of first moves ambiguous
(both barriers of 0.05 A inside the first minute). v3 changes:

  first move        B = 0.10 A (was 0.05): 1 ambiguous session instead of 37 and a
                    real 'neither' class (17 %, quiet opens).
  opening type      two_sided u, d >= 0.12 (0.15); drive |r| >= 0.15 (0.20),
                    counter-excursion <= 0.08 (0.05), efficiency >= 0.40 (0.50);
                    range w <= 0.25 (0.20), |r| <= 0.08 (0.05). 'mixed' 69 -> 38 %.
  session type      trend |r| >= 0.40 (0.50), close location >= 0.75 / <= 0.25
                    (0.80 / 0.20), 5m efficiency >= 0.15 (0.30 - above the 90th
                    percentile, 0.26); two_sided_volatile w >= 0.90 (1.00), u, d >=
                    0.25 (0.30); range w <= 0.80 (0.60), |r| <= 0.25 (0.20).
                    'mixed' 65 -> 33 %. Reversal and the directions are unchanged.

The label rules consume the metrics only. A required measurement that is
missing makes the label ineligible (``label_status``), never an automatic
'mixed' or 'flat'. Changing a threshold, window or vocabulary means a new
version.
"""

import hashlib
import json
from datetime import time, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from features import calendar as cal
from features.indicators import finite, path_efficiency, ratio

ONE_MIN = timedelta(minutes=1)
RTH_END = time(16, 0)   # the full-RTH targets' window end, whatever the scheduled close

METRIC_VERSION = "nq_outcome_metrics_v4"
LABEL_VERSION = "nq_labels_v5_candidate"

LABEL_STATUSES = ("valid", "missing_bars", "ambiguous_intrabar", "incomplete_window",
                  "shortened_session", "not_yet_available", "invalid_reference", "insufficient_history")

# Section 11 rules with thresholds set from the realised label mix (see the module
# docstring); issue a new LABEL_VERSION after any change.
PARAMETERS = {
    "first_move_barrier": "B = max(1.0 point, 0.10 * A); barriers O + B and O - B",
    "first_move_min_points": 1.0,
    "first_move_atr_fraction": 0.10,
    "direction_15m_band_atr": 0.10,
    "direction_rth_band_atr": 0.20,
    "direction_1h_band_atr": 0.10,
    "first_break_1h": "first minute in [09:45, 10:30) whose high > ORH or low < ORL, with ORH / ORL the "
                      "high / low of [09:30, 09:45); both in one minute: its close >= (ORH + ORL) / 2 -> above",
    "on_breach_atr": 0.02,
    "opening_type_15m": {
        "two_sided": {"u_min": 0.12, "d_min": 0.12},
        "sweep_low_rebound": {"r_gt": 0.10},
        "sweep_high_reverse": {"r_lt": -0.10},
        "drive": {"r_abs_min": 0.15, "counter_excursion_max": 0.08, "e_min": 0.40},
        "range": {"w_max": 0.25, "r_abs_max": 0.08},
    },
    "session_type_rth": {
        "reversal": {"f_abs_min": 0.20, "r_abs_min": 0.20},
        "trend": {"r_abs_min": 0.40, "q_bull_min": 0.75, "q_bear_max": 0.25, "e_min": 0.15},
        "two_sided_volatile": {"w_min": 0.90, "u_min": 0.25, "d_min": 0.25},
        "range": {"w_max": 0.80, "r_abs_max": 0.25},
    },
    "range_regime": {
        "median_window": 40,
        "rule": "wide if the metric > the median of its values over the previous median_window sessions "
                "(one snapshot per session, same metric version), else narrow",
    },
    "window_coverage": "every one-minute bar of the window is required",
    "early_close": "full-RTH targets are ineligible (shortened_session); opening targets stay eligible",
}

TARGETS: Dict[str, Dict[str, Any]] = {
    "first_move_5m": {
        "labels": ("up_first", "down_first", "neither"),
        "window_minutes": 5,
        "definition": "Which barrier O +/- B (B = max(1 point, 0.10 A)) is touched first in [09:30, 09:35), "
                      "by first-touch minute index. Both first touched in the same minute: that minute's "
                      "open at/above the upper barrier -> up_first, at/below the lower -> down_first, "
                      "otherwise ineligible (ambiguous_intrabar).",
    },
    "opening_type_15m": {
        "labels": ("drive_up", "drive_down", "sweep_low_rebound", "sweep_high_reverse",
                   "two_sided", "range", "mixed"),
        "window_minutes": 15,
        "definition": "First matching rule over [09:30, 09:45), with r, u, d, w, e the 15m return, up/down "
                      "excursion, range (all / A) and efficiency: two_sided u>=0.12 and d>=0.12; "
                      "sweep_low_rebound ON-low breach-and-close-reclaim and r>0.10; sweep_high_reverse "
                      "ON-high breach-and-close-reject and r<-0.10; drive_up r>=0.15, d<=0.08, e>=0.40; "
                      "drive_down r<=-0.15, u<=0.08, e>=0.40; range w<=0.25 and |r|<=0.08; else mixed. "
                      "An unknown, potentially decisive higher-priority rule makes the label ineligible.",
    },
    "direction_15m": {
        "labels": ("up", "down", "flat"),
        "window_minutes": 15,
        "definition": "09:44 close vs 09:30 open: up if return_15m_atr > 0.10, down if < -0.10, else flat "
                      "(equality is flat).",
    },
    "direction_rth": {
        "labels": ("up", "down", "flat"),
        "window_minutes": None,
        "definition": "15:59 close vs 09:30 open: up if return_rth_atr > 0.20, down if < -0.20, else flat "
                      "(equality is flat). Ineligible on early-close sessions.",
    },
    "session_type_rth": {
        "labels": ("bull_trend", "bear_trend", "reversal", "two_sided_volatile", "range", "mixed"),
        "window_minutes": None,
        "definition": "First matching rule over [09:30, 16:00), with r, u, d, w the RTH return, excursions "
                      "and range (/ A), q the close location, e the 5m path efficiency and f the first-hour "
                      "return: reversal f>=0.20 and r<=-0.20, or f<=-0.20 and r>=0.20; bull_trend r>=0.40, "
                      "q>=0.75, e>=0.15; bear_trend r<=-0.40, q<=0.25, e>=0.15; two_sided_volatile w>=0.90, "
                      "u>=0.25, d>=0.25; range w<=0.80 and |r|<=0.25; else mixed. Ineligible on early-close "
                      "sessions.",
    },
    "range_15m_regime": {
        "labels": ("wide", "narrow"),
        "window_minutes": 15,
        "definition": "range_15m_atr over [09:30, 09:45) vs the median of range_15m_atr over the previous "
                      "40 sessions that have it: wide if greater, else narrow; fewer than 40 earlier values: "
                      "ineligible (insufficient_history).",
    },
    "range_rth_regime": {
        "labels": ("wide", "narrow"),
        "window_minutes": None,
        "definition": "range_rth_atr over [09:30, 16:00) vs the median of range_rth_atr over the previous 40 "
                      "sessions that have it (early closes have none): wide if greater, else narrow; fewer "
                      "than 40 earlier values: ineligible (insufficient_history). Ineligible on early-close "
                      "sessions.",
    },
}

TARGETS.update({
    "direction_1h": {
        "labels": ("up", "down", "flat"),
        "window_minutes": 60,
        "definition": "10:29 close vs 09:30 open: up if first_hour_return_atr > 0.10, down if < -0.10, else "
                      "flat (equality is flat).",
    },
    "range_1h_regime": {
        "labels": ("wide", "narrow"),
        "window_minutes": 60,
        "definition": "range_1h_atr (high - low of [09:30, 10:30) / A) vs the median of range_1h_atr over the "
                      "previous 40 sessions that have it: wide if greater, else narrow; fewer than 40 earlier "
                      "values: ineligible (insufficient_history).",
    },
    "first_break_1h": {
        "labels": ("above", "below", "none"),
        "window_minutes": 60,
        "definition": "Which side of the 15-minute opening range (high / low of [09:30, 09:45)) price crossed "
                      "first in [09:45, 10:30): above (a high above ORH), below (a low below ORL), none; both "
                      "in one minute: above if its close >= the range's middle, else below.",
    },
})

# Range-regime target -> the metric it compares with its trailing median.
RANGE_TARGETS = {"range_15m_regime": "range_15m_atr", "range_rth_regime": "range_rth_atr",
                 "range_1h_regime": "range_1h_atr"}

# Metric groups by the window they need; a group's metrics are all null when its
# window is incomplete.
_METRICS_5M = ("return_5m_atr", "first_up_touch_minute", "first_down_touch_minute", "first_move_barrier_points",
               "first_touch_tie_open")
_METRICS_15M = ("return_15m_atr", "up_excursion_15m_atr", "down_excursion_15m_atr", "range_15m_atr",
                "efficiency_15m", "on_low_breach_close_reclaim_15m", "on_high_breach_close_reject_15m")
_METRICS_60M = ("first_hour_return_atr", "range_1h_atr", "first_break_side_1h", "first_break_minute_1h")
_METRICS_RTH = ("return_rth_atr", "up_excursion_rth_atr", "down_excursion_rth_atr", "range_rth_atr",
                "rth_close_location", "efficiency_rth_5m")
METRIC_NAMES = ("open_0930",) + _METRICS_5M + _METRICS_15M + _METRICS_60M + _METRICS_RTH


def label_registry_record() -> Dict[str, Any]:
    targets = [{"target_id": t, "labels": list(d["labels"]), "definition": d["definition"],
                "parameters": {"window_minutes": d["window_minutes"], "metric_version": METRIC_VERSION,
                               "rules": PARAMETERS,
                               "anchor": "O = 09:30 open; A = daily ATR14 and ONH/ONL frozen in the snapshot"}}
               for t, d in TARGETS.items()]
    digest = hashlib.sha256(json.dumps(targets, sort_keys=True).encode()).hexdigest()
    return {"label_version": LABEL_VERSION, "definition_hash": digest, "targets": targets,
            "description": "nq_schema_v2 candidate labels, thresholds retuned on the realised label mix: first "
                           "move, opening type, 15m, first-hour and RTH direction, RTH session type, range "
                           "regimes and the first opening-range break (deterministic "
                           "rules over nq_outcome_metrics_v4)."}


# --------------------------------------------------------------------------
# Three-valued logic: a condition on a missing measurement is unknown (None).
# --------------------------------------------------------------------------

def _ge(x, t):
    return None if x is None else x >= t


def _le(x, t):
    return None if x is None else x <= t


def _gt(x, t):
    return None if x is None else x > t


def _lt(x, t):
    return None if x is None else x < t


def _and(*conds):
    if any(c is False for c in conds):
        return False
    return None if any(c is None for c in conds) else True


def _or(*conds):
    if any(c is True for c in conds):
        return True
    return None if any(c is None for c in conds) else False


def _first_rule(rules):
    """(label, None) for the first rule that is true; (None, 'invalid_reference') when
    a rule before it is unknown - it might have decided the label."""
    for label, cond in rules:
        if cond is None:
            return None, "invalid_reference"
        if cond:
            return label, None
    raise AssertionError("the last rule must be unconditional")


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def _window(df: pd.DataFrame, start, minutes: int) -> Optional[pd.DataFrame]:
    """The bars of [start, start + minutes), or None unless every minute is present."""
    end = start + minutes * ONE_MIN
    w = df[(df["bar_start_at"] >= start) & (df["bar_start_at"] < end)]
    expected = pd.date_range(start, end, freq="1min", inclusive="left")
    if len(w) != minutes or not (pd.DatetimeIndex(w["bar_start_at"]) == expected).all():
        return None
    return w.reset_index(drop=True)


def compute_metrics(df: pd.DataFrame, s: cal.Session, A: Optional[float], ONH: Optional[float],
                    ONL: Optional[float], params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Section 10 metrics from RTH bars ``df`` (bar_start_at UTC, open/high/low/close).
    Returns {'metrics': {...}, 'status': {...}}; every metric is present, null
    with a status when it cannot be measured. ``params`` (default: PARAMETERS)
    exists for threshold studies; stored outcomes always use PARAMETERS.
    """
    params = params or PARAMETERS
    metrics: Dict[str, Any] = {}
    status: Dict[str, str] = {}

    def put(key, value, fail="undefined"):
        if isinstance(value, bool):
            metrics[key], status[key] = value, "valid"
            return
        v = finite(value)
        metrics[key] = v
        status[key] = "valid" if v is not None else fail

    def void(keys, why):
        for k in keys:
            metrics[k], status[k] = None, why

    open_bar = df[df["bar_start_at"] == s.rth_open_at]
    O = float(open_bar["open"].iloc[0]) if not open_bar.empty else None
    put("open_0930", O, "missing_bars")
    A_ok = A is not None and A > 0

    def over_a(x):
        return None if x is None or not A_ok else x / A

    # --- [09:30, 09:35): first move -------------------------------------------
    w5 = _window(df, s.rth_open_at, 5)
    if w5 is None or O is None:
        void(_METRICS_5M, "missing_bars")
    elif not A_ok:
        void(_METRICS_5M, "invalid_reference")
    else:
        B = max(params["first_move_min_points"], params["first_move_atr_fraction"] * A)
        up = [i for i, h in enumerate(w5["high"]) if h >= O + B]
        dn = [i for i, lo in enumerate(w5["low"]) if lo <= O - B]
        put("return_5m_atr", over_a(float(w5["close"].iloc[-1]) - O))
        put("first_move_barrier_points", B)
        metrics["first_up_touch_minute"] = up[0] if up else None
        metrics["first_down_touch_minute"] = dn[0] if dn else None
        status["first_up_touch_minute"] = "valid" if up else "not_applicable"
        status["first_down_touch_minute"] = "valid" if dn else "not_applicable"
        tie = up and dn and up[0] == dn[0]
        put("first_touch_tie_open", float(w5["open"].iloc[up[0]]) if tie else None, "not_applicable")

    # --- [09:30, 09:45): opening ----------------------------------------------
    w15 = _window(df, s.rth_open_at, 15)
    if w15 is None or O is None:
        void(_METRICS_15M, "missing_bars")
    elif not A_ok:
        void(_METRICS_15M, "invalid_reference")
    else:
        H15, L15, C15 = float(w15["high"].max()), float(w15["low"].min()), float(w15["close"].iloc[-1])
        put("return_15m_atr", over_a(C15 - O))
        put("up_excursion_15m_atr", over_a(max(0.0, H15 - O)))
        put("down_excursion_15m_atr", over_a(max(0.0, O - L15)))
        put("range_15m_atr", over_a(H15 - L15))
        put("efficiency_15m", path_efficiency([O] + w15["close"].tolist()))
        breach = params["on_breach_atr"] * A
        for key, level, breached, confirmed in (
            ("on_low_breach_close_reclaim_15m", ONL,
             lambda bar, lv: bar["low"] <= lv - breach, lambda c, lv: c > lv),
            ("on_high_breach_close_reject_15m", ONH,
             lambda bar, lv: bar["high"] >= lv + breach, lambda c, lv: c < lv),
        ):
            if level is None:
                metrics[key], status[key] = None, "invalid_reference"
                continue
            closes = w15["close"].tolist()
            hit = [i for i, bar in w15.iterrows() if breached(bar, level)]
            put(key, bool(hit) and any(confirmed(c, level) for c in closes[hit[0]:]))

    # --- [09:30, 10:30): first hour -------------------------------------------
    w60 = _window(df, s.rth_open_at, 60)
    if w60 is None or O is None or s.scheduled_close_at < s.rth_open_at + 60 * ONE_MIN:
        void(_METRICS_60M, "missing_bars")
    elif not A_ok:
        void(_METRICS_60M, "invalid_reference")
    else:
        put("first_hour_return_atr", over_a(float(w60["close"].iloc[-1]) - O))
        put("range_1h_atr", over_a(float(w60["high"].max()) - float(w60["low"].min())))
        orh, orl = float(w60["high"].iloc[:15].max()), float(w60["low"].iloc[:15].min())
        side, minute_of = 0.0, None
        for t in range(15, 60):
            bar = w60.iloc[t]
            up, down = bar["high"] > orh, bar["low"] < orl
            if up or down:
                side = (1.0 if bar["close"] >= (orh + orl) / 2 else -1.0) if up and down else (1.0 if up else -1.0)
                minute_of = t
                break
        put("first_break_side_1h", side)                          # +1 above, -1 below, 0 none
        metrics["first_break_minute_1h"] = minute_of
        status["first_break_minute_1h"] = "valid" if minute_of is not None else "not_applicable"

    # --- [09:30, 16:00): full RTH ---------------------------------------------
    if s.is_early_close:
        void(_METRICS_RTH, "not_applicable")
    else:
        minutes = int((s.scheduled_close_at - s.rth_open_at) / ONE_MIN)
        wr = _window(df, s.rth_open_at, minutes)
        if wr is None or O is None:
            void(_METRICS_RTH, "missing_bars")
        elif not A_ok:
            void(_METRICS_RTH, "invalid_reference")
        else:
            H, L, C = float(wr["high"].max()), float(wr["low"].min()), float(wr["close"].iloc[-1])
            put("return_rth_atr", over_a(C - O))
            put("up_excursion_rth_atr", over_a(max(0.0, H - O)))
            put("down_excursion_rth_atr", over_a(max(0.0, O - L)))
            put("range_rth_atr", over_a(H - L))
            put("rth_close_location", ratio(C - L, H - L))
            put("efficiency_rth_5m", path_efficiency([O] + wr["close"].iloc[4::5].tolist()))

    return {"metrics": metrics, "status": status}


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

def _need(m, st, keys):
    """None when every key is measured, else the status explaining the first gap."""
    for k in keys:
        if m.get(k) is None and st.get(k) not in ("not_applicable",):
            return st.get(k) if st.get(k) in LABEL_STATUSES else "missing_bars"
    return None


def label_first_move(m, st):
    bad = _need(m, st, ("open_0930", "first_move_barrier_points"))
    if bad:
        return None, bad
    up, dn = m["first_up_touch_minute"], m["first_down_touch_minute"]
    if up is None and dn is None:
        return "neither", None
    if dn is None or (up is not None and up < dn):
        return "up_first", None
    if up is None or dn < up:
        return "down_first", None
    O, B, first_open = m["open_0930"], m["first_move_barrier_points"], m["first_touch_tie_open"]
    if first_open >= O + B:
        return "up_first", None
    if first_open <= O - B:
        return "down_first", None
    return None, "ambiguous_intrabar"


def label_direction(r, band):
    return "up" if r > band else "down" if r < -band else "flat"


def label_opening_type(m, st, params=None):
    bad = _need(m, st, ("return_15m_atr", "up_excursion_15m_atr", "down_excursion_15m_atr",
                        "range_15m_atr", "efficiency_15m"))
    if bad:
        return None, bad
    p = (params or PARAMETERS)["opening_type_15m"]
    r, u, d = m["return_15m_atr"], m["up_excursion_15m_atr"], m["down_excursion_15m_atr"]
    w, e = m["range_15m_atr"], m["efficiency_15m"]
    reclaim, reject = m.get("on_low_breach_close_reclaim_15m"), m.get("on_high_breach_close_reject_15m")
    return _first_rule([
        ("two_sided", _and(_ge(u, p["two_sided"]["u_min"]), _ge(d, p["two_sided"]["d_min"]))),
        ("sweep_low_rebound", _and(reclaim, _gt(r, p["sweep_low_rebound"]["r_gt"]))),
        ("sweep_high_reverse", _and(reject, _lt(r, p["sweep_high_reverse"]["r_lt"]))),
        ("drive_up", _and(_ge(r, p["drive"]["r_abs_min"]), _le(d, p["drive"]["counter_excursion_max"]),
                          _ge(e, p["drive"]["e_min"]))),
        ("drive_down", _and(_le(r, -p["drive"]["r_abs_min"]), _le(u, p["drive"]["counter_excursion_max"]),
                            _ge(e, p["drive"]["e_min"]))),
        ("range", _and(_le(w, p["range"]["w_max"]), _le(abs(r), p["range"]["r_abs_max"]))),
        ("mixed", True),
    ])


def label_session_type(m, st, params=None):
    bad = _need(m, st, ("return_rth_atr", "up_excursion_rth_atr", "down_excursion_rth_atr", "range_rth_atr",
                        "efficiency_rth_5m", "first_hour_return_atr"))
    if bad:
        return None, bad
    p = (params or PARAMETERS)["session_type_rth"]
    r, u, d, w = m["return_rth_atr"], m["up_excursion_rth_atr"], m["down_excursion_rth_atr"], m["range_rth_atr"]
    q, e, f = m.get("rth_close_location"), m["efficiency_rth_5m"], m["first_hour_return_atr"]
    rv, tr = p["reversal"], p["trend"]
    return _first_rule([
        ("reversal", _or(_and(_ge(f, rv["f_abs_min"]), _le(r, -rv["r_abs_min"])),
                         _and(_le(f, -rv["f_abs_min"]), _ge(r, rv["r_abs_min"])))),
        ("bull_trend", _and(_ge(r, tr["r_abs_min"]), _ge(q, tr["q_bull_min"]), _ge(e, tr["e_min"]))),
        ("bear_trend", _and(_le(r, -tr["r_abs_min"]), _le(q, tr["q_bear_max"]), _ge(e, tr["e_min"]))),
        ("two_sided_volatile", _and(_ge(w, p["two_sided_volatile"]["w_min"]),
                                    _ge(u, p["two_sided_volatile"]["u_min"]),
                                    _ge(d, p["two_sided_volatile"]["d_min"]))),
        ("range", _and(_le(w, p["range"]["w_max"]), _le(abs(r), p["range"]["r_abs_max"]))),
        ("mixed", True),
    ])


def range_reference(history: Optional[Dict[str, List[float]]],
                    params: Optional[Dict[str, Any]] = None) -> Dict[str, Optional[float]]:
    """
    The range-regime thresholds: per RANGE_TARGETS metric, the median of its
    ``median_window`` most recent earlier values (``history[metric]``, newest
    first), or None with fewer.
    """
    window = int((params or PARAMETERS)["range_regime"]["median_window"])
    out: Dict[str, Optional[float]] = {}
    for metric in RANGE_TARGETS.values():
        vals = [v for v in (history or {}).get(metric, []) if v is not None][:window]
        out[metric] = float(pd.Series(vals).median()) if len(vals) == window else None
    return out


def label_range_regime(m, st, metric, reference):
    bad = _need(m, st, (metric,))
    if bad:
        return None, bad
    if reference is None:
        return None, "insufficient_history"
    return ("wide" if m[metric] > reference else "narrow"), None


def compute_labels(m: Dict[str, Any], st: Dict[str, str], s: cal.Session,
                   params: Optional[Dict[str, Any]] = None,
                   reference: Optional[Dict[str, Optional[float]]] = None) -> Dict[str, Dict[str, Any]]:
    """
    {target_id: {'label', 'status', 'window_start_at', 'window_end_at', 'available_at'}}.
    ``reference`` holds the range-regime thresholds (``range_reference``); without
    it the range-regime labels are insufficient_history.
    """
    params = params or PARAMETERS
    reference = reference or {}
    out = {}
    for target, d in TARGETS.items():
        end = (s.rth_open_at + d["window_minutes"] * ONE_MIN if d["window_minutes"]
               else cal.ny_instant(s.session_date, RTH_END))
        if d["window_minutes"] is None and s.is_early_close:
            label, why = None, "shortened_session"
        elif target == "first_move_5m":
            label, why = label_first_move(m, st)
        elif target == "direction_15m":
            bad = _need(m, st, ("return_15m_atr",))
            label, why = (None, bad) if bad else (
                label_direction(m["return_15m_atr"], params["direction_15m_band_atr"]), None)
        elif target == "direction_rth":
            bad = _need(m, st, ("return_rth_atr",))
            label, why = (None, bad) if bad else (
                label_direction(m["return_rth_atr"], params["direction_rth_band_atr"]), None)
        elif target == "direction_1h":
            bad = _need(m, st, ("first_hour_return_atr",))
            label, why = (None, bad) if bad else (
                label_direction(m["first_hour_return_atr"], params["direction_1h_band_atr"]), None)
        elif target == "first_break_1h":
            bad = _need(m, st, ("first_break_side_1h",))
            side = m.get("first_break_side_1h")
            label, why = (None, bad) if bad else (
                ("above" if side > 0 else "below" if side < 0 else "none"), None)
        elif target in RANGE_TARGETS:
            label, why = label_range_regime(m, st, RANGE_TARGETS[target], reference.get(RANGE_TARGETS[target]))
        elif target == "opening_type_15m":
            label, why = label_opening_type(m, st, params)
        else:
            label, why = label_session_type(m, st, params)
        out[target] = {"label": label, "status": "valid" if label is not None else why,
                       "window_start_at": s.rth_open_at, "window_end_at": end,
                       "available_at": min(end, s.scheduled_close_at)}
    return out


def compute_outcome(md, snapshot: Dict[str, Any],
                    history: Optional[Dict[str, List[float]]] = None) -> Dict[str, Any]:
    """
    Metrics and labels for one stored snapshot (a dict from forecast_store).
    ``history`` holds the earlier sessions' range metrics, newest first
    (``forecast_store.trailing_metric_values``), for the range-regime labels.
    Returns {'metrics', 'metric_status', 'available_at', 'digest', 'labels':
    {target_id: {'label', 'status', 'window_start_at', 'window_end_at', 'available_at',
    and for the range-regime targets 'digest' (the session digest plus the threshold)}}}.
    """
    s = cal.session(snapshot["session_date"])
    ref = snapshot["reference_values"]
    A, ONH, ONL = finite(ref.get("A")), finite(ref.get("ONH")), finite(ref.get("ONL"))
    cid = int(snapshot["instrument_id"])
    df, digest, _ = md.bars(cid, s.rth_open_at, s.scheduled_close_at)
    digest = hashlib.sha256(f"{digest}|A={A!r}|ONH={ONH!r}|ONL={ONL!r}".encode()).hexdigest()[:24]
    res = compute_metrics(df, s, A, ONH, ONL)
    reference = range_reference(history)
    labels = compute_labels(res["metrics"], res["status"], s, reference=reference)
    for target, metric in RANGE_TARGETS.items():
        labels[target]["digest"] = hashlib.sha256(
            f"{digest}|{metric}_median={reference[metric]!r}".encode()).hexdigest()[:24]
    return {"metrics": res["metrics"], "metric_status": res["status"], "available_at": s.scheduled_close_at,
            "digest": digest, "labels": labels}


def session_finalised(snapshot: Dict[str, Any], now, settle: timedelta = timedelta(hours=2)) -> bool:
    """True once the RTH session is over and past the collector's revision window
    (bars within 2h of collection are stored as not completed)."""
    s = cal.session(snapshot["session_date"])
    return s.scheduled_close_at is not None and pd.Timestamp(now) >= s.scheduled_close_at + settle


def vocabulary() -> Dict[str, List[str]]:
    return {t: list(d["labels"]) for t, d in TARGETS.items()}
