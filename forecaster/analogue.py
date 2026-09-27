# forecaster/analogue.py
"""
Chooses the historical analogues of a session and measures how their first hour
went - the inputs of the analogue forecast (forecaster/client.py,
analogue_baseline_v2). Used by the dashboard and scripts/daily_forecast.py.

Matching
    NQ: the stored point-in-time snapshots (features/catalogue.py
    FEATURE_VERSION) of every earlier NQ session, whatever contract it traded
    on, compared on MATCH_FEATURES - the pre-open volatility inputs the trained
    model uses (models_v2.VOL_FEATURES) plus the signed gap in ATR. The target
    session's own snapshot is read from the store, or reconstructed from the
    stored bars (and stored) when there is none.
    Other instruments (no v2 snapshots): gap and overnight range in units of one
    day's volatility, from the v1 snapshots the caller passes.
    Either way each input is standardised over the candidates and the distance
    is the root mean square of the differences (matching.normalizer.rank_analogues);
    there is no direction flag, which in v1 outweighed everything else.

Outcomes
    First hour of each analogue: 10:29 close minus 09:30 open, from the best
    contract holding that day (database.queries.contracts_for_day), in units of
    that day's own volatility scale (its ATR, or previous close x historical
    volatility) and labelled up / down / flat against client.FLAT_BAND.
    Analogues without a complete first hour are skipped for the next closest.
"""

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import pandas as pd

from database import forecast_store as store
from database.queries import contracts_for_day, get_day_bars
from features import calendar as cal
from features import catalogue as catv2
from features.indicators import finite
from forecaster import models_v2
from forecaster.client import ForecastClient, classify_move
from matching.normalizer import rank_analogues, v1_match_vector

K = 10
V2_SYMBOL = "NQ"
MATCH_FEATURES = list(models_v2.VOL_FEATURES) + ["gap_signed_atr"]
MIN_V2_DIMS = 6                  # of the 11 inputs, both sides must have at least this many
MIN_V2_POOL = 20                 # fewer comparable stored snapshots: match on the v1 features instead
V1_KEYS = ["gap_sigma", "overnight_range_sigma"]
HOUR = pd.Timedelta(minutes=60)


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------

def _open_at(day: str) -> pd.Timestamp:
    try:
        return pd.Timestamp(cal.session(day).rth_open_at)
    except cal.CalendarCoverageError:
        return pd.Timestamp(f"{day} 09:30").tz_localize("America/New_York").tz_convert("UTC")


def first_hour_on(conn, contract_id: int, day: str) -> Optional[Dict[str, Any]]:
    """
    ``{'open', 'close', 'move', 'contract_id'}`` of ``day``'s first hour on one
    contract: the 09:30 open to the close of the last bar before 10:30, which
    must start at 10:25 or later. None when those bars are not stored.
    """
    rows = get_day_bars(conn, contract_id, day, interval="1m", session_scope="RTH")
    if not rows:
        return None
    open_at = _open_at(day)
    df = pd.DataFrame([dict(r) for r in rows])
    df["ts"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    first = df[df["ts"] == open_at]
    hour = df[(df["ts"] >= open_at) & (df["ts"] < open_at + HOUR)].sort_values("ts")
    if first.empty or hour.empty or hour["ts"].iloc[-1] < open_at + pd.Timedelta(minutes=55):
        return None
    o, c = float(first["open"].iloc[0]), float(hour["close"].iloc[-1])
    return {"open": o, "close": c, "move": c - o, "contract_id": int(contract_id)}


def first_hour_move(conn, symbol: str, day: str) -> Optional[Dict[str, Any]]:
    """``first_hour_on`` the best contract of ``symbol`` holding ``day`` that has it."""
    for contract in contracts_for_day(conn, symbol, day):
        move = first_hour_on(conn, int(contract["contract_id"]), day)
        if move is not None:
            return move
    return None


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------

def v2_target(conn, day: str, symbol: str = V2_SYMBOL, build: bool = True) -> Optional[Dict[str, Any]]:
    """
    The session's stored point-in-time snapshot (its live capture if any), or one
    reconstructed from the stored bars and stored - None when it cannot be built.
    """
    found = store.find_snapshots(conn, day, catv2.FEATURE_VERSION, symbol)
    if found:
        return found[0]
    if not build:
        return None
    from features.market_data import DbMarketData
    from features.nq_v2 import SnapshotError, build_snapshot
    try:
        store.register_feature_version(conn, catv2.registry_record())
        snapshot_id, _ = store.save_feature_snapshot(conn, build_snapshot(DbMarketData(conn), day))
    except (SnapshotError, cal.CalendarCoverageError, ValueError):
        return None
    return store.get_feature_snapshot(conn, snapshot_id)


def _v2_vector(features: Dict[str, Any]) -> Dict[str, Optional[float]]:
    return {k: finite(features.get(k)) for k in MATCH_FEATURES}


def _atr(reference_values: Dict[str, Any]) -> Optional[float]:
    a = finite((reference_values or {}).get("A"))
    return a if a and a > 0 else None


# --------------------------------------------------------------------------
# Forecast
# --------------------------------------------------------------------------

V1History = Union[Sequence[Dict[str, Any]], Callable[[], Sequence[Dict[str, Any]]]]


def find_analogues(conn, symbol: str, day: str, target_v1: Dict[str, Any],
                   v1_history: Optional[V1History] = None, k: int = K) -> Dict[str, Any]:
    """
    The ``k`` closest earlier sessions with a measurable first hour:
    ``{'analogues', 'method', 'pool', 'scale', 'scale_name'}``. Each analogue has
    'match_date', 'distance', 'similarity_score', 'ranking', 'n_dims' and
    'outcome' {'move', 'normalized', 'label'}. ``v1_history`` - the earlier
    sessions' v1 snapshots, or a callable returning them - is only read when the
    NQ snapshot path is not available.
    """
    day = str(day)
    target = v2_target(conn, day) if symbol == V2_SYMBOL else None
    scale = _atr(target["reference_values"]) if target else None
    ranked = []
    if target is not None and scale is not None:
        candidates = [
            {"session_date": c["session_date"], "vector": _v2_vector(c["features"]),
             "scale": _atr(c["reference_values"])}
            for c in store.snapshots_before(conn, day, catv2.FEATURE_VERSION, symbol)
        ]
        ranked = rank_analogues(_v2_vector(target["features"]), candidates, MATCH_FEATURES, MIN_V2_DIMS)
        method = (f"standardised distance over {len(MATCH_FEATURES)} pre-open volatility and gap inputs "
                  f"({catv2.FEATURE_VERSION} snapshots, all {symbol} contracts)")
        scale_name = "ATR"
    if len(ranked) < MIN_V2_POOL:
        # No point-in-time snapshots for this instrument, or too few stored yet.
        history = v1_history() if callable(v1_history) else (v1_history or [])
        target_vector, scale = v1_match_vector(target_v1 or {})
        candidates = []
        for snap in history:
            if str(snap.get("trading_day")) >= day:
                continue
            vector, s = v1_match_vector(snap)
            candidates.append({"session_date": str(snap["trading_day"]), "vector": vector, "scale": s})
        ranked = rank_analogues(target_vector, candidates, V1_KEYS, len(V1_KEYS))
        method = "standardised gap and overnight range in units of one day's volatility"
        scale_name = "daily σ"

    analogues: List[Dict[str, Any]] = []
    for c in ranked:
        if len(analogues) == k:
            break
        if not c.get("scale"):
            continue
        move = first_hour_move(conn, symbol, c["session_date"])
        if move is None:
            continue
        normalized = move["move"] / c["scale"]
        analogues.append({
            "match_date": c["session_date"], "distance": c["distance"],
            "similarity_score": c["similarity_score"], "n_dims": c["n_dims"],
            "ranking": len(analogues) + 1,
            "outcome": {"move": move["move"], "normalized": normalized, "label": classify_move(normalized)},
        })
    return {"analogues": analogues, "method": method, "pool": len(candidates), "scale": scale,
            "scale_name": scale_name}


def analogue_forecast(conn, symbol: str, day: str, target_v1: Dict[str, Any],
                      v1_history: Optional[V1History] = None, k: int = K,
                      instrument: Optional[str] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]], ForecastClient]:
    """(forecast, analogues, client) for ``symbol``'s session ``day``."""
    found = find_analogues(conn, symbol, day, target_v1, v1_history, k)
    client = ForecastClient()
    forecast = client.get_forecast(day, target_v1, found["analogues"], instrument=instrument,
                                   scale=found["scale"], scale_name=found["scale_name"],
                                   method=found["method"], pool=found["pool"])
    return forecast, found["analogues"], client
