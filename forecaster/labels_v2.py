# forecaster/labels_v2.py
"""
Realised outcomes for nq_features_v2 snapshots: continuous outcome metrics and
the discrete labels predictions are scored against.

Both are measured from the snapshot's own anchors - P (the 09:28 close) and A
(daily ATR14 through the previous session) as recorded in its
``reference_values`` - over RTH bars of the snapshot's contract, so an outcome
always refers to exactly the information the forecast had.

  METRIC_VERSION  nq_outcome_metrics_v1: returns, ATR-scaled returns, ranges,
                  excursions (MFE/MAE), close location and path efficiency for
                  the first 15/30/60 minutes and the whole RTH session.
  LABEL_VERSION   nq_labels_v1: the targets below. Each target's vocabulary is
                  registered in forecast.label_definitions; the database checks
                  predictions and realised labels against that one row.

Changing a threshold, window or vocabulary means a new version.
"""

import hashlib
import json
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from features import calendar as cal
from features.indicators import finite, path_efficiency, ratio

ONE_MIN = timedelta(minutes=1)

METRIC_VERSION = "nq_outcome_metrics_v1"
LABEL_VERSION = "nq_labels_v1"
DIRECTION_LABELS = ("down", "flat", "up")

# target_id -> (window minutes after 09:30, None = to the scheduled close; flat band in ATR)
TARGETS = {
    "first_hour_direction": {
        "window_minutes": 60,
        "flat_band_atr": 0.10,
        "definition": "Sign of (close of the 10:29 ET bar - P) / A: 'flat' within +/-0.10 ATR. "
                      "Requires the full [09:30, 10:30) window.",
    },
    "session_direction": {
        "window_minutes": None,
        "flat_band_atr": 0.20,
        "definition": "Sign of (close of the last scheduled RTH bar - P) / A: 'flat' within "
                      "+/-0.20 ATR. Requires >= 90% of the RTH bars and the closing minute.",
    },
}

_WINDOWS = {"15m": 15, "30m": 30, "60m": 60, "rth": None}
_RTH_MIN_COVERAGE = 0.9


def label_registry_record() -> Dict[str, Any]:
    targets = [{"target_id": t, "labels": list(DIRECTION_LABELS), "definition": d["definition"],
                "parameters": {"window_minutes": d["window_minutes"], "flat_band_atr": d["flat_band_atr"],
                               "anchor": "P = 09:28 close; A = daily ATR14 (snapshot reference_values)"}}
               for t, d in TARGETS.items()]
    digest = hashlib.sha256(json.dumps(targets, sort_keys=True).encode()).hexdigest()
    return {"label_version": LABEL_VERSION, "definition_hash": digest, "targets": targets,
            "description": "Direction of NQ from P over the first hour and the RTH session, in ATR units."}


def _direction(r: float, band: float) -> str:
    return "up" if r > band else "down" if r < -band else "flat"


def compute_outcome(md, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Metrics and labels for one stored snapshot (a dict from forecast_store).
    Returns {'metrics', 'metric_status', 'available_at', 'digest', 'labels':
    {target_id: (label | None, ineligibility_reason | None, available_at)}}.
    """
    s = cal.session(snapshot["session_date"])
    ref = snapshot["reference_values"]
    P, A = finite(ref.get("P")), finite(ref.get("A"))
    anchors_ok = P is not None and A is not None and A > 0
    cid = int(snapshot["instrument_id"])

    df, digest, _ = md.bars(cid, s.rth_open_at, s.scheduled_close_at)
    digest = hashlib.sha256(f"{digest}|P={P!r}|A={A!r}".encode()).hexdigest()[:24]
    metrics: Dict[str, Any] = {}
    status: Dict[str, str] = {}

    def put(key, value, fail="undefined"):
        v = finite(value)
        metrics[key] = v
        status[key] = "valid" if v is not None else fail

    open_bar = df[df["bar_start_at"] == s.rth_open_at]
    if anchors_ok and not open_bar.empty:
        put("open_gap_atr", (float(open_bar["open"].iloc[0]) - P) / A)
    else:
        put("open_gap_atr", None, "missing")

    complete = {}
    for name, minutes in _WINDOWS.items():
        end = s.scheduled_close_at if minutes is None else min(s.rth_open_at + minutes * ONE_MIN,
                                                                s.scheduled_close_at)
        w = df[df["bar_start_at"] < end]
        expected = int((end - s.rth_open_at) / ONE_MIN)
        last_ok = not w.empty and w["bar_start_at"].iloc[-1] == end - ONE_MIN
        ok = last_ok and (len(w) == expected if minutes is not None
                          else len(w) >= expected * _RTH_MIN_COVERAGE)
        complete[name] = ok
        metrics[f"bars_{name}"] = int(len(w))
        status[f"bars_{name}"] = "valid"
        keys = ("ret", "ret_atr", "mfe_atr", "mae_atr", "range_atr", "close_location", "efficiency")
        if not ok or not anchors_ok:
            for k in keys:
                put(f"{k}_{name}", None, "missing")
            continue
        hi, lo, close = float(w["high"].max()), float(w["low"].min()), float(w["close"].iloc[-1])
        put(f"ret_{name}", close / P - 1)
        put(f"ret_atr_{name}", (close - P) / A)
        put(f"mfe_atr_{name}", (hi - P) / A)
        put(f"mae_atr_{name}", (lo - P) / A)
        put(f"range_atr_{name}", (hi - lo) / A)
        put(f"close_location_{name}", ratio(close - lo, hi - lo))
        put(f"efficiency_{name}", path_efficiency([P] + w["close"].tolist()))
        if name == "rth":
            put("rth_high_minute", (w.loc[w["high"].idxmax(), "bar_start_at"] - s.rth_open_at) / ONE_MIN)
            put("rth_low_minute", (w.loc[w["low"].idxmin(), "bar_start_at"] - s.rth_open_at) / ONE_MIN)
    if "rth_high_minute" not in metrics:
        put("rth_high_minute", None, "missing")
        put("rth_low_minute", None, "missing")

    labels = {}
    for target, d in TARGETS.items():
        window = "rth" if d["window_minutes"] is None else f"{d['window_minutes']}m"
        end = (s.scheduled_close_at if d["window_minutes"] is None
               else min(s.rth_open_at + d["window_minutes"] * ONE_MIN, s.scheduled_close_at))
        if not anchors_ok:
            labels[target] = (None, "anchor_missing", end)
        elif not complete[window]:
            labels[target] = (None, "window_incomplete", end)
        else:
            labels[target] = (_direction(metrics[f"ret_atr_{window}"], d["flat_band_atr"]), None, end)

    return {"metrics": metrics, "metric_status": status, "available_at": s.scheduled_close_at,
            "digest": digest, "labels": labels}


def session_finalised(snapshot: Dict[str, Any], now, settle: timedelta = timedelta(hours=2)) -> bool:
    """True once the RTH session is over and past the collector's revision window
    (bars within 2h of collection are stored as not completed)."""
    s = cal.session(snapshot["session_date"])
    return s.scheduled_close_at is not None and pd.Timestamp(now) >= s.scheduled_close_at + settle
