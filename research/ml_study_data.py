# research/ml_study_data.py
"""
The data of ml_study_v1 (research/ml_study.py), read from the database read-only.

  connect()            a read-only connection (default_transaction_read_only): the study never writes
  preopen(conn)        the pre-open task: the research_0929 pool's dataset exactly as the development
                       comparison builds it (forecaster/ml_train.dataset), the stored A and B runs, and
                       the frozen N, M and P walk-forward refits (forecaster/ml_eval._fold) - so the
                       development comparison is reproduced on identical rows
  rth_sessions(conn)   the RTH task's sessions: NQ, ES, RTY and VXN 1m bars of every session from
                       RTH_FIRST, minute-indexed from the open on each session's active contract, with
                       the as-of statistics each session's features are normalised by (earlier sessions
                       only)
  rth_table(...)       one row per session and cutoff: the NQ state (G0) and intermarket (G1) features
                       as of the cutoff, the two-minute ATR frozen at the cutoff, and per horizon and
                       origin the window's move and label - or why there is none
  rth_problem(...)     one horizon and origin as a research/ml_study.Problem
  analogue_arms(...)   B_rth and B_rth_recent: nq_match_rth_v3's RTH-20 at each cutoff of the test
                       sessions (the whole earlier pool re-scored; the recent-path variant beside it)

A feature reads only bars that ended by the cutoff and earlier sessions; a label reads only the bars
of its window. Nothing missing is filled in: a missing input is NaN (the models impute it, fitted on
the training rows), a missing label bar leaves the row without a label, counted by reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from contracts import nq_rth as rth
from features import calendar as cal
from forecaster import ml_features as mf
from research import ml_study as st

RTH_FIRST = "2025-07-08"          # twenty sessions after the IB history floor (2025-06-09) for the as-of statistics
HISTORY_FIRST = "2025-06-09"
AS_OF_SESSIONS = 20               # 1m volatility, volume profile, beta: the previous 20 sessions (at least 10)
ATR_SESSIONS = 14                 # the daily range: the previous 14 sessions' RTH ranges (at least 10)
MIN_SESSIONS = 10
PRE = 1440                        # minute arrays start this long before the open (the whole Globex day)
STALE_MINUTES = 5                 # an intermarket value older than this at the cutoff is missing
RTH_SYMBOLS = ("NQ", "ES", "RTY", "VXN")


def connect():
    from config import Config
    from database.connection import get_db_connection
    conn = get_db_connection(Config.DATABASE_URL)
    conn.execute("SET default_transaction_read_only = on")
    return conn


# --------------------------------------------------------------------------
# The pre-open task
# --------------------------------------------------------------------------

@dataclass
class Preopen:
    data: Any                                    # forecaster/ml_train.Data
    problem: st.Problem
    stored: Dict[str, Dict[str, np.ndarray]]     # A, B: day -> probabilities (their first issued replay run)
    frozen: Dict[str, Dict[str, np.ndarray]]     # nq_only/logit (N), multi/logit (M), pooled/gbm (P), ... by day
    frozen_log: List[Dict[str, Any]]
    abstain: List[str]


def preopen(conn, jobs: int = 8) -> Preopen:
    from joblib import Parallel, delayed
    from forecaster import ml_eval
    from forecaster.ml_train import dataset
    data = dataset(conn)
    days, y = data.days, data.y
    X = data.X["multi"].copy()
    cls = {c: i for i, c in enumerate(st.CLASSES)}
    yy = np.array([cls[y[d]] if isinstance(y[d], str) else -1 for d in days])
    problem = st.Problem(
        name="preopen", sessions=list(days), row_session=np.arange(len(days)), keys=np.zeros(len(days), int),
        phase=np.zeros(len(days), int), X=X, y=yy, feature_sets=st.PREOPEN_FEATURES, cp_grid=st.CP_GRID_PREOPEN,
        cp_context={"nq_rv_on": X["nq_rv_on"].to_numpy(float), "abs_gap": np.abs(X["nq_gap_atr"].to_numpy(float)),
                    "vix_level": X["vix_level"].to_numpy(float)})
    stored = {arm: ml_eval.stored_arm(conn, data.snaps, alg) for arm, alg in ml_eval.ARMS_A_B.items()}
    abstain = {d for d in days if mf.required_missing(data.fs, d)}
    plan = ml_eval.folds(len(days))
    done = Parallel(n_jobs=min(jobs, len(plan)))(
        delayed(ml_eval._fold)(data.X, data.PX, y, data.py, abstain, days, *f) for f in plan)
    frozen: Dict[str, Dict[str, np.ndarray]] = {}
    log = []
    for preds, entry in done:
        for arm, got in preds.items():
            frozen.setdefault(arm, {}).update(got)
        log.append(entry)
    return Preopen(data, problem, stored, frozen, log, sorted(abstain))


# --------------------------------------------------------------------------
# The RTH task: bars and as-of statistics
# --------------------------------------------------------------------------

@dataclass
class Minutes:
    """One instrument's bars of one session on its active contract, by minute offset from the RTH open (index
    offset + PRE), NaN where no bar is stored."""
    cid: Optional[int]
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray

    def at(self, k: int) -> float:
        """The close of the bar starting k minutes after the open (ending at k + 1)."""
        i = k + PRE
        return float(self.c[i]) if 0 <= i < len(self.c) else float("nan")


@dataclass
class SessionData:
    day: str
    session: cal.Session
    L: int                                    # scheduled RTH minutes (390, 210 on an early close)
    bars: Dict[str, Minutes]
    prev_close: float = float("nan")          # NQ: the previous session's RTH close, high and low, same contract
    prev_high: float = float("nan")
    prev_low: float = float("nan")
    buckets: Optional[Tuple[np.ndarray, ...]] = None    # NQ 2m buckets of the trading day: start, h, l, c, complete
    asof: Dict[str, Any] = field(default_factory=dict)  # statistics from earlier sessions only


def _minutes(frame: Optional[pd.DataFrame], open_at: datetime, L: int, cid: Optional[int]) -> Minutes:
    n = PRE + L
    arr = {k: np.full(n, np.nan) for k in ("o", "h", "l", "c", "v")}
    if frame is not None and not frame.empty:
        off = ((frame["start"] - pd.Timestamp(open_at)).dt.total_seconds() // 60).astype(int).to_numpy() + PRE
        ok = (off >= 0) & (off < n)
        for k, col in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume")):
            arr[k][off[ok]] = frame[col].to_numpy(float)[ok]
    return Minutes(cid, arr["o"], arr["h"], arr["l"], arr["c"], arr["v"])


def _buckets(frame: Optional[pd.DataFrame]) -> Optional[Tuple[np.ndarray, ...]]:
    """The trading day's clock-aligned 2m buckets (features/nq_evidence.aggregate): start (epoch minutes), high, low,
    close, complete (both minutes present)."""
    if frame is None or frame.empty:
        return None
    em = ((frame["start"] - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(minutes=1)).to_numpy(np.int64)
    b = em - em % 2
    starts, idx = np.unique(b, return_inverse=True)
    hi = np.full(len(starts), -np.inf)
    lo = np.full(len(starts), np.inf)
    np.maximum.at(hi, idx, frame["high"].to_numpy(float))
    np.minimum.at(lo, idx, frame["low"].to_numpy(float))
    cnt = np.bincount(idx, minlength=len(starts))
    last = np.zeros(len(starts), np.int64)
    np.maximum.at(last, idx, np.arange(len(em)))
    close = frame["close"].to_numpy(float)[last]
    return starts, hi, lo, close, cnt == 2


def buckets_from_minutes(mn: "Minutes", open_at: datetime) -> Optional[Tuple[np.ndarray, ...]]:
    """_buckets from a session's minute arrays (the bars from PRE minutes before the open to the RTH close)."""
    ok = ~np.isnan(mn.c)
    if not ok.any():
        return None
    off = np.flatnonzero(ok) - PRE
    start = pd.to_datetime((int(open_at.timestamp()) // 60 + off) * 60, unit="s", utc=True)
    return _buckets(pd.DataFrame({"start": start, "high": mn.h[ok], "low": mn.l[ok], "close": mn.c[ok]}))


def atr2m(sd: SessionData, cutoff_at: datetime) -> float:
    """The two-minute ATR frozen at the cutoff (features/nq_evidence._two_minute_atr): Wilder's 14 over the true
    ranges of the last 71 complete, consecutive 2m buckets ending by the cutoff; NaN when they are not there."""
    if sd.buckets is None:
        return float("nan")
    starts, hi, lo, close, complete = sd.buckets
    end_min = int(cutoff_at.timestamp()) // 60
    j = int(np.searchsorted(starts + 2, end_min, side="right")) - 1        # the last bucket ending by the cutoff
    last = end_min - 2 - (end_min - 2) % 2
    period, window = 14, 70
    if j < window or starts[j] != last:
        return float("nan")
    s = slice(j - window, j + 1)
    if not complete[s].all() or not (np.diff(starts[s]) == 2).all():
        return float("nan")
    h, l, c = hi[s], lo[s], close[s]
    tr = np.maximum(h[1:], c[:-1]) - np.minimum(l[1:], c[:-1])
    atr = tr[:period].mean()
    for x in tr[period:]:
        atr = (atr * (period - 1) + x) / period
    return float(atr)


def rth_sessions(conn, first: str = RTH_FIRST, last: Optional[str] = None,
                 loaded: Optional[Dict[str, mf.InstrumentBars]] = None) -> List[SessionData]:
    """Every scheduled session from HISTORY_FIRST to ``last`` with NQ RTH bars, with its as-of statistics; the
    sessions from ``first`` on are the task's (the earlier ones only feed the statistics)."""
    last = last or conn.execute("SELECT max(trading_day)::text FROM bars b JOIN contracts k USING (contract_id) "
                                "WHERE k.symbol = 'NQ' AND b.interval = '1m';").fetchone()[0]
    sessions = [s for s in cal.sessions_between(HISTORY_FIRST, last) if s.is_open]
    bars = loaded or mf.load(conn, RTH_SYMBOLS, (sessions[0].session_date - timedelta(days=5)).isoformat(), last)
    out: List[SessionData] = []
    for s in sessions:
        day = s.session_date.isoformat()
        L = rth.session_minutes(s)
        m = {}
        for sym in RTH_SYMBOLS:
            ib = bars[sym]
            cid = ib.active.get(day)
            m[sym] = _minutes(ib.days.get((cid, day)) if cid is not None else None, s.rth_open_at, L, cid)
        nq = m["NQ"]
        if np.isnan(nq.c[PRE:PRE + L]).all():
            continue
        sd = SessionData(day, s, L, m, buckets=_buckets(bars["NQ"].days.get((nq.cid, day))))
        if out:                                      # the previous session's RTH on today's contract (a roll's warm-up)
            p = out[-1]
            pf = bars["NQ"].days.get((nq.cid, p.day))
            pm = _minutes(pf, p.session.rth_open_at, p.L, nq.cid)
            rc = pm.c[PRE:PRE + p.L]
            if not np.isnan(rc[-1]):
                sd.prev_close = float(rc[-1])
            if not np.isnan(rc).all():
                sd.prev_high = float(np.nanmax(pm.h[PRE:PRE + p.L]))
                sd.prev_low = float(np.nanmin(pm.l[PRE:PRE + p.L]))
        out.append(sd)
    _asof(out)
    return [sd for sd in out if sd.day >= first]


def _rth(mn: Minutes, L: int, log: bool = False) -> np.ndarray:
    c = mn.c[PRE:PRE + L]
    return np.log(c) if log else c


def _asof(sessions: List[SessionData]) -> None:
    """Each session's statistics from the sessions before it only: NQ's 1m RMS change (points), the other
    instruments' 1m RMS log change, the daily range, the cumulative volume profile, NQ's beta on ES."""
    per = []
    for sd in sessions:
        L = sd.L
        st_: Dict[str, Any] = {}
        for sym in RTH_SYMBOLS:
            x = _rth(sd.bars[sym], L, log=sym != "NQ")
            d = np.diff(x)
            ok = ~np.isnan(d)
            st_[f"ss_{sym}"], st_[f"n_{sym}"] = float((d[ok] ** 2).sum()), int(ok.sum())
        nq = sd.bars["NQ"]
        rh, rl = nq.h[PRE:PRE + L], nq.l[PRE:PRE + L]
        st_["range"] = float(np.nanmax(rh) - np.nanmin(rl)) if (~np.isnan(rh)).sum() >= 30 else np.nan
        v = np.nan_to_num(nq.v[PRE:PRE + L])
        st_["cumvol"] = np.cumsum(v)
        a, b = np.diff(np.log(_rth(nq, L))), np.diff(_rth(sd.bars["ES"], L, log=True))
        ok = ~np.isnan(a) & ~np.isnan(b)
        st_["pairs"] = (a[ok], b[ok])
        per.append(st_)
    for i, sd in enumerate(sessions):
        prev = per[max(0, i - AS_OF_SESSIONS):i]
        a: Dict[str, Any] = {}
        for sym in RTH_SYMBOLS:
            n = sum(p[f"n_{sym}"] for p in prev)
            a[f"sigma_{sym}"] = math.sqrt(sum(p[f"ss_{sym}"] for p in prev) / n) if len(prev) >= MIN_SESSIONS and n \
                else float("nan")
        ranges = [p["range"] for p in per[max(0, i - ATR_SESSIONS):i] if not np.isnan(p["range"])]
        a["atr_d"] = float(np.mean(ranges)) if len(ranges) >= MIN_SESSIONS else float("nan")
        base = np.full(rth.SESSION_MAX_MINUTES, np.nan)           # [k - 1]: the mean volume of the first k minutes
        for k in range(1, rth.SESSION_MAX_MINUTES + 1):
            vols = [p["cumvol"][k - 1] for p in prev if len(p["cumvol"]) >= k]
            if len(vols) >= MIN_SESSIONS:
                base[k - 1] = float(np.mean(vols))
        a["volbase"] = base
        if len(prev) >= MIN_SESSIONS:
            na = np.concatenate([p["pairs"][0] for p in prev])
            ea = np.concatenate([p["pairs"][1] for p in prev])
            if len(ea) > 100 and ea.var() > 0:
                beta = float(np.cov(na, ea)[0, 1] / ea.var(ddof=1))
                a["beta"], a["sd_resid"] = beta, float((na - beta * ea).std(ddof=1))
        sd.asof = a


# --------------------------------------------------------------------------
# The RTH task: rows, features, labels
# --------------------------------------------------------------------------

def _events(conn) -> np.ndarray:
    rows = conn.execute("SELECT extract(epoch FROM scheduled_at)::bigint FROM economic_events WHERE tier = 'high' "
                        "ORDER BY 1;").fetchall()
    return np.array([r[0] for r in rows], dtype=np.int64)


def _any_between(events: np.ndarray, a: datetime, b: datetime) -> float:
    """1 when a high-tier event is scheduled in [a, b)."""
    i = np.searchsorted(events, int(a.timestamp()), side="left")
    return float(i < len(events) and events[i] < int(b.timestamp()))


def _value(mn: Minutes, k: int, limit: int = STALE_MINUTES) -> Tuple[float, Optional[int]]:
    """The close of the newest bar ending by minute k after the open (bar k - 1 or earlier) within ``limit``
    minutes; ``(value, age in minutes)`` or (nan, None)."""
    for age in range(0, limit + 1):
        x = mn.at(k - 1 - age)
        if not np.isnan(x):
            return x, age
    return float("nan"), None


def _rms(x: np.ndarray) -> float:
    d = np.diff(x)
    d = d[~np.isnan(d)]
    return float(np.sqrt((d ** 2).mean())) if len(d) >= 5 else float("nan")


def features_at(sd: SessionData, c: int, events: np.ndarray) -> Dict[str, Any]:
    """The features of ``sd`` at the cutoff ``c`` minutes after the open: only bars ending by the cutoff (the bar
    starting at c - 1 and earlier) and earlier sessions' statistics."""
    nq, a = sd.bars["NQ"], sd.asof
    t_c = sd.session.rth_open_at + timedelta(minutes=c)
    p = nq.at(c - 1)
    sig, atr = a.get("sigma_NQ", np.nan), a.get("atr_d", np.nan)
    f: Dict[str, Any] = {"cutoff_bar": not np.isnan(p)}
    o0 = float(nq.o[PRE])
    for k in (5, 15, 30, 60):
        f[f"ret_{k}"] = (p - nq.at(c - 1 - k)) / (sig * math.sqrt(k))
    f["ret_open"] = (p - o0) / (sig * math.sqrt(c))
    f["ret_on"] = (o0 - sd.prev_close) / atr
    seg30 = nq.c[PRE + c - 31:PRE + c]
    f["rv_30"] = math.log(_rms(seg30) / sig) if _rms(seg30) > 0 else np.nan
    seg = nq.c[PRE:PRE + c]
    f["rv_open"] = math.log(_rms(seg) / sig) if _rms(seg) > 0 else np.nan
    steps = np.abs(np.diff(seg30))
    f["eff_30"] = ((p - seg30[0]) / np.nansum(steps)) if np.nansum(steps) > 0 and not np.isnan(seg30[0]) else np.nan
    hlc3 = (nq.h[:PRE + c] + nq.l[:PRE + c] + nq.c[:PRE + c]) / 3
    vol = nq.v[:PRE + c]
    ok = ~np.isnan(hlc3) & ~np.isnan(vol)
    f["vwap_dist"] = ((p - (hlc3[ok] * vol[ok]).sum() / vol[ok].sum()) / atr) if vol[ok].sum() > 0 else np.nan
    hi, lo = np.nanmax(nq.h[PRE:PRE + c]), np.nanmin(nq.l[PRE:PRE + c])
    f["range_pos"] = ((p - (hi + lo) / 2) / (hi - lo)) if hi > lo else np.nan
    f["range_sofar"] = (hi - lo) / atr
    f["dist_prev_high"] = (p - sd.prev_high) / atr
    f["dist_prev_low"] = (p - sd.prev_low) / atr
    base = a["volbase"][c - 1] if "volbase" in a else np.nan
    cum = np.nansum(nq.v[PRE:PRE + c])
    f["rel_vol"] = math.log(cum / base) if base and base > 0 and cum > 0 else np.nan
    f["elapsed"], f["to_close"] = float(c), float(sd.L - c)
    f["ev_recent"] = _any_between(events, t_c - timedelta(minutes=30), t_c)
    # intermarket: the newest bar ending by the cutoff within STALE_MINUTES, else missing (flagged)
    es, es_age = _value(sd.bars["ES"], c)
    es15, _ = _value(sd.bars["ES"], c - 15)
    rty, rty_age = _value(sd.bars["RTY"], c)
    vxn, vxn_age = _value(sd.bars["VXN"], c)
    es0, rty0 = float(sd.bars["ES"].o[PRE]), float(sd.bars["RTY"].o[PRE])
    vxn0 = next((x for x in (sd.bars["VXN"].at(0), sd.bars["VXN"].at(1)) if not np.isnan(x)), np.nan)
    f["es_ret_15"] = (math.log(es / es15) / (a["sigma_ES"] * math.sqrt(15))) if es > 0 and es15 > 0 else np.nan
    if "beta" in a and es > 0 and es0 > 0 and p > 0 and o0 > 0:
        f["resid_open"] = (math.log(p / o0) - a["beta"] * math.log(es / es0)) / (a["sd_resid"] * math.sqrt(c))
    else:
        f["resid_open"] = np.nan
    f["rty_ret_open"] = (math.log(rty / rty0) / (a["sigma_RTY"] * math.sqrt(c))) if rty > 0 and rty0 > 0 else np.nan
    f["vxn_chg"] = (math.log(vxn / vxn0) / (a["sigma_VXN"] * math.sqrt(c))) if vxn > 0 and vxn0 > 0 else np.nan
    f["es_missing"] = float(np.isnan(f["es_ret_15"]) or np.isnan(f["resid_open"]))
    f["rty_missing"] = float(np.isnan(f["rty_ret_open"]))
    f["vxn_missing"] = float(np.isnan(f["vxn_chg"]))
    f["es_age"], f["rty_age"], f["vxn_age"] = es_age, rty_age, vxn_age
    f["atr2m"] = atr2m(sd, t_c)
    f["sigma"] = sig
    return {k: (float(v) if isinstance(v, (int, float, np.floating)) and not isinstance(v, bool) else v)
            for k, v in f.items()}


def label_at(sd: SessionData, c: int, h: int, origin: str, band_atr2m: float) -> Dict[str, Any]:
    """The window [S, S + h) of the cutoff ``c`` (S = c + the origin's offset): its move, band and label - or the
    reason there is none."""
    S = c + st.RTH_ORIGINS[origin]
    out = {"S": S, "move": np.nan, "band": np.nan, "label": -1, "reason": None}
    if S + h > sd.L:
        out["reason"] = "crosses_close"
        return out
    nq = sd.bars["NQ"]
    if np.isnan(nq.at(c - 1)):
        out["reason"] = "no_cutoff_bar"
        return out
    if np.isnan(nq.c[PRE + S - 1:PRE + S + h]).any():          # every bar S - 1 .. S + h - 1 (rth_eval.window_move)
        out["reason"] = "window_bar_missing"
        return out
    a, b = nq.at(S - 1), nq.at(S + h - 1)
    if not band_atr2m > 0:
        out["reason"] = "no_band"
        return out
    band = 0.5 * band_atr2m * math.sqrt(h / 15)
    move = b - a
    out.update(move=move, band=band, label=1 if move > band else 0 if move < -band else 2, reason="ok")
    return out


def rth_table(conn, sessions: List[SessionData]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """``(features, labels)``: one feature row per session and cutoff the session reaches; one label row per
    session, cutoff, horizon and origin whose window fits the session."""
    events = _events(conn)
    frows, lrows = [], []
    for sd in sessions:
        for c in st.RTH_CUTOFFS:
            if c >= sd.L:
                continue
            f = features_at(sd, c, events)
            frows.append({"session": sd.day, "cutoff": c, **f})
            for h in st.RTH_HORIZONS:
                for origin in st.RTH_ORIGINS:
                    lab = label_at(sd, c, h, origin, f["atr2m"])
                    if lab["reason"] == "crosses_close":
                        continue
                    t_c = sd.session.rth_open_at + timedelta(minutes=c)
                    t_end = sd.session.rth_open_at + timedelta(minutes=lab["S"] + h)
                    lrows.append({"session": sd.day, "cutoff": c, "h": h, "origin": origin, **lab,
                                  "ev_ahead": _any_between(events, t_c, t_end),
                                  "z": lab["move"] / (f["sigma"] * math.sqrt(h)) if f["sigma"] else np.nan})
    return pd.DataFrame(frows), pd.DataFrame(lrows)


def phase_of(c: int) -> int:
    for i, (a, b) in enumerate(st.RTH_PHASES.values()):
        if a <= c <= b:
            return i
    return -1


def rth_problem(features: pd.DataFrame, labels: pd.DataFrame, h: int, origin: str
                ) -> Tuple[st.Problem, pd.DataFrame]:
    """The horizon and origin as a Problem, and its rows (session then cutoff order) with every column."""
    lab = labels[(labels["h"] == h) & (labels["origin"] == origin)]
    rows = lab.merge(features, on=["session", "cutoff"], how="left", validate="one_to_one")
    rows = rows.sort_values(["session", "cutoff"]).reset_index(drop=True)
    sessions = sorted(rows["session"].unique())
    pos = {d: i for i, d in enumerate(sessions)}
    X = rows[st.RTH_FEATURES["multi"]].astype(float)
    return st.Problem(
        name=f"rth/h{h}/{origin}", sessions=sessions, row_session=rows["session"].map(pos).to_numpy(),
        keys=rows["cutoff"].to_numpy(), phase=rows["cutoff"].map(phase_of).to_numpy(), X=X,
        y=rows["label"].to_numpy(int), feature_sets=st.RTH_FEATURES, cp_grid=st.CP_GRID_RTH,
        cp_context={"rv_30": rows["rv_30"].to_numpy(float), "rv_open": rows["rv_open"].to_numpy(float)}), rows


# --------------------------------------------------------------------------
# The RTH analogues (B_rth, B_rth_recent)
# --------------------------------------------------------------------------

RECENT_MINUTES = 30
B_MEMBERS = 20
B_MIN_MEMBERS = 10


def openings(conn, last_day: str, cache_dir: str) -> Dict[str, Any]:
    """nq_match_rth_v3's openings to ``last_day`` (forecaster/rth_analogues.load_session_openings), its cache in the
    research directory - never production's."""
    from forecaster import rth_analogues as ra
    ra.CACHE_DIR = cache_dir
    ops, _ = ra.load_session_openings(conn, last_day, cache=True)
    return ops


def _recent_path(op, m: int, w: int = RECENT_MINUTES) -> Optional[np.ndarray]:
    if op.context is None or not op.context.atr or op.minutes < m or m <= w:
        return None
    c = np.array([b[4] for b in op.bars[m - w - 1:m]], float)
    return (c[1:] - c[0]) / op.context.atr


def analogue_forecast(target, pool: Sequence[Any], m: int, windows: Sequence[Tuple[int, int, float]]
                      ) -> Dict[str, Any]:
    """At ``m`` minutes: the v3 ranking of ``pool`` for ``target`` truncated to its first m bars; per window
    (S, h, band in the target's daily-ATR units) the probabilities of RTH-20 and of the recent-path variant
    (Laplace-smoothed member class counts), None with fewer than B_MIN_MEMBERS members holding the window."""
    from matching import rth as mr
    t = replace(target, bars=target.bars[:m])
    ranked = mr.rank(t, pool, m)
    ordered = ranked["ordered"]
    tp = _recent_path(target, m)
    tol = mr.tolerance("path", RECENT_MINUTES)
    combo = []
    for r in ordered:
        op = r["opening"]
        rp = _recent_path(op, m)
        sim_r = 0.0 if rp is None or tp is None else max(0.0, 1 - math.sqrt(float(((tp - rp) ** 2).mean())) / tol)
        combo.append((0.5 * r["similarity"] + 50.0 * sim_r, r))
    combo.sort(key=lambda x: (-round(x[0], 9), -x[1]["comparable_weight"], -int(x[1]["opening"].session_date
                                                                                 .replace("-", ""))))
    out = {"pool_size": ranked["pool_size"], "candidates": len(ordered), "windows": {}}
    for S, h, thr in windows:
        res = {}
        for name, members in (("B_rth", [r["opening"] for r in ordered]),
                              ("B_rth_recent", [r["opening"] for _, r in combo])):
            counts, n = np.zeros(st.K), 0
            for op in members:
                if n >= B_MEMBERS:
                    break
                if op.minutes < S + h or not op.context or not op.context.atr:
                    continue
                mv = (op.bars[S + h - 1][4] - op.bars[S - 1][4]) / op.context.atr
                counts[1 if mv > thr else 0 if mv < -thr else 2] += 1
                n += 1
            res[name] = None if n < B_MIN_MEMBERS else ((counts + 1) / (n + st.K)).tolist()
            res[f"{name}_n"] = n
        out["windows"][(S, h)] = res
    return out
