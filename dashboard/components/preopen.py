# dashboard/components/preopen.py
"""
The pre-open chart of one stored snapshot, for the Forecast page: the session's
overnight on 2-minute bars from 18:00 to the cutoff, with the frozen references
(previous RTH high / low / close, ON high / low) and the three moving averages
computed as the structure annotation computes them (on the 2m bars, seeded at
18:00). Also the names of P1's price-location levels, as the pages show them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

import pandas as pd

from dashboard.components.spec import build_chart_spec
from features.calculations import calculate_moving_averages, enrich_candle_timezones

_PREV_RTH = "#29b6f6"
# The price-location levels as the pages name them, in P1's order.
LEVEL_LABELS = {"prev_rth_high": "Prev RTH High", "prev_rth_low": "Prev RTH Low", "prev_rth_close": "Prev RTH Close",
                "on_high": "ON High", "on_low": "ON Low"}
_EMPTY = {"candles": [], "volume": [], "series": {}, "bands": {}, "legend": []}


def _ref(payload: Dict[str, Any], name: str) -> Optional[float]:
    ref = (payload.get("references") or {}).get(name) or {}
    value = ref.get("value") if ref.get("status") == "valid" else None
    return None if value is None else float(Decimal(str(value)))


def frozen_preopen_spec(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    The pre-open chart drawn only from the snapshot's own frozen evidence - its archived 2m buckets from 18:00 to
    the cutoff, the moving averages computed on exactly those buckets (as the structure annotation and the frozen
    Long MA are), and its frozen levels - never from the mutable bars table: the chart behind an issued forecast
    (guideline revision 2, 3F). A bucket missing a minute (nq_conv_v5) is drawn grey.
    """
    p = snapshot["payload"]
    on_start = p["schedule"]["overnight_start_at"]
    rows = [r for r in (p.get("bars") or {}).get("2m") or [] if r[0] >= on_start]
    if not rows:
        return dict(_EMPTY)
    df = pd.DataFrame([{"timestamp_utc": r[0][:19].replace("T", " "), "open": float(r[1]), "high": float(r[2]),
                        "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                        "muted": len(r) > 7 and not r[7]} for r in rows])
    bars = calculate_moving_averages(enrich_candle_timezones(df))
    levels = {"previous_rth_close": _ref(p, "prev_rth_close"), "overnight_high": _ref(p, "on_high"),
              "overnight_low": _ref(p, "on_low")}
    candidates = (p.get("first_level_candidates") or {}).get("levels") or {}

    def frozen(name):
        c = candidates.get(name) or {}
        return float(Decimal(str(c["value"]))) if c.get("status") == "valid" and c.get("value") is not None else None
    extra = [{"key": "prev_rth_high", "label": "Prev RTH High", "value": _ref(p, "prev_rth_high"), "color": _PREV_RTH,
              "dash": 2},
             {"key": "prev_rth_low", "label": "Prev RTH Low", "value": _ref(p, "prev_rth_low"), "color": _PREV_RTH,
              "dash": 2},
             {"key": "premarket_high", "label": "Premarket High", "value": frozen("premarket_high"),
              "color": "#ab47bc", "dash": 1},
             {"key": "premarket_low", "label": "Premarket Low", "value": frozen("premarket_low"), "color": "#ab47bc",
              "dash": 1},
             {"key": "vwap_frozen", "label": "VWAP (frozen)", "value": frozen("vwap"), "color": "#fdd835", "dash": 1}]
    return build_chart_spec(bars, levels=levels, extra_levels=extra, show_vwap=False, fit=True)
