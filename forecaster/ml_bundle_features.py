# forecaster/ml_bundle_features.py
"""
The seven-target bundles' features (contracts/nq_ml_bundle.FEATURE_GROUPS, nq_ml_features_v2), for
one instrument's session, frozen before its outcome.

  snapshot_features(payload)     from the snapshot frozen at the cutoff only: level geometry (every
                                 candidate's signed distance from the cutoff price in two-minute-ATR
                                 units, the nearest above / below, how many within T, how many
                                 coincident), known volatility (two-minute against daily ATR, the
                                 overnight and premarket ranges in daily ATRs), the path extras from
                                 the archived overnight bars (efficiency overnight and premarket, the
                                 last 30 minutes in two-minute ATRs and against the premarket direction)
                                 and the calendar timing from the frozen scheduled events
  candidate_table(payload)       first level: per frozen candidate its geometry, and whether the label
                                 contract makes it impossible (it shares its price with a candidate
                                 earlier in FIRST_LEVEL_PRECEDENCE, which would name the level)
  row(fs, day, symbol, payload, ...)   one session's feature row: the v1 own features (normalised by the
                                 instrument's earlier sessions, forecaster/ml_features.py), the snapshot
                                 features, the calendar, and for NQ the context group and data ages

Everything reads values that existed at the cutoff: the snapshot excludes every bar after it, the v1
features read only earlier sessions and bars ended by the cutoff (and received by the issue time when
given). Event features use scheduled times and tiers only - never an actual, a surprise or a revision.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from contracts import nq_ml as ml
from contracts import nq_ml_bundle as mb
from contracts import nq_prompt_v2 as defs
from features import calendar as cal

NAN = float("nan")
CAND_FEATURES = ("dist_atr", "abs_dist_atr", "above", "nearest_side", "rank_side", "coincident")


def _num(v) -> Optional[float]:
    """A stored number (int, float, Decimal, 'n/d' or decimal string) as a float; None when absent."""
    if v is None:
        return None
    if isinstance(v, str):
        return float(Fraction(v)) if "/" in v else float(Decimal(v))
    return float(v)


def _exact(level: Dict[str, Any]) -> Optional[Fraction]:
    if level.get("status") != "valid" or level.get("value") is None:
        return None
    return Fraction(level["exact"]) if level.get("exact") else Fraction(Decimal(str(level["value"])))


def _utc(text) -> datetime:
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def levels(payload: Dict[str, Any]) -> Dict[str, Optional[Fraction]]:
    """The frozen first-level candidates as exact prices (None: unavailable)."""
    frozen = ((payload.get("first_level_candidates") or {}).get("levels")) or {}
    return {c: _exact(frozen.get(c) or {}) for c in mb.CANDIDATES}


def possible(prices: Dict[str, Optional[Fraction]]) -> Dict[str, bool]:
    """Per candidate, whether the label contract allows it to be the first level: a valid candidate that is the first in
    FIRST_LEVEL_PRECEDENCE among those at its price (the others at that price can never be named)."""
    out = {}
    for c in mb.CANDIDATES:
        p = prices[c]
        if p is None:
            out[c] = False
            continue
        same = [x for x in defs.FIRST_LEVEL_PRECEDENCE if prices.get(x) == p]
        out[c] = same[0] == c
    return out


def _cutoff_price(payload) -> Optional[Fraction]:
    ref = (payload.get("references") or {}).get("cutoff_price") or {}
    return None if ref.get("status") != "valid" or ref.get("value") is None else Fraction(Decimal(str(ref["value"])))


def _atr2(payload) -> Optional[float]:
    two = ((payload.get("atr") or {}).get("two_minute")) or {}
    v = _num(two.get("exact") or two.get("value"))
    return v if v and v > 0 else None


def _atr_daily(payload) -> Optional[float]:
    d = ((payload.get("atr") or {}).get("daily")) or {}
    v = _num(d.get("exact") or d.get("value"))
    return v if v and v > 0 else None


def candidate_table(payload: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """Per frozen candidate: CAND_FEATURES (NaN when it or the cutoff price / two-minute ATR is unavailable), ``valid``
    and ``possible`` (see the module docstring)."""
    prices = levels(payload)
    cp, atr = _cutoff_price(payload), _atr2(payload)
    ok = possible(prices)
    dist = {c: (float(prices[c] - cp) / atr if prices[c] is not None and cp is not None and atr else None)
            for c in mb.CANDIDATES}
    out = {}
    for c in mb.CANDIDATES:
        d = dist[c]
        row = {k: NAN for k in CAND_FEATURES}
        row["valid"] = float(prices[c] is not None)
        row["possible"] = float(ok[c])
        if d is not None:
            side = [x for x in mb.CANDIDATES if dist[x] is not None and (dist[x] > 0) == (d > 0) and dist[x] != 0]
            ranked = sorted({abs(dist[x]) for x in side})
            row.update(dist_atr=d, abs_dist_atr=abs(d), above=float(d > 0),
                       nearest_side=float(bool(ranked) and abs(d) == ranked[0]),
                       rank_side=float(ranked.index(abs(d)) + 1) if abs(d) in ranked else 0.0,
                       coincident=float(sum(1 for x in mb.CANDIDATES if prices[x] is not None
                                            and prices[x] == prices[c]) - 1))
        out[c] = row
    return out


def _bars(payload) -> List[list]:
    return ((payload.get("bars") or {}).get("1m")) or []


def _efficiency(closes: Sequence[float]) -> float:
    if len(closes) < 2:
        return NAN
    path = float(np.sum(np.abs(np.diff(closes))))
    return NAN if path <= 0 else abs(closes[-1] - closes[0]) / path


def _events(payload, session: cal.Session, cutoff: datetime) -> Dict[str, float]:
    ev = ((payload.get("events") or {}).get("events")) or []
    high = sorted(_utc(e["scheduled_at"]) for e in ev if e.get("tier") == "high" and e.get("scheduled_at"))
    after = [t for t in high if cutoff < t < session.scheduled_close_at]
    nxt = (after[0] - cutoff).total_seconds() / 60 if after else float(mb.EVENT_CAP_MINUTES)
    first30 = session.rth_open_at + timedelta(minutes=30)
    covered = ((payload.get("events") or {}).get("covered_sources"))
    out = {"ev_minutes_to_next": min(nxt, float(mb.EVENT_CAP_MINUTES)),
           "ev_first_30": float(any(session.rth_open_at <= t < first30 for t in high)),
           "ev_rth": float(any(session.rth_open_at <= t < session.scheduled_close_at for t in high))}
    if covered is not None and not covered:          # no coverage row vouches for the day: unknown, not 'no event'
        out = {k: NAN for k in out}
    return out


def snapshot_features(payload: Dict[str, Any]) -> Dict[str, float]:
    """The snapshot-only groups (see the module docstring); NaN where an input is unavailable."""
    out: Dict[str, float] = {}
    cp, atr2, atrd = _cutoff_price(payload), _atr2(payload), _atr_daily(payload)
    table = candidate_table(payload)
    for c in mb.CANDIDATES:
        out[f"geo_{c}"] = table[c]["dist_atr"]
    d = [table[c]["dist_atr"] for c in mb.CANDIDATES if not math.isnan(table[c]["dist_atr"])]
    up, down = [x for x in d if x > 0], [x for x in d if x < 0]
    t = _num((payload.get("thresholds") or {}).get("T"))
    out["near_up_atr"] = min(up) if up else NAN
    out["near_down_atr"] = max(down) if down else NAN
    out["n_within_t"] = (float(sum(1 for x in d if atr2 and abs(x) * atr2 <= t)) if t is not None and atr2 and cp
                         is not None else NAN)
    out["n_coincident"] = float(sum(1 for c in mb.CANDIDATES if table[c]["valid"] and table[c]["coincident"] > 0)) \
        if cp is not None else NAN
    refs = payload.get("references") or {}
    rng = lambda hi, lo: (_num(refs.get(hi, {}).get("value")), _num(refs.get(lo, {}).get("value")))  # noqa: E731
    on_h, on_l = rng("on_high", "on_low")
    pm_h, pm_l = rng("premarket_high", "premarket_low")
    out["atr_ratio"] = math.log(atr2 / atrd) if atr2 and atrd else NAN
    out["on_range_atr"] = (on_h - on_l) / atrd if on_h is not None and on_l is not None and atrd else NAN
    out["pm_range_atr"] = (pm_h - pm_l) / atrd if pm_h is not None and pm_l is not None and atrd else NAN
    # path extras from the archived overnight bars (all ended by the cutoff)
    bars = _bars(payload)
    session = cal.session(payload["identity"]["session_date"])
    cutoff = _utc(payload["cutoff"]["input_cutoff_at"])
    times = [_utc(b[0]) for b in bars]
    closes = [float(b[4]) for b in bars]
    pm_start = cal.ny_instant(session.session_date, defs.PREMARKET_START)
    pm = [c for t_, c in zip(times, closes) if t_ >= pm_start]
    out["eff_on"] = _efficiency(closes)
    out["eff_pm"] = _efficiency(pm)
    last30 = [c for t_, c in zip(times, closes) if t_ >= cutoff - timedelta(minutes=31)]
    move30 = (last30[-1] - last30[0]) if len(last30) >= 2 else None
    out["ret_30_atr2m"] = move30 / atr2 if move30 is not None and atr2 else NAN
    pm_move = (pm[-1] - pm[0]) if len(pm) >= 2 else None
    out["rev_30"] = (np.sign(pm_move) * move30 / atr2 if pm_move is not None and move30 is not None and atr2
                     else NAN)
    out.update(_events(payload, session, cutoff))
    return out


def _f(v) -> float:
    if v is None:
        return NAN
    try:
        x = float(v)
    except (TypeError, ValueError):
        return NAN
    return x


def row(fs, day: str, symbol: str, payload: Optional[Dict[str, Any]], calendar_payload: Optional[Dict[str, Any]],
        context: bool) -> Dict[str, float]:
    """One session's feature row for ``symbol`` (see the module docstring). ``fs``: the v1 FeatureSet holding the
    instrument's own features for ``day``; ``payload``: the instrument's own snapshot (None: its snapshot features are
    missing); ``calendar_payload``: the snapshot whose frozen events give the calendar group (NQ's of the same date -
    the calendar is common); ``context``: add the context group (NQ rows of the multi-instrument arm)."""
    out: Dict[str, float] = {}
    own = fs.own.get(symbol)
    for name in mb.OWN:
        out[name] = _f(own.loc[day, name]) if own is not None and day in own.index else NAN
    snap = snapshot_features(payload) if payload is not None else {}
    for g in ("path", "geometry_summary", "geometry", "volatility"):
        for c in mb.FEATURE_GROUPS[g]["features"]:
            if c not in out:
                out[c] = _f(snap.get(c))
    cal_src = snapshot_features(calendar_payload) if calendar_payload is not None and calendar_payload is not payload \
        else snap
    for c in ("ev_minutes_to_next", "ev_first_30", "ev_rth"):
        out[c] = _f(cal_src.get(c))
    nq_row = fs.rows.get(day) or {}
    out["event_preopen"] = _f(nq_row.get("event_preopen"))
    out["event_session"] = _f(nq_row.get("event_session"))
    if context:
        for c in mb.FEATURE_GROUPS["context"]["features"]:
            if c.endswith("_age"):
                sym = next(s for s in ml.INSTRUMENTS if c == f"{s.lower()}_age")
                st = (fs.instruments.get(day) or {}).get(sym) or {}
                from forecaster.ml_features import USED
                out[c] = _f(st.get("age_min")) if st.get("status") in USED else NAN
            else:
                out[c] = _f(nq_row.get(c))
    return out


def candidate_columns() -> List[str]:
    return [f"c_{c}_{k}" for c in mb.CANDIDATES for k in CAND_FEATURES + ("valid", "possible")]


def candidate_row(payload: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """The candidate table flattened to columns c_<candidate>_<feature>."""
    table = candidate_table(payload) if payload is not None else {}
    out = {}
    for c in mb.CANDIDATES:
        for k in CAND_FEATURES + ("valid", "possible"):
            out[f"c_{c}_{k}"] = table.get(c, {}).get(k, NAN if k in CAND_FEATURES else 0.0)
    return out
