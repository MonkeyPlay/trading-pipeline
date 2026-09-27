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

Running it like the trained model
    ``backfill`` stores the forecast of every session in a range, each from
    earlier sessions only (``nq_forecast_v2.py scenario-backfill``), and
    ``backtest`` replays it walk-forward over the stored point-in-time snapshots
    and scores it (``scenario-backtest``). Both for NQ, the instrument with
    snapshots.

Outcomes
    First hour of each analogue: 10:29 close minus 09:30 open, from the best
    contract holding that day (database.queries.contracts_for_day), in units of
    that day's own volatility scale (its ATR, or previous close x historical
    volatility) and labelled up / down / flat against client.FLAT_BAND.
    Analogues without a complete first hour are skipped for the next closest.
"""

import math
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from config import Config
from database import forecast_store as store
from database.queries import (
    contracts_for_day,
    get_bars,
    get_contract_by_expiry,
    get_daily_rth_closes,
    get_day_bars,
    get_day_prediction,
    save_analogue_matches,
    save_feature_snapshot,
    save_prediction,
)
from features.calculations import calculate_pre_open_snapshot
from features import calendar as cal
from features import catalogue as catv2
from features.indicators import finite
from forecaster import models_v2
from forecaster.client import FLAT_BAND, MODEL_VERSION, PROMPT_VERSION, ForecastClient, classify_move
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


def rank_preopen(conn, symbol: str, day: str, target_v1: Dict[str, Any],
                 v1_history: Optional[V1History] = None) -> Dict[str, Any]:
    """
    Every earlier session ranked by pre-open similarity to ``day``, closest
    first: ``{'ranked', 'method', 'pool', 'scale', 'scale_name', 'target'}``.
    ``target`` is the day's point-in-time snapshot on the NQ path, else None.
    ``v1_history`` - the earlier sessions' v1 snapshots, or a callable
    returning them - is only read when the NQ snapshot path is not available.
    """
    day = str(day)
    target = v2_target(conn, day) if symbol == V2_SYMBOL else None
    scale = _atr(target["reference_values"]) if target else None
    ranked: List[Dict[str, Any]] = []
    method, scale_name = "", ""
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
        target = None
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
    return {"ranked": ranked, "method": method, "pool": len(candidates), "scale": scale,
            "scale_name": scale_name, "target": target}


def find_analogues(conn, symbol: str, day: str, target_v1: Dict[str, Any],
                   v1_history: Optional[V1History] = None, k: int = K) -> Dict[str, Any]:
    """
    The ``k`` closest earlier sessions (``rank_preopen``) with a measurable first
    hour: ``{'analogues', 'method', 'pool', 'scale', 'scale_name'}``. Each
    analogue has 'match_date', 'distance', 'similarity_score', 'ranking',
    'n_dims' and 'outcome' {'move', 'normalized', 'label'}.
    """
    found = rank_preopen(conn, symbol, day, target_v1, v1_history)
    analogues: List[Dict[str, Any]] = []
    for c in found["ranked"]:
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
    return {"analogues": analogues, "method": found["method"], "pool": found["pool"], "scale": found["scale"],
            "scale_name": found["scale_name"]}


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


# --------------------------------------------------------------------------
# The v1 pre-open snapshot and storage (dashboard and backfill)
# --------------------------------------------------------------------------

def cutoff_utc_iso(session_date: str) -> str:
    """09:30 ET of the given YYYY-MM-DD, as a UTC ISO-8601 string."""
    return pd.Timestamp(f"{session_date} 09:30").tz_localize("America/New_York").tz_convert("UTC").isoformat()


def vix_bars(conn, start_day: Optional[str], end_day: str) -> Optional[pd.DataFrame]:
    """Cash VIX bars over the given days, for pre-open context. None when absent."""
    if "VIX" not in Config.CONTEXT_SYMBOLS:
        return None
    contract = get_contract_by_expiry(conn, "VIX", Config.expiry_for("VIX"))
    if contract is None:
        return None
    rows = get_bars(conn, contract["contract_id"], interval="1m", start_day=start_day, end_day=end_day)
    return pd.DataFrame([dict(r) for r in rows]) if rows else None


def day_snapshot(conn, contract_id: int, day: str) -> Dict[str, Any]:
    """
    The pre-open feature snapshot (v1) of one stored day on one contract - as
    the dashboard's panel shows it - or {} when the contract lacks the previous
    session or the day's RTH open.
    """
    row = conn.execute(
        "SELECT MAX(trading_day) FROM session_days WHERE contract_id = %s AND interval = '1m' "
        "AND price_type = 'TRADES' AND bar_count > 0 AND trading_day < %s",
        (contract_id, day),
    ).fetchone()
    start = str(row[0]) if row is not None and row[0] is not None else day
    bars = get_bars(conn, contract_id, interval="1m", start_day=start, end_day=day)
    if not bars:
        return {}
    snapshot = calculate_pre_open_snapshot(
        pd.DataFrame([dict(r) for r in bars]), day,
        get_daily_rth_closes(conn, contract_id, interval="1m", end_day=day),
        vix_df=vix_bars(conn, start, day),
    )
    return snapshot if snapshot and "error" not in snapshot else {}


def store_forecast(conn, contract_id: int, day: str, snapshot: Dict[str, Any], forecast: Dict[str, Any],
                   analogues: List[Dict[str, Any]], model_version: str = MODEL_VERSION) -> int:
    """Stores a forecast with its v1 snapshot and its analogue matches; returns the prediction id."""
    cutoff = cutoff_utc_iso(day)
    snapshot_id = save_feature_snapshot(
        conn=conn, contract_id=contract_id, timestamp_utc=cutoff,
        previous_rth_high=snapshot.get("previous_rth_high"), previous_rth_low=snapshot.get("previous_rth_low"),
        previous_rth_close=snapshot.get("previous_rth_close"), overnight_high=snapshot.get("overnight_high"),
        overnight_low=snapshot.get("overnight_low"), overnight_range=snapshot.get("overnight_range"),
        gap=snapshot.get("gap"), pre_open_direction=snapshot.get("pre_open_direction", "FLAT"),
        historical_volatility=snapshot.get("historical_volatility"), vwap=snapshot.get("vwap"),
        raw_features=snapshot, feature_version=Config.FEATURE_VERSION,
        vix_pre_open=snapshot.get("vix_pre_open"), vix_change=snapshot.get("vix_change"),
    )
    prediction_id = save_prediction(
        conn=conn, contract_id=contract_id, forecast_cutoff=cutoff, snapshot_id=snapshot_id,
        model_version=model_version, prompt_version=PROMPT_VERSION,
        opening_bias=forecast.get("opening_bias"), scenarios=forecast.get("scenarios", {}),
        probabilities=forecast.get("probabilities", {}), raw_response=str(forecast),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    if analogues:
        save_analogue_matches(conn, prediction_id, [
            {"match_date": a["match_date"], "similarity_score": a["similarity_score"], "ranking": a["ranking"]}
            for a in analogues])
    return prediction_id


# --------------------------------------------------------------------------
# Backfill and backtest
# --------------------------------------------------------------------------

def backfill(conn, start: str, end: str, symbol: str = V2_SYMBOL, force: bool = False,
             log: Callable[[str], None] = print) -> Dict[str, int]:
    """
    Stores the forecast of every session of ``symbol`` in [start, end], in
    session order, each from earlier sessions only - the pre-open snapshot and
    matches of that day, the first hours of earlier days. A session that already
    has a forecast of this model version is skipped unless ``force``.
    """
    counts = {"stored": 0, "skipped": 0, "unavailable": 0}
    for s in cal.sessions_between(start, end):
        day = s.session_date.isoformat()
        held = contracts_for_day(conn, symbol, day)
        if not held:
            continue
        contract_id = int(held[0]["contract_id"])
        existing = get_day_prediction(conn, symbol, day, contract_id)
        if existing is not None and existing["model_version"] == MODEL_VERSION and not force:
            counts["skipped"] += 1
            continue
        snapshot = day_snapshot(conn, contract_id, day)
        if not snapshot:
            counts["unavailable"] += 1
            log(f"{day}: no pre-open snapshot (previous session or open missing)")
            continue
        def v1_history(contract_id=contract_id, day=day):
            # only read when the snapshot path cannot match (too few stored snapshots)
            rows = conn.execute(
                "SELECT trading_day FROM session_days WHERE contract_id = %s AND interval = '1m' AND bar_count > 0 "
                "AND trading_day < %s ORDER BY trading_day DESC LIMIT 60", (contract_id, day)).fetchall()
            return [h for h in (day_snapshot(conn, contract_id, str(r[0])) for r in rows) if h]

        forecast, analogues, client = analogue_forecast(conn, symbol, day, snapshot, v1_history)
        if not analogues:
            counts["unavailable"] += 1
            log(f"{day}: no earlier sessions to match")
            continue
        pid = store_forecast(conn, contract_id, day, snapshot, forecast, analogues, client.model_name)
        counts["stored"] += 1
        p = forecast["probabilities"]
        log(f"{day}: prediction #{pid} {forecast['opening_bias']} (up {p['bullish_continuation_pct']:.0f} % · "
            f"flat {p['mean_reversion_gap_fill_pct']:.0f} % · down {p['bearish_rejection_pct']:.0f} %)")
    return counts


_LABELS = ("up", "flat", "down")
PRIOR_WEIGHT = 10.0      # when scoring, the base rates count as this many sessions next to the analogues


def backtest(conn, symbol: str = V2_SYMBOL, k: int = K, min_history: int = 60) -> Dict[str, Any]:
    """
    The forecast replayed walk-forward on every stored point-in-time snapshot of
    ``symbol`` with at least ``min_history`` earlier sessions that have a first
    hour, scored on the first hour it forecasts:

      probabilities  3-way log loss of up / flat / down - the analogues'
                     frequencies shrunk toward the base rates of all earlier
                     sessions (worth PRIOR_WEIGHT sessions; raw frequencies of 10
                     analogues can be 0) - against those base rates
      bias           hit rate of the shown bias (the most frequent outcome; a tie
                     is NEUTRAL, counted as flat) against always calling the most
                     common earlier outcome

    Gains are base minus forecast (> 0: the forecast is better).
    """
    from forecaster.first_hour import load_first_hours

    hours = load_first_hours(conn, symbol)
    snaps = [s for s in store.snapshots_before(conn, "9999-12-31", catv2.FEATURE_VERSION, symbol)
             if s["session_date"] in hours and _atr(s["reference_values"])]

    def label(s):
        h = hours[s["session_date"]]
        return classify_move((h.close - h.open) / _atr(s["reference_values"]))

    loss_m, loss_b, hit_m, hit_b = [], [], [], []
    for i, snap in enumerate(snaps):
        earlier = snaps[:i]
        if len(earlier) < min_history:
            continue
        cands = [{"session_date": c["session_date"], "vector": _v2_vector(c["features"])} for c in earlier]
        ranked = rank_analogues(_v2_vector(snap["features"]), cands, MATCH_FEATURES, MIN_V2_DIMS)[:k]
        if len(ranked) < k:
            continue
        by_day = {c["session_date"]: c for c in earlier}
        outcomes = [label(by_day[r["session_date"]]) for r in ranked]
        history = [label(c) for c in earlier]
        base = {lab: (history.count(lab) + 1) / (len(history) + 3) for lab in _LABELS}
        shrunk = {lab: (outcomes.count(lab) + PRIOR_WEIGHT * base[lab]) / (len(outcomes) + PRIOR_WEIGHT)
                  for lab in _LABELS}
        actual = label(snap)
        loss_m.append(-math.log(shrunk[actual]))
        loss_b.append(-math.log(base[actual]))
        counts = {lab: outcomes.count(lab) for lab in _LABELS}
        top = max(counts.values())
        leaders = [lab for lab, c in counts.items() if c == top]
        bias = leaders[0] if len(leaders) == 1 else "flat"
        hit_m.append(bias == actual)
        hit_b.append(max(_LABELS, key=lambda lab: history.count(lab)) == actual)
    n = len(loss_m)

    def summary(model, base):
        m, b = np.asarray(model, float), np.asarray(base, float)
        g = b - m
        return {"model": float(m.mean()) if n else math.nan, "base": float(b.mean()) if n else math.nan,
                "gain": float(g.mean()) if n else math.nan,
                "gain_se": float(g.std(ddof=1) / math.sqrt(n)) if n > 1 else math.nan}

    return {"sessions": n, "k": k, "band": FLAT_BAND, "probabilities": summary(loss_m, loss_b),
            "hit_rate": float(np.mean(hit_m)) if n else math.nan,
            "base_hit_rate": float(np.mean(hit_b)) if n else math.nan,
            "hits": summary([1.0 - h for h in hit_m], [1.0 - h for h in hit_b])}


def format_backtest(r: Dict[str, Any]) -> str:
    def verdict(s):
        return ("better" if s["gain"] > 2 * s["gain_se"] else "worse" if s["gain"] < -2 * s["gain_se"]
                else "no difference")

    p, h = r["probabilities"], r["hits"]
    return "\n".join([
        f"Opening scenario generator backtest: {r['sessions']} session(s), {r['k']} analogues each, only "
        f"earlier sessions used; first hour up / down beyond ±{r['band']:.2f} ATR, else flat.", "",
        f"{'':38} {'forecast':>9} {'simple':>9} {'gain ± SE':>20}",
        f"{'probabilities, log loss (3-way)':38} {p['model']:9.4f} {p['base']:9.4f} "
        f"{p['gain']:+10.4f} ± {p['gain_se']:.4f}  {verdict(p)}",
        f"{'bias, hit rate':38} {r['hit_rate'] * 100:8.1f}% {r['base_hit_rate'] * 100:8.1f}% "
        f"{(r['hit_rate'] - r['base_hit_rate']) * 100:+9.1f} pp ± {h['gain_se'] * 100:.1f}  {verdict(h)}",
        "", "simple: the base rates of all earlier sessions, and always calling the most common earlier outcome.",
    ])
