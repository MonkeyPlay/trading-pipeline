# forecaster/labels_v2.py
"""
Realised outcomes for nq_features_v2 snapshots: the continuous outcome metrics
and the deterministic labels predictions are scored against (nq_schema_v2,
sections 8-11).

Everything is measured from NQ one-minute RTH bars of the snapshot's own
contract. O is the 09:30 bar's open; C5, C15 and C are the 09:34, 09:44 and
15:59 closes; every normalised value divides by the snapshot's frozen A (daily
ATR14 through the previous session), and the opening labels use the snapshot's
frozen overnight extremes - post-open bars never redefine them.

  METRIC_VERSION  nq_outcome_metrics_v2: section 10.
  LABEL_VERSION   nq_labels_v2_candidate: the five targets of section 8 with the
                  starting thresholds of section 11. Each target's vocabulary is
                  registered in forecast.label_definitions; the database checks
                  predictions and realised labels against that one row.

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

METRIC_VERSION = "nq_outcome_metrics_v2"
LABEL_VERSION = "nq_labels_v2_candidate"

LABEL_STATUSES = ("valid", "missing_bars", "ambiguous_intrabar", "incomplete_window",
                  "shortened_session", "not_yet_available", "invalid_reference")

# Section 11 starting configuration. Not empirically established optima: tune on
# training/development periods only, and issue a new LABEL_VERSION after a change.
PARAMETERS = {
    "first_move_barrier": "B = max(1.0 point, 0.05 * A); barriers O + B and O - B",
    "first_move_min_points": 1.0,
    "first_move_atr_fraction": 0.05,
    "direction_15m_band_atr": 0.10,
    "direction_rth_band_atr": 0.20,
    "on_breach_atr": 0.02,
    "opening_type_15m": {
        "two_sided": {"u_min": 0.15, "d_min": 0.15},
        "sweep_low_rebound": {"r_gt": 0.10},
        "sweep_high_reverse": {"r_lt": -0.10},
        "drive": {"r_abs_min": 0.20, "counter_excursion_max": 0.05, "e_min": 0.50},
        "range": {"w_max": 0.20, "r_abs_max": 0.05},
    },
    "session_type_rth": {
        "reversal": {"f_abs_min": 0.20, "r_abs_min": 0.20},
        "trend": {"r_abs_min": 0.50, "q_bull_min": 0.80, "q_bear_max": 0.20, "e_min": 0.30},
        "two_sided_volatile": {"w_min": 1.00, "u_min": 0.30, "d_min": 0.30},
        "range": {"w_max": 0.60, "r_abs_max": 0.20},
    },
    "window_coverage": "every one-minute bar of the window is required",
    "early_close": "full-RTH targets are ineligible (shortened_session); opening targets stay eligible",
}

TARGETS: Dict[str, Dict[str, Any]] = {
    "first_move_5m": {
        "labels": ("up_first", "down_first", "neither"),
        "window_minutes": 5,
        "definition": "Which barrier O +/- B (B = max(1 point, 0.05 A)) is touched first in [09:30, 09:35), "
                      "by first-touch minute index. Both first touched in the same minute: that minute's "
                      "open at/above the upper barrier -> up_first, at/below the lower -> down_first, "
                      "otherwise ineligible (ambiguous_intrabar).",
    },
    "opening_type_15m": {
        "labels": ("drive_up", "drive_down", "sweep_low_rebound", "sweep_high_reverse",
                   "two_sided", "range", "mixed"),
        "window_minutes": 15,
        "definition": "First matching rule over [09:30, 09:45), with r, u, d, w, e the 15m return, up/down "
                      "excursion, range (all / A) and efficiency: two_sided u>=0.15 and d>=0.15; "
                      "sweep_low_rebound ON-low breach-and-close-reclaim and r>0.10; sweep_high_reverse "
                      "ON-high breach-and-close-reject and r<-0.10; drive_up r>=0.20, d<=0.05, e>=0.50; "
                      "drive_down r<=-0.20, u<=0.05, e>=0.50; range w<=0.20 and |r|<=0.05; else mixed. "
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
                      "return: reversal f>=0.20 and r<=-0.20, or f<=-0.20 and r>=0.20; bull_trend r>=0.50, "
                      "q>=0.80, e>=0.30; bear_trend r<=-0.50, q<=0.20, e>=0.30; two_sided_volatile w>=1.00, "
                      "u>=0.30, d>=0.30; range w<=0.60 and |r|<=0.20; else mixed. Ineligible on early-close "
                      "sessions.",
    },
}

# Metric groups by the window they need; a group's metrics are all null when its
# window is incomplete.
_METRICS_5M = ("return_5m_atr", "first_up_touch_minute", "first_down_touch_minute", "first_move_barrier_points",
               "first_touch_tie_open")
_METRICS_15M = ("return_15m_atr", "up_excursion_15m_atr", "down_excursion_15m_atr", "range_15m_atr",
                "efficiency_15m", "on_low_breach_close_reclaim_15m", "on_high_breach_close_reject_15m")
_METRICS_60M = ("first_hour_return_atr",)
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
            "description": "nq_schema_v2 candidate labels: first move, opening type, 15m and RTH direction, "
                           "RTH session type (deterministic rules over nq_outcome_metrics_v2)."}


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
                    ONL: Optional[float]) -> Dict[str, Any]:
    """
    Section 10 metrics from RTH bars ``df`` (bar_start_at UTC, open/high/low/close).
    Returns {'metrics': {...}, 'status': {...}}; every metric is present, null
    with a status when it cannot be measured.
    """
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
        B = max(PARAMETERS["first_move_min_points"], PARAMETERS["first_move_atr_fraction"] * A)
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
        breach = PARAMETERS["on_breach_atr"] * A
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


def label_opening_type(m, st):
    bad = _need(m, st, ("return_15m_atr", "up_excursion_15m_atr", "down_excursion_15m_atr",
                        "range_15m_atr", "efficiency_15m"))
    if bad:
        return None, bad
    p = PARAMETERS["opening_type_15m"]
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


def label_session_type(m, st):
    bad = _need(m, st, ("return_rth_atr", "up_excursion_rth_atr", "down_excursion_rth_atr", "range_rth_atr",
                        "efficiency_rth_5m", "first_hour_return_atr"))
    if bad:
        return None, bad
    p = PARAMETERS["session_type_rth"]
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


def compute_labels(m: Dict[str, Any], st: Dict[str, str], s: cal.Session) -> Dict[str, Dict[str, Any]]:
    """{target_id: {'label', 'status', 'window_start_at', 'window_end_at', 'available_at'}}."""
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
                label_direction(m["return_15m_atr"], PARAMETERS["direction_15m_band_atr"]), None)
        elif target == "direction_rth":
            bad = _need(m, st, ("return_rth_atr",))
            label, why = (None, bad) if bad else (
                label_direction(m["return_rth_atr"], PARAMETERS["direction_rth_band_atr"]), None)
        elif target == "opening_type_15m":
            label, why = label_opening_type(m, st)
        else:
            label, why = label_session_type(m, st)
        out[target] = {"label": label, "status": "valid" if label is not None else why,
                       "window_start_at": s.rth_open_at, "window_end_at": end,
                       "available_at": min(end, s.scheduled_close_at)}
    return out


def compute_outcome(md, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Metrics and labels for one stored snapshot (a dict from forecast_store).
    Returns {'metrics', 'metric_status', 'available_at', 'digest', 'labels':
    {target_id: {'label', 'status', 'window_start_at', 'window_end_at', 'available_at'}}}.
    """
    s = cal.session(snapshot["session_date"])
    ref = snapshot["reference_values"]
    A, ONH, ONL = finite(ref.get("A")), finite(ref.get("ONH")), finite(ref.get("ONL"))
    cid = int(snapshot["instrument_id"])
    df, digest, _ = md.bars(cid, s.rth_open_at, s.scheduled_close_at)
    digest = hashlib.sha256(f"{digest}|A={A!r}|ONH={ONH!r}|ONL={ONL!r}".encode()).hexdigest()[:24]
    res = compute_metrics(df, s, A, ONH, ONL)
    return {"metrics": res["metrics"], "metric_status": res["status"], "available_at": s.scheduled_close_at,
            "digest": digest, "labels": compute_labels(res["metrics"], res["status"], s)}


def session_finalised(snapshot: Dict[str, Any], now, settle: timedelta = timedelta(hours=2)) -> bool:
    """True once the RTH session is over and past the collector's revision window
    (bars within 2h of collection are stored as not completed)."""
    s = cal.session(snapshot["session_date"])
    return s.scheduled_close_at is not None and pd.Timestamp(now) >= s.scheduled_close_at + settle


def vocabulary() -> Dict[str, List[str]]:
    return {t: list(d["labels"]) for t, d in TARGETS.items()}
