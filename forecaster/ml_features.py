# forecaster/ml_features.py
"""
Point-in-time features for the NQ direction model (contracts/nq_ml.py): NQ's own,
the context instruments', the cross-instrument ones and the calendar's, as of a
session's cutoff - each with its source instrument, market timestamp, availability
status and data age.

  load(conn, symbols, first, last)   every instrument's 1m bars and active contracts
                                     over the sessions: one query per instrument, run in
                                     parallel threads on their own connections (the time is
                                     the database's), times as integer microseconds
  raw_day(...)                       one instrument's raw values on one session as of the
                                     cutoff: the last completed bar ending at or before it
                                     (and received by ``as_of`` when given), the previous
                                     close at the previous session's scheduled close, the
                                     08:00 and cutoff-30 prices, the overnight range,
                                     volatility and volume, the previous session's RTH
                                     high and low - all on the session's own contract,
                                     whose warm-up bars cover the day before a roll, so a
                                     return never straddles two contracts
  build(conn, days, ...)             the feature rows of ``days``: the raw values
                                     normalised by the instrument's earlier sessions only
                                     (shifted rolling windows), the cross-instrument
                                     features, the calendar features from each session's
                                     frozen snapshot events, the missing indicators

Availability of an input (contracts/nq_ml.Instrument):

  observed       the bar used was received live (within 2 hours of its end)
  reconstructed  the bar used was stored after the fact (history, no live receipt)
  closed         the market is closed at the cutoff (the instrument's schedule); its last
                 value is used while younger than closed_max_age_minutes
  delayed        a fresher bar exists in market time but had not been received by
                 ``as_of``; the latest received one is used while within the limit
  stale          nothing received within the freshness limit: missing
  missing        no bar on the instrument's session before the cutoff: missing
  no_session     the instrument has no session that day: missing

Nothing is forward-filled past those limits and a missing return is never a zero: a
missing input leaves its features missing (NaN), which the model's imputer - fitted on
the training window - fills, beside the instrument's missing indicator.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from contracts import nq_ml as ml
from contracts import nq_prompt_v2 as defs
from features import calendar as cal

MINUTE = timedelta(minutes=1)
LIVE_WINDOW = timedelta(hours=2)
USED = ("observed", "reconstructed", "closed", "delayed")
ALL_SYMBOLS = ("NQ",) + tuple(ml.INSTRUMENTS)


def _utc(value) -> Optional[datetime]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    t = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

@dataclass
class InstrumentBars:
    """One instrument's bars by (contract_id, trading_day) and its active contract by trading day."""
    symbol: str
    days: Dict[Tuple[int, str], pd.DataFrame] = field(default_factory=dict)
    active: Dict[str, int] = field(default_factory=dict)


def load(conn, symbols: Sequence[str], first: str, last: str) -> Dict[str, InstrumentBars]:
    """Every instrument's 1m TRADES bars of the trading days [first, last], on every contract stored for them (the
    active one and the next one's warm-up days), and the active contract of each day. The instruments are read in
    parallel, each on its own connection (committed data; the shared ``conn`` when one cannot be opened)."""
    symbols = list(symbols)
    if len(symbols) <= 1:
        return {s: _load_one(conn, s, first, last) for s in symbols}

    def work(symbol: str) -> InstrumentBars:
        try:
            own = conn.reopen()
        except Exception:                               # not a reopenable connection: the shared one, serialised
            return _load_one(conn, symbol, first, last)
        try:
            return _load_one(own, symbol, first, last)
        finally:
            own.close()

    with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
        return dict(zip(symbols, pool.map(work, symbols)))


def _load_one(conn, symbol: str, first: str, last: str) -> InstrumentBars:
    rows = conn.fetch_tuples(
        "SELECT b.contract_id, b.trading_day::text AS day, (extract(epoch FROM b.timestamp_utc) * 1000000)::bigint, "
        "b.open, b.high, b.low, b.close, b.volume, (extract(epoch FROM b.first_stored_at) * 1000000)::bigint "
        "FROM bars b JOIN contracts k USING (contract_id) WHERE k.symbol = %s AND b.interval = '1m' "
        "AND b.price_type = 'TRADES' AND b.trading_day BETWEEN %s AND %s "
        "ORDER BY b.contract_id, b.trading_day, b.timestamp_utc;", (symbol, first, last))
    frame = pd.DataFrame(rows, columns=["contract_id", "day", "start", "open", "high", "low", "close", "volume",
                                        "stored"])
    ib = InstrumentBars(symbol)
    if not frame.empty:
        frame["start"] = pd.to_datetime(frame["start"].astype("int64"), unit="us", utc=True)
        frame["stored"] = pd.to_datetime(frame["stored"].astype("Int64"), unit="us", utc=True)
        for col in ("open", "high", "low", "close"):
            frame[col] = frame[col].astype(float)
        frame["volume"] = frame["volume"].astype(float)
        for (cid, day), g in frame.groupby(["contract_id", "day"], sort=False):
            ib.days[(int(cid), str(day))] = g.reset_index(drop=True)
    for r in conn.execute("SELECT trading_day::text, contract_id FROM active_contracts WHERE symbol = %s AND "
                          "trading_day BETWEEN %s AND %s;", (symbol, first, last)).fetchall():
        ib.active[str(r[0])] = int(r[1])
    return ib


# --------------------------------------------------------------------------
# One instrument, one session: raw values as of the cutoff
# --------------------------------------------------------------------------

def _hm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def closed_at(symbol: str, when: datetime) -> bool:
    """Whether the instrument's market is closed at ``when`` (its contract's closed windows, New York time)."""
    if symbol == "NQ":
        return False
    t = when.astimezone(cal.NY_TZ).time()
    for start, end in ml.INSTRUMENTS[symbol].closed_windows_et:
        a, b = _hm(start), _hm(end)
        if (a <= t < b) if a < b else (t >= a or t < b):
            return True
    return False


def _limit(symbol: str, closed: bool) -> Optional[int]:
    if symbol == "NQ":
        return 5
    inst = ml.INSTRUMENTS[symbol]
    return inst.closed_max_age_minutes if closed else inst.max_age_minutes


def _upto(frame: pd.DataFrame, when: datetime, side: str = "right") -> int:
    """How many of a day's bars (in time order) start at or before ``when`` (side 'left': before it)."""
    return int(frame["start"].searchsorted(pd.Timestamp(when), side=side))


def raw_day(ib: InstrumentBars, session: cal.Session, prev: Optional[cal.Session], cutoff: datetime,
            as_of: Optional[datetime] = None) -> Dict[str, Any]:
    """One instrument's raw values on ``session`` as of ``cutoff`` (see the module docstring); ``as_of`` limits the
    session's own bars to those received by then."""
    day = session.session_date.isoformat()
    out: Dict[str, Any] = {"symbol": ib.symbol, "day": day, "status": "no_session", "market_ts": None, "age_min": None,
                           "contract_id": ib.active.get(day)}
    cid = ib.active.get(day)
    frame = ib.days.get((cid, day)) if cid is not None else None
    if frame is None or frame.empty:
        return out
    # the day's bars are in time order: positions by binary search, values as arrays (the bars that ended by the
    # cutoff are the first k; those also received by as_of the positions in ``seen``)
    k = _upto(frame, cutoff - MINUTE)
    if k == 0:
        out["status"] = "missing"
        return out
    seen = (np.arange(k) if as_of is None else
            np.flatnonzero((frame["stored"].iloc[:k] <= pd.Timestamp(as_of)).to_numpy()))
    closed = closed_at(ib.symbol, cutoff - MINUTE)
    limit = _limit(ib.symbol, closed)
    if len(seen) == 0:
        out["status"] = "stale"
        return out
    j = int(seen[-1])
    start = _utc(frame["start"].iat[j])
    age = (cutoff - (start + MINUTE)).total_seconds() / 60
    out.update(market_ts=start.strftime("%Y-%m-%dT%H:%M:%SZ"), age_min=round(age, 2))
    if limit is not None and age > limit:
        out["status"] = "stale"
        return out
    stored = _utc(frame["stored"].iat[j])
    live = stored is not None and stored - (start + MINUTE) < LIVE_WINDOW
    newest = _utc(frame["start"].iat[k - 1])
    out["status"] = ("closed" if closed else "delayed" if newest > start else "observed" if live else "reconstructed")
    close = frame["close"].to_numpy()

    def last_seen_before(when: datetime) -> Optional[float]:
        n = int(np.searchsorted(seen, _upto(frame, when - MINUTE)))     # the seen bars ending by ``when``
        return None if n == 0 else float(close[seen[n - 1]])

    first_pm = cal.ny_instant(session.session_date, time(8, 0))
    rets = np.diff(np.log(close[seen]))
    out.update(
        p_cut=float(close[j]), p_0800=last_seen_before(first_pm), p_m30=last_seen_before(cutoff - timedelta(minutes=30)),
        on_high=float(np.nanmax(frame["high"].to_numpy()[seen])), on_low=float(np.nanmin(frame["low"].to_numpy()[seen])),
        rv_on=float(np.std(rets)) if len(rets) >= 30 else None,
        vol_on=float(np.nansum(frame["volume"].to_numpy()[seen])) if ml.INSTRUMENTS.get(ib.symbol) is None or
        ml.INSTRUMENTS[ib.symbol].has_volume else None)
    # the previous session on the same contract: its close at the scheduled close and its RTH high and low
    if prev is not None:
        pframe = ib.days.get((cid, prev.session_date.isoformat()))
        if pframe is not None and not pframe.empty:
            n = _upto(pframe, prev.scheduled_close_at - MINUTE)
            lo, hi = _upto(pframe, prev.rth_open_at, "left"), _upto(pframe, prev.scheduled_close_at, "left")
            out.update(p_prev_close=None if n == 0 else float(pframe["close"].iat[n - 1]),
                       prev_rth_high=None if hi <= lo else float(np.nanmax(pframe["high"].to_numpy()[lo:hi])),
                       prev_rth_low=None if hi <= lo else float(np.nanmin(pframe["low"].to_numpy()[lo:hi])))
    return out


def rth_range(ib: InstrumentBars, session: cal.Session) -> Optional[float]:
    """The session's whole RTH high-low range on its own contract (an input of later sessions' daily range only)."""
    day = session.session_date.isoformat()
    frame = ib.days.get((ib.active.get(day), day))
    if frame is None or frame.empty:
        return None
    lo, hi = _upto(frame, session.rth_open_at, "left"), _upto(frame, session.scheduled_close_at, "left")
    if hi - lo < 30:
        return None
    return float(np.nanmax(frame["high"].to_numpy()[lo:hi]) - np.nanmin(frame["low"].to_numpy()[lo:hi]))


# --------------------------------------------------------------------------
# Normalised features over the sessions
# --------------------------------------------------------------------------

def _change(kind: str, a: Optional[float], b: Optional[float]) -> float:
    """The move from ``b`` to ``a`` for the instrument's value kind: a log return of a price or an index level, a
    difference of a yield."""
    if a is None or b is None or (kind != "yield" and (a <= 0 or b <= 0)):
        return np.nan
    return (a - b) if kind == "yield" else math.log(a / b)


def _prior_std(s: pd.Series) -> pd.Series:
    return s.shift(1).rolling(ml.ROLLING_SESSIONS, min_periods=ml.ROLLING_MIN_SESSIONS).std()


def _prior_mean(s: pd.Series, n: int, min_n: int) -> pd.Series:
    return s.shift(1).rolling(n, min_periods=min_n).mean()


def instrument_frame(ib: InstrumentBars, sessions: Sequence[cal.Session], cutoff_of, as_of_day: Optional[str] = None,
                     as_of: Optional[datetime] = None) -> pd.DataFrame:
    """One instrument's raw values and normalised own features over ``sessions`` (oldest first). Only the session
    ``as_of_day`` is limited to the bars received by ``as_of``; the earlier ones are complete history."""
    kind = "price" if ib.symbol == "NQ" else ml.INSTRUMENTS[ib.symbol].value_kind
    rows, ranges = [], []
    prev = None
    for s in sessions:
        day = s.session_date.isoformat()
        r = raw_day(ib, s, prev, cutoff_of(s), as_of if day == as_of_day else None)
        rows.append(r)
        ranges.append(rth_range(ib, s) if day != as_of_day else None)
        prev = s
    f = pd.DataFrame(rows).set_index("day")
    for col in ("p_cut", "p_0800", "p_m30", "on_high", "on_low", "rv_on", "vol_on", "p_prev_close", "prev_rth_high",
                "prev_rth_low"):
        if col not in f:
            f[col] = np.nan
        f[col] = pd.to_numeric(f[col], errors="coerce")
    used = f["status"].isin(USED)
    f["ret_on_raw"] = [(_change(kind, a, b) if u else np.nan) for a, b, u in zip(f["p_cut"], f["p_prev_close"], used)]
    f["ret_pm_raw"] = [(_change(kind, a, b) if u else np.nan) for a, b, u in zip(f["p_cut"], f["p_0800"], used)]
    f["ret_30_raw"] = [(_change(kind, a, b) if u else np.nan) for a, b, u in zip(f["p_cut"], f["p_m30"], used)]
    f["rv_raw"] = f["rv_on"].where(used)
    f["vol_raw"] = f["vol_on"].where(used)
    f["range_raw"] = pd.Series(ranges, index=f.index, dtype=float)
    for name in ("ret_on", "ret_pm", "ret_30"):
        f[name] = f[f"{name}_raw"] / _prior_std(f[f"{name}_raw"])
    rv_med = f["rv_raw"].shift(1).rolling(ml.ROLLING_SESSIONS, min_periods=ml.ROLLING_MIN_SESSIONS).median()
    f["rv_on_f"] = np.log(f["rv_raw"] / rv_med)
    vol_mean = _prior_mean(f["vol_raw"], ml.VOLUME_SESSIONS, ml.VOLUME_SESSIONS // 2)
    f["vol_on_f"] = np.log(f["vol_raw"] / vol_mean)
    span = f["on_high"] - f["on_low"]
    f["range_pos"] = ((f["p_cut"] - f["on_low"]) / span - 0.5).where(used & (span > 0))
    atr = _prior_mean(f["range_raw"], ml.ATR_SESSIONS, ml.ATR_SESSIONS - 4)
    f["atr"] = atr
    f["gap_atr"] = ((f["p_cut"] - f["p_prev_close"]) / atr).where(used)
    f["dist_high_atr"] = ((f["p_cut"] - f["prev_rth_high"]) / atr).where(used)
    f["dist_low_atr"] = ((f["p_cut"] - f["prev_rth_low"]) / atr).where(used)
    if kind == "index_level":                       # VIX: its level against its own recent range
        logv = np.log(f["p_cut"].where(used))
        f["level_z"] = (logv - _prior_mean(logv, ml.ROLLING_SESSIONS, ml.ROLLING_MIN_SESSIONS)) / _prior_std(logv)
    return f


OWN = {"ret_on": "ret_on", "ret_pm": "ret_pm", "ret_30": "ret_30", "rv_on": "rv_on_f", "vol_on": "vol_on_f",
       "range_pos": "range_pos", "gap_atr": "gap_atr", "dist_high_atr": "dist_high_atr",
       "dist_low_atr": "dist_low_atr"}


def _beta_resid(n: pd.Series, e: pd.Series) -> Tuple[pd.Series, pd.Series]:
    """Per session, NQ's move net of beta x ES's, over the residual's standard deviation - beta and the deviation
    from the previous sessions only; and the previous sessions' correlation."""
    resid, corr = [], []
    for i in range(len(n)):
        lo = max(0, i - ml.ROLLING_SESSIONS)
        a, b = n.iloc[lo:i], e.iloc[lo:i]
        ok = a.notna() & b.notna()
        a, b = a[ok], b[ok]
        if len(a) < ml.ROLLING_MIN_SESSIONS or b.var() == 0 or np.isnan(n.iloc[i]) or np.isnan(e.iloc[i]):
            resid.append(np.nan)
            corr.append(np.nan if len(a) < ml.ROLLING_MIN_SESSIONS else float(np.corrcoef(a, b)[0, 1]))
            continue
        beta = float(np.cov(a, b)[0, 1] / b.var())
        sd = float((a - beta * b).std())
        resid.append((n.iloc[i] - beta * e.iloc[i]) / sd if sd > 0 else np.nan)
        corr.append(float(np.corrcoef(a, b)[0, 1]))
    return pd.Series(resid, index=n.index), pd.Series(corr, index=n.index)


def _events(snapshot: Optional[Dict[str, Any]], session: cal.Session, cutoff: datetime) -> Tuple[float, float]:
    """(event_preopen, event_session) from the snapshot's frozen events: a high-tier event scheduled from the
    previous close to the cutoff, and from the cutoff to the scheduled close."""
    if snapshot is None:
        return np.nan, np.nan
    events = ((snapshot.get("payload") or {}).get("events") or {}).get("events") or []
    pre = sess = 0.0
    for e in events:
        if e.get("tier") != "high" or not e.get("scheduled_at"):
            continue
        at = _utc(e["scheduled_at"])
        if at <= cutoff:
            pre = 1.0
        elif at < session.scheduled_close_at:
            sess = 1.0
    return pre, sess


@dataclass
class FeatureSet:
    """The feature rows of the sessions asked for (``rows``: day -> feature -> value) and, per day, every feature's
    provenance (source instruments, market timestamps, statuses, ages) and every instrument's status."""
    rows: Dict[str, Dict[str, float]]
    provenance: Dict[str, Dict[str, Dict[str, Any]]]
    instruments: Dict[str, Dict[str, Dict[str, Any]]]
    own: Dict[str, pd.DataFrame]                     # per instrument its normalised own features (pooled rows)
    # day -> instrument -> what was observed, in its own units: the value at the cutoff and the moves since the
    # previous close and since 08:00 (percent for prices and index levels, yield points for a yield)
    observations: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)


def build(conn, days: Sequence[str], snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
          as_of: Optional[datetime] = None, symbols: Sequence[str] = ALL_SYMBOLS,
          profile: str = ml.PROFILE, history_sessions: int = ml.ROLLING_SESSIONS + 25,
          loaded: Optional[Dict[str, InstrumentBars]] = None) -> FeatureSet:
    """The features of ``days`` as of their profile cutoff. ``as_of`` (live or replay issue) limits the last day's own
    bars to those received by then; history before it is complete. ``snapshots`` (day -> the session's snapshot)
    give the calendar features. ``loaded`` reuses bars already loaded."""
    days = sorted(days)
    first = cal.session(days[0]).session_date
    sessions = [s for s in cal.sessions_between((first - timedelta(days=history_sessions * 2)).isoformat(), days[-1])
                if s.is_open]
    sessions = sessions[-(history_sessions + len(sessions[sessions.index(next(s for s in sessions
                                                                               if s.session_date >= first)):])):]
    p = defs.PROFILES[profile]
    cutoff_of = lambda s: cal.ny_instant(s.session_date, p.cutoff)          # noqa: E731
    bars = loaded or load(conn, symbols, (sessions[0].session_date - timedelta(days=5)).isoformat(), days[-1])
    as_of_day = days[-1] if as_of is not None else None
    frames = {sym: instrument_frame(bars[sym], sessions, cutoff_of, as_of_day, as_of) for sym in symbols}
    nq = frames["NQ"]
    es = frames.get("ES")
    resid_on, corr = (_beta_resid(nq["ret_on_raw"], es["ret_on_raw"]) if es is not None else
                      (pd.Series(np.nan, index=nq.index), pd.Series(np.nan, index=nq.index)))
    resid_pm, _ = (_beta_resid(nq["ret_pm_raw"], es["ret_pm_raw"]) if es is not None else
                   (pd.Series(np.nan, index=nq.index), None))
    rows, prov, inst, obs = {}, {}, {}, {}
    by_day = {s.session_date.isoformat(): s for s in sessions}
    for day in days:
        s = by_day[day]
        cutoff = cutoff_of(s)
        r: Dict[str, float] = {}
        pv: Dict[str, Dict[str, Any]] = {}
        st = {sym: {k: frames[sym].loc[day, k] if day in frames[sym].index else None
                    for k in ("status", "market_ts", "age_min", "contract_id")} for sym in symbols}
        for sym in st:                              # plain Python values: they go into the run's evidence as JSON
            st[sym] = {k: (None if v is None or (isinstance(v, float) and math.isnan(v)) else
                           v.item() if hasattr(v, "item") else v) for k, v in st[sym].items()}
        inst[day] = st
        obs[day] = {}
        for sym in symbols:
            fr = frames[sym]
            kind = "price" if sym == "NQ" else ml.INSTRUMENTS[sym].value_kind
            scale, unit = (1.0, "yield points") if kind == "yield" else (100.0, "%")
            val = lambda c: (None if day not in fr.index or fr.loc[day, c] != fr.loc[day, c]  # noqa: E731
                             else float(fr.loc[day, c]))
            on, pm = val("ret_on_raw"), val("ret_pm_raw")
            obs[day][sym] = {"value": val("p_cut"), "since_close": None if on is None else on * scale,
                             "since_0800": None if pm is None else pm * scale, "unit": unit}

        def put(name, value, sources):
            v = float(value) if value is not None and not (isinstance(value, float) and math.isnan(value)) else np.nan
            r[name] = v
            pv[name] = {"sources": list(sources), "market_ts": {x: st[x]["market_ts"] for x in sources},
                        "status": {x: st[x]["status"] for x in sources}, "age_min": {x: st[x]["age_min"] for x in sources},
                        "missing": bool(np.isnan(v))}
        for name, col in OWN.items():
            put(f"nq_{name}", nq.loc[day, col], ["NQ"])
        if "ES" in frames:
            for name in ("ret_on", "ret_pm", "ret_30"):
                put(f"es_{name}", frames["ES"].loc[day, name], ["ES"])
        if "RTY" in frames:
            for name in ("ret_on", "ret_pm"):
                put(f"rty_{name}", frames["RTY"].loc[day, name], ["RTY"])
        if "VIX" in frames:
            put("vix_chg_on", frames["VIX"].loc[day, "ret_on"], ["VIX"])
            put("vix_level", frames["VIX"].loc[day, "level_z"], ["VIX"])
        if "10Y" in frames:
            put("y10_chg_on", frames["10Y"].loc[day, "ret_on"], ["10Y"])
        if "DX" in frames:
            put("dx_ret_on", frames["DX"].loc[day, "ret_on"], ["DX"])
        if "ES" in frames:
            put("nq_es_resid_on", resid_on.loc[day], ["NQ", "ES"])
            put("nq_es_resid_pm", resid_pm.loc[day], ["NQ", "ES"])
            put("nq_es_corr", corr.loc[day], ["NQ", "ES"])
        eq = [x for x in ("NQ", "ES", "RTY") if x in frames]
        raw = [frames[x].loc[day, "ret_on_raw"] for x in eq]
        zs = [frames[x].loc[day, "ret_on"] for x in eq]
        ok_raw = [v for v in raw if not np.isnan(v)]
        ok_z = [v for v in zs if not np.isnan(v)]
        put("eq_agree", float(np.mean(np.sign(ok_raw))) if len(ok_raw) >= 2 else np.nan, eq)
        put("eq_dispersion", float(np.std(ok_z)) if len(ok_z) >= 2 else np.nan, eq)
        if "RTY" in frames:
            put("nq_rty_spread_pm", nq.loc[day, "ret_pm"] - frames["RTY"].loc[day, "ret_pm"], ["NQ", "RTY"])
        pre, sess = _events((snapshots or {}).get(day), s, cutoff)
        r["event_preopen"], r["event_session"] = pre, sess
        pv["event_preopen"] = pv["event_session"] = {"sources": ["snapshot events"], "missing": bool(np.isnan(pre))}
        for f in ml.INDICATOR_FEATURES:
            sym = f.sources[0]
            mine = [k for k, v in pv.items() if sym in v.get("sources", []) and len(v["sources"]) == 1]
            r[f.name] = float(any(np.isnan(r[k]) for k in mine)) if mine else 1.0
            pv[f.name] = {"sources": [sym], "missing": False}
        rows[day], prov[day] = r, pv
    own = {sym: frames[sym][[c for c in OWN.values()]].rename(columns={v: k for k, v in OWN.items()})
           for sym in symbols}
    return FeatureSet(rows=rows, provenance=prov, instruments=inst, own=own, observations=obs)


def matrix(fs: FeatureSet, days: Sequence[str], config: str) -> pd.DataFrame:
    """The config's feature columns (contracts/nq_ml.CONFIGS) for NQ on ``days``, NaN where missing - the pooled
    config's NQ rows take NQ's own features under their unprefixed names and is_es = is_rty = 0."""
    cols = ml.CONFIGS[config]
    if config == "pooled":
        own = {f.name for f in ml.OWN_FEATURES}
        get = lambda d, c: (fs.rows[d].get(f"nq_{c}", np.nan) if c in own else 0.0 if c.startswith("is_")  # noqa
                            else fs.rows[d].get(c, np.nan))
        return pd.DataFrame([[get(d, c) for c in cols] for d in days], index=list(days), columns=cols)
    return pd.DataFrame([[fs.rows[d].get(c, np.nan) for c in cols] for d in days], index=list(days), columns=cols)


def required_missing(fs: FeatureSet, day: str) -> List[str]:
    """The required context instruments (contracts/nq_ml.Instrument.required) whose value is not usable on ``day``."""
    return [s for s, inst in ml.INSTRUMENTS.items()
            if inst.required and (fs.instruments[day].get(s) or {}).get("status") not in USED]


# --------------------------------------------------------------------------
# The pooled candidate's labels: each instrument's own direction_15m
# --------------------------------------------------------------------------

def own_direction(ib: InstrumentBars, session: cal.Session, cutoff: datetime) -> Optional[str]:
    """``ib``'s direction_15m on ``session`` (contracts/nq_ml.POOLED): the 09:44 bar's close minus the 09:30 open
    against 0.5 x its two-minute ATR frozen at the cutoff - None when an input is missing."""
    from features.nq_evidence import _two_minute_atr, aggregate
    day = session.session_date.isoformat()
    frame = ib.days.get((ib.active.get(day), day))
    if frame is None or frame.empty:
        return None
    o_at = session.rth_open_at
    c_at = o_at + timedelta(minutes=14)
    by_start = {_utc(t): i for i, t in enumerate(frame["start"])}
    if o_at not in by_start or c_at not in by_start:
        return None
    before = frame[frame["start"] < pd.Timestamp(cutoff)]
    bars = [(_utc(t), o, h, l, c, v) for t, o, h, l, c, v in
            zip(before["start"], before["open"], before["high"], before["low"], before["close"], before["volume"])]
    atr = _two_minute_atr(aggregate(bars, 2, cutoff), cutoff)
    if atr.get("status") != "valid":
        return None
    t = 0.5 * float(atr["exact"])
    move = float(frame.iloc[by_start[c_at]]["close"]) - float(frame.iloc[by_start[o_at]]["open"])
    return "bullish" if move > t else "bearish" if move < -t else "neutral_band"
