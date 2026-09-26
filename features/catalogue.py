# features/catalogue.py
"""
The nq_features_v2 catalogue: every predictive feature, its type, unit, hard
bounds and definition, plus the parameters the version fixes.

A feature version is a contract. Changing a definition, a window, a warm-up, a
roll or session policy, a coverage threshold or a source choice means a new
``FEATURE_VERSION`` - never an edit to this one. ``definition_hash()`` is stored
with the version in ``forecast.feature_versions``, and registering a changed
catalogue under the same name is refused.

Each model version picks a documented subset of these features
(``forecaster/models_v2.py``); ``DEFAULT_REQUIRED`` is the subset that governs a
snapshot's own ``data_quality_status``.
"""

import hashlib
import json
import logging
import math
from dataclasses import asdict, dataclass
from typing import Dict, Optional, Tuple

from config import ASSET_SOURCES, INSTRUMENTS
from features.calendar import CALENDAR_VERSION

logger = logging.getLogger(__name__)

FEATURE_VERSION = "nq_features_v2"
TARGET_SYMBOL = "NQ"

FEATURE_STATUSES = ("valid", "missing", "stale", "insufficient_history", "undefined", "not_applicable")
DATA_MODES = ("live_capture", "historical_reconstruction")
QUALITY_STATUSES = ("valid", "partial", "invalid")

PARAMETERS = {
    "target_symbol": TARGET_SYMBOL,
    "cutoff_et": "09:29",
    "latest_input_bar_start_et": "09:28",
    "calendar_version": CALENDAR_VERSION,
    "bar_label": "start",
    "roll_policy": "active_contract_same_contract_reference_v1",
    "roll_policy_detail": (
        "Each session uses the contract active_contracts assigns it (fallback: the configured "
        "contract). Cross-session references (Cprev, reference closes, the previous close in a "
        "daily true range) come from that same contract. Intraday indicator series (5m/15m EMA, "
        "1m ATR) use only the snapshot contract's bars; nothing is spliced or back-adjusted."
    ),
    "warmup_multiple": 5,
    "recursive_window": "fixed trailing window of warmup_multiple * n inputs, seeded with the first n",
    "daily_atr_periods": [14, 63],
    "intraday_atr_period": 14,
    "aggregation": "fixed ET clock boundaries; only buckets with every constituent minute feed indicators",
    "overnight_window": "[18:00 ET previous calendar day, T)",
    "overnight_min_coverage": 0.9,
    "window_60m": "[08:29, 09:29) ET, all 60 bars required",
    "rth_daily_min_coverage": 0.9,
    "rvol_baseline_sessions": 30,
    "atr_1m_relative_baseline_sessions": 30,
    "divergence_baseline_sessions": 60,
    "baseline_search_sessions": 90,
    "full_baseline_required": True,
    "asset_sources": {a: {"symbol": s.symbol, "max_age_minutes": s.max_age_minutes,
                          "is_proxy": s.is_proxy}
                      for a, s in sorted(ASSET_SOURCES.items())},
    # Units of the instruments the sources above read (not every configured one).
    "asset_units": {sym: {"value_kind": i.value_kind, "value_unit": i.value_unit,
                          "bps_per_unit": i.bps_per_unit, "what_to_show": i.what_to_show}
                    for sym, i in sorted(INSTRUMENTS.items())
                    if any(s.symbol == sym for s in ASSET_SOURCES.values())},
}


@dataclass(frozen=True)
class FeatureDef:
    name: str
    family: str                                  # nq | intermarket | intermarket_optional | calendar | event
    data_type: str                               # float | integer | boolean | categorical
    unit: Optional[str]
    definition: str
    lower: Optional[float] = None                # hard mathematical bounds, inclusive
    upper: Optional[float] = None
    allowed: Optional[Tuple[str, ...]] = None
    default_required: bool = False


def _f(name, unit, definition, lower=None, upper=None, family="nq", required=False):
    return FeatureDef(name, family, "float", unit, definition, lower, upper, None, required)


_EPS = 0.0  # bounds are inclusive; ">0" features are checked separately below

CATALOGUE: Tuple[FeatureDef, ...] = (
    # --- NQ -------------------------------------------------------------------
    _f("daily_atr_fraction", "ratio", "A / Cprev; A = Wilder ATR14 of RTH sessions through the previous one; > 0",
       lower=_EPS, required=True),
    _f("daily_volatility_ratio", "ratio", "ATR14 / ATR63, both through the previous completed RTH session; > 0",
       lower=_EPS),
    _f("atr_1m_14_fraction", "ratio", "Wilder ATR14 of 1m bars through the 09:28 bar, divided by P; >= 0",
       lower=0.0),
    _f("atr_1m_14_relative_30d", "ratio",
       "Current 1m ATR14 / mean same-cutoff 1m ATR14 over the previous 30 eligible sessions", lower=0.0),
    _f("gap_signed_atr", "atr", "(P - Cprev) / A", required=True),
    _f("prior_range_position", "ratio", "(P - PDL) / (PDH - PDL); < 0 and > 1 are valid", required=True),
    _f("distance_pdh_atr", "atr", "(P - PDH) / A", required=True),
    _f("distance_pdl_atr", "atr", "(P - PDL) / A", required=True),
    _f("distance_onh_atr", "atr", "(P - ONH) / A; <= 0 with consistent observations", required=True),
    _f("distance_onl_atr", "atr", "(P - ONL) / A; >= 0 with consistent observations", required=True),
    _f("overnight_range_atr", "atr", "(ONH - ONL) / A; >= 0", lower=0.0, required=True),
    _f("overnight_range_position", "ratio", "(P - ONL) / (ONH - ONL); 0-1", lower=0.0, upper=1.0),
    _f("distance_on_vwap_hlc3_atr", "atr",
       "(P - volume-weighted HLC3 over ON) / A; a minute-bar VWAP approximation"),
    _f("return_15m_atr", "atr", "(09:28 close - 09:13 close) / A", required=True),
    _f("return_60m_atr", "atr", "(09:28 close - 08:28 close) / A", required=True),
    _f("range_60m_atr", "atr", "(high - low over [08:29, 09:29)) / A; >= 0", lower=0.0, required=True),
    _f("efficiency_60m", "ratio", "|c60 - c0| / sum |ci - ci-1| from the 08:28 close over 60 closes; 0-1",
       lower=0.0, upper=1.0),
    _f("ema200_distance_5m_atr", "atr", "(P - latest completed 5m EMA200) / A"),
    _f("ema9_21_spread_5m_atr", "atr", "(5m EMA9 - 5m EMA21) / A"),
    _f("ema20_slope_15m_atr", "atr", "(15m EMA20 at the 09:15 endpoint - at the 08:15 endpoint) / A"),
    _f("rvol_overnight_30d", "ratio", "ON volume / previous-30-session mean ON volume; >= 0", lower=0.0),
    _f("rvol_60m_30d", "ratio", "Last-60m volume / previous-30-session mean for the identical window; >= 0",
       lower=0.0),
    _f("prior_rth_return_atr", "atr", "(Cprev - prior RTH open) / A"),
    _f("prior_rth_range_atr", "atr", "(PDH - PDL) / A; >= 0", lower=0.0),
    _f("prior_rth_close_location", "ratio", "(Cprev - PDL) / (PDH - PDL); 0-1", lower=0.0, upper=1.0),

    # --- Intermarket -------------------------------------------------------------
    _f("nq_preopen_return", "decimal_return", "P / Cprev - 1", family="intermarket", required=True),
    _f("es_preopen_return", "decimal_return", "ES latest eligible close / reference close - 1",
       family="intermarket"),
    _f("rty_preopen_return", "decimal_return", "RTY latest eligible close / reference close - 1",
       family="intermarket"),
    _f("nq_es_relative_return", "decimal_return", "nq_preopen_return - es_preopen_return",
       family="intermarket"),
    _f("nq_es_standardized_divergence", "z", "z(NQ) - z(ES) against each asset's prior 60 same-window "
       "returns; sample SD; zero SD -> null", family="intermarket"),
    _f("nq_es_relative_return_60m", "decimal_return",
       "NQ 09:28/08:28 - 1 minus ES 09:28/08:28 - 1", family="intermarket"),
    _f("vix_level", "index_points", "Latest eligible spot VIX; > 0", lower=_EPS, family="intermarket"),
    _f("vix_change_points", "index_points", "VIX minus its reference value", family="intermarket"),
    _f("vxn_level", "index_points", "Latest eligible spot VXN; > 0", lower=_EPS, family="intermarket"),
    _f("vxn_change_points", "index_points", "VXN minus its reference value", family="intermarket"),
    _f("us10y_change_bps", "bps", "Spot 10-year yield change (TNX)", family="intermarket"),
    _f("us2y_change_bps", "bps", "Spot 2-year yield change (unmapped: always null)", family="intermarket"),
    _f("yield_curve_10y_2y_change_bps", "bps", "us10y_change_bps - us2y_change_bps", family="intermarket"),
    _f("dxy_preopen_return", "decimal_return", "Cash DXY return (unmapped: always null; see dx_fut)",
       family="intermarket"),
    _f("smh_preopen_return", "decimal_return", "SMH previous regular close -> latest eligible premarket print",
       family="intermarket"),
    _f("dx_fut_preopen_return", "decimal_return", "ICE US Dollar Index future (a DXY proxy)",
       family="intermarket_optional"),
    _f("us10y_yield_fut_change_bps", "bps", "Micro 10Y yield future change (not the spot yield)",
       family="intermarket_optional"),
    _f("us2y_yield_fut_change_bps", "bps", "Micro 2Y yield future change (not the spot yield)",
       family="intermarket_optional"),
    _f("yield_fut_curve_10y_2y_change_bps", "bps",
       "us10y_yield_fut_change_bps - us2y_yield_fut_change_bps", family="intermarket_optional"),
    _f("gc_preopen_return", "decimal_return", "Gold future return (optional source)",
       family="intermarket_optional"),
    _f("cl_preopen_return", "decimal_return", "WTI crude future return (optional source)",
       family="intermarket_optional"),

    # --- Calendar and events -------------------------------------------------------
    FeatureDef("weekday", "calendar", "categorical", None, "ET session weekday",
               allowed=("mon", "tue", "wed", "thu", "fri"), default_required=True),
    FeatureDef("monthly_opex_week", "calendar", "boolean", None,
               "Week containing the designated monthly equity/index options expiration"),
    FeatureDef("days_to_nq_expiry", "calendar", "integer", "days",
               "Calendar days from the session to the selected contract's last trading day; >= 0",
               lower=0.0),
    FeatureDef("roll_transition", "calendar", "boolean", None,
               "Calendar-defined roll date and two scheduled sessions either side"),
    FeatureDef("is_early_close", "calendar", "boolean", None,
               "Scheduled RTH close before 16:00 ET", default_required=True),
    FeatureDef("remaining_event_risk", "event", "categorical", None,
               "Highest scheduled event tier in (T, scheduled close); null without a valid calendar",
               allowed=("none", "moderate", "high")),
    FeatureDef("has_future_high_event", "event", "boolean", None,
               "A high-tier event in (T, scheduled close); null without a valid calendar"),
    FeatureDef("minutes_to_high_event", "event", "float", "minutes",
               "Minutes from T to the next high-tier event before the close; not_applicable when none",
               lower=0.0),
)

BY_NAME: Dict[str, FeatureDef] = {f.name: f for f in CATALOGUE}
FEATURE_NAMES = tuple(f.name for f in CATALOGUE)
DEFAULT_REQUIRED = tuple(f.name for f in CATALOGUE if f.default_required)

# Features whose ">0" constraint is strict (the others' lower bounds are inclusive).
_STRICTLY_POSITIVE = {"daily_atr_fraction", "daily_volatility_ratio", "vix_level", "vxn_level"}


def check_value(name: str, value):
    """
    ``(value, None)`` if ``value`` satisfies the catalogue, else ``(None, reason)``.
    Floats must be finite; bounds are the hard mathematical ones.
    """
    d = BY_NAME[name]
    if value is None:
        return None, None
    if d.data_type == "boolean":
        return (bool(value), None) if isinstance(value, bool) else (None, "not a boolean")
    if d.data_type == "categorical":
        return (value, None) if value in d.allowed else (None, f"{value!r} not in {d.allowed}")
    if d.data_type == "integer":
        if isinstance(value, bool) or not float(value).is_integer():
            return None, "not an integer"
        value = int(value)
    else:
        value = float(value)
        if not math.isfinite(value):
            return None, "not finite"
    if d.lower is not None and (value < d.lower or (name in _STRICTLY_POSITIVE and value <= 0)):
        return None, f"{value} below bound {d.lower}"
    if d.upper is not None and value > d.upper:
        return None, f"{value} above bound {d.upper}"
    return value, None


def quality_status(feature_status: Dict[str, str], required) -> str:
    """invalid: a required feature is not valid; partial: a non-required one is
    not valid (not_applicable counts as valid); valid: everything is valid."""
    ok = ("valid", "not_applicable")
    if any(feature_status.get(n) not in ok for n in required):
        return "invalid"
    if any(s not in ok for s in feature_status.values()):
        return "partial"
    return "valid"


def definition_hash() -> str:
    payload = {"feature_version": FEATURE_VERSION, "parameters": PARAMETERS,
               "features": [asdict(f) for f in CATALOGUE]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=list).encode()).hexdigest()


def registry_record() -> dict:
    """What database.forecast_store.register_feature_version stores."""
    return {
        "feature_version": FEATURE_VERSION,
        "definition_hash": definition_hash(),
        "parameters": PARAMETERS,
        "description": "NQ pre-open snapshot at T = 09:29 ET: NQ price/volume, intermarket and "
                       "calendar/event features (docs/forecast_contract_v2.md).",
        "definitions": [
            {"feature_name": f.name, "family": f.family, "data_type": f.data_type, "unit": f.unit,
             "allowed_values": list(f.allowed) if f.allowed else None, "lower_bound": f.lower,
             "upper_bound": f.upper, "default_required": f.default_required,
             "definition": f.definition}
            for f in CATALOGUE
        ],
    }
