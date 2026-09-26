# features/nq_v2.py
"""
Builds one nq_features_v2 snapshot: every feature in features/catalogue.py,
its status, the per-source status, and the manifest its source revision is
hashed from. See docs/forecast_contract_v2.md for the full contract.

Time rules, all enforced here:

  * T (cutoff_at) is 09:29:00 ET. Only bars with bar_end_at <= T are read, so
    the latest input bar starts at 09:28. P is the close of exactly that bar;
    if it is missing, P - and everything built on it - is null.
  * Cprev / PDH / PDL / prior open come from the previous *scheduled* RTH
    session's minute bars, [09:30, scheduled close): the 15:59 close normally,
    12:59 on an early-close day.
  * A (daily ATR14) runs through the previous session; no bar of the current
    RTH session is ever read.

Quantities travel as ``Q(value, status)``. A derived feature is valid only when
all its inputs are; otherwise it inherits the first failing input's status, so
"why is this null" is always recorded.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np

from config import ASSET_SOURCES, INSTRUMENTS, AssetSource
from features import calendar as cal
from features import catalogue as catv2
from features.indicators import (
    aggregate_clock,
    ema_trailing,
    finite,
    path_efficiency,
    ratio,
    true_ranges,
    vwap_hlc3,
    wilder_atr_trailing,
    zscore,
)

logger = logging.getLogger(__name__)

ONE_MIN = timedelta(minutes=1)
P = catv2.PARAMETERS
WARMUP = P["warmup_multiple"]
NQ = catv2.TARGET_SYMBOL

# Intermarket assets computed with the generic latest-eligible rule, and the
# feature each value kind produces. NQ itself uses P and Cprev directly.
_ASSET_ORDER = ("es", "rty", "vix", "vxn", "us10y", "us2y", "dxy", "smh",
                "dx_fut", "us10y_yield_fut", "us2y_yield_fut", "gc", "cl")


@dataclass(frozen=True)
class Q:
    value: Any
    status: str = "valid"

    @property
    def ok(self):
        return self.status == "valid"


def _q(value, fail="undefined") -> Q:
    """A computed value: valid if it is a finite number (or a bool/str), else ``fail``."""
    if isinstance(value, (bool, str)):
        return Q(value)
    v = finite(value)
    return Q(v) if v is not None else Q(None, fail)


def _bad(status: str) -> Q:
    return Q(None, status)


def derive(fn, *inputs: Q) -> Q:
    for q in inputs:
        if not q.ok:
            return _bad(q.status)
    return _q(fn(*(q.value for q in inputs)))


def _iso(ts) -> Optional[str]:
    if ts is None:
        return None
    ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SnapshotResult:
    session_date: str
    instrument_id: int
    cutoff_at: datetime
    features_frozen_at: datetime
    data_mode: str
    pit_availability_status: str
    feature_version: str
    session_schedule: str
    scheduled_close_at: Optional[datetime]
    source_status: Dict[str, Any]
    feature_status: Dict[str, str]
    data_quality_status: str
    reference_values: Dict[str, Any]
    features: Dict[str, Any]
    manifest: Dict[str, Any] = field(repr=False)
    source_revision_id: str = ""


class SnapshotError(RuntimeError):
    """The snapshot cannot be built at all (not merely with missing features)."""


class SnapshotBuilder:
    def __init__(self, md, session_date, data_mode: str = "historical_reconstruction",
                 frozen_at: Optional[datetime] = None, sources: Dict[str, AssetSource] = None):
        if data_mode not in catv2.DATA_MODES:
            raise ValueError(f"data_mode must be one of {catv2.DATA_MODES}")
        self.md = md
        self.sess = cal.session(session_date)
        if not self.sess.is_open:
            raise SnapshotError(f"{session_date} is a closed session; there is no open to forecast.")
        self.prev = cal.previous_session(self.sess.session_date)
        self.T = self.sess.cutoff_at
        self.data_mode = data_mode
        self.frozen_at = frozen_at
        self.sources = sources or ASSET_SOURCES
        self.reads: Dict[str, str] = {}
        self.contracts_used: Dict[str, Dict] = {}
        self.source_status: Dict[str, Any] = {}
        self.ref: Dict[str, Any] = {}
        self.q: Dict[str, Q] = {}
        self._stats: Dict[Any, Dict[str, Q]] = {}
        self._daily: Dict[Any, Optional[Dict]] = {}

    # ------------------------------------------------------------------ reads
    def _bars(self, cid, start, end, pt="TRADES"):
        df, digest, key = self.md.bars(cid, start, end, pt)
        self.reads[key] = digest
        return df

    def contract_for(self, symbol: str, day) -> Optional[int]:
        key = f"{symbol}/{day}"
        if key not in self.contracts_used:
            a = self.md.active_contract(symbol, day)
            if a is not None:
                rec = {"contract_id": int(a["contract_id"]), "rule": a["rule"]}
            else:
                cid = self.md.fallback_contract(symbol)
                rec = {"contract_id": cid,
                       "rule": "fallback: configured contract (no active_contracts row)"}
            self.contracts_used[key] = rec
        return self.contracts_used[key]["contract_id"]

    def daily(self, cid, s: cal.Session) -> Optional[Dict]:
        """RTH OHLC of one scheduled session on one contract, or None if the
        open or close minute is missing or coverage is below the threshold."""
        key = (cid, s.session_date)
        if key not in self._daily:
            df = self._bars(cid, s.rth_open_at, s.scheduled_close_at)
            expected = int((s.scheduled_close_at - s.rth_open_at) / ONE_MIN)
            out = None
            if not df.empty:
                first, last = df.iloc[0], df.iloc[-1]
                if (first["bar_start_at"] == s.rth_open_at
                        and last["bar_start_at"] == s.scheduled_close_at - ONE_MIN
                        and len(df) >= expected * P["rth_daily_min_coverage"]):
                    out = {"open": float(first["open"]), "high": float(df["high"].max()),
                           "low": float(df["low"].min()), "close": float(last["close"]),
                           "bars": len(df), "expected": expected}
            self._daily[key] = out
        return self._daily[key]

    def session_stats(self, cid, s: cal.Session) -> Dict[str, Q]:
        """Pre-open quantities of one session on one contract, from its ON window."""
        key = (cid, s.session_date)
        if key in self._stats:
            return self._stats[key]
        T = s.cutoff_at
        df = self._bars(cid, s.overnight_start_at, T)
        expected = int((T - s.overnight_start_at) / ONE_MIN)
        by_start = dict(zip(df["bar_start_at"], df.index)) if not df.empty else {}

        def close_at(start):
            i = by_start.get(start)
            return Q(float(df.at[i, "close"])) if i is not None else _bad("missing")

        st: Dict[str, Q] = {"P": close_at(T - ONE_MIN),
                            "c0913": close_at(T - 16 * ONE_MIN),
                            "c0828": close_at(T - 61 * ONE_MIN)}

        coverage = len(df) / expected if expected else 0.0
        st["on_coverage"] = Q(coverage)
        if df.empty or coverage < P["overnight_min_coverage"]:
            for k in ("ONH", "ONL", "on_vwap", "on_volume"):
                st[k] = _bad("missing")
        else:
            st["ONH"] = Q(float(df["high"].max()))
            st["ONL"] = Q(float(df["low"].min()))
            st["on_vwap"] = _q(vwap_hlc3(df))
            st["on_volume"] = Q(float(df["volume"].sum()))

        w = df[df["bar_start_at"] >= T - 60 * ONE_MIN] if not df.empty else df
        if len(w) == 60 and st["c0828"].ok:
            st["high60"] = Q(float(w["high"].max()))
            st["low60"] = Q(float(w["low"].min()))
            st["volume60"] = Q(float(w["volume"].sum()))
            st["efficiency60"] = _q(path_efficiency([st["c0828"].value] + w["close"].tolist()))
        else:
            for k in ("high60", "low60", "volume60", "efficiency60"):
                st[k] = _bad("missing")

        # Wilder ATR14 of 1m bars through the 09:28 bar, over a 5n trailing window.
        n = P["intraday_atr_period"]
        if not st["P"].ok:
            st["atr1m"] = _bad("stale" if not df.empty else "missing")
        elif len(df) < WARMUP * n + 1:
            st["atr1m"] = _bad("insufficient_history")
        else:
            tail = df.iloc[-(WARMUP * n + 1):]
            tr = true_ranges(tail["high"].values[1:], tail["low"].values[1:], tail["close"].values[:-1])
            st["atr1m"] = _q(wilder_atr_trailing(tr, n, WARMUP), fail="insufficient_history")

        self._stats[key] = st
        return st

    # --------------------------------------------------------------- building
    def daily_atr(self, n: int) -> Q:
        k = WARMUP * n
        sessions = cal.sessions_before(self.sess.session_date, k + 1)
        if len(sessions) < k + 1:
            return _bad("insufficient_history")
        trs = []
        for i in range(k, 0, -1):          # newest first: fail fast on short history
            s, sp = sessions[i], sessions[i - 1]
            cid = self.contract_for(NQ, s.session_date)
            d = self.daily(cid, s) if cid is not None else None
            dp = self.daily(cid, sp) if d is not None else None
            if d is None or dp is None:
                return _bad("insufficient_history")
            trs.append(float(true_ranges([d["high"]], [d["low"]], [dp["close"]])[0]))
        atr = wilder_atr_trailing(trs[::-1], n, WARMUP)
        if atr is None:
            return _bad("insufficient_history")
        return Q(atr) if atr > 0 else _bad("undefined")

    def baseline(self, name: str, count: int, fn) -> Optional[List[float]]:
        """The most recent ``count`` valid values of ``fn(session)`` over prior
        scheduled sessions (searching back a fixed number), or None."""
        out = []
        for s in reversed(cal.sessions_before(self.sess.session_date, P["baseline_search_sessions"])):
            q = fn(s)
            if q.ok:
                out.append(q.value)
                if len(out) == count:
                    return out
        return None

    def _nq_preopen(self, s: cal.Session) -> Q:
        cid = self.contract_for(NQ, s.session_date)
        if cid is None:
            return _bad("missing")
        prev = cal.previous_session(s.session_date)
        d = self.daily(cid, prev)
        cprev = Q(d["close"]) if d else _bad("missing")
        return derive(lambda p, c: ratio(p, c) - 1 if ratio(p, c) is not None else None,
                      self.session_stats(cid, s)["P"], cprev)

    def asset_observation(self, asset: str, s: cal.Session, record: bool = False) -> Dict[str, Q]:
        """
        Latest eligible value at T and at the previous reference RTH close for
        one logical asset, same contract, under the asset's freshness rule.
        Returns {'now': Q, 'ref': Q} and optionally records source_status.
        """
        src = self.sources.get(asset)
        status = {"asset": asset, "symbol": src.symbol if src else None,
                  "is_proxy": bool(src and src.is_proxy),
                  "max_age_minutes": src.max_age_minutes if src else None}
        if src is None or src.symbol is None or src.max_age_minutes is None:
            status["validity"] = "unmapped"
            if record:
                self.source_status[asset] = status
            return {"now": _bad("missing"), "ref": _bad("missing")}

        inst = INSTRUMENTS[src.symbol]
        pt = inst.what_to_show
        cid = self.contract_for(src.symbol, s.session_date)
        status["contract_id"] = cid
        if cid is None:
            status["validity"] = "no_contract"
            if record:
                self.source_status[asset] = status
            return {"now": _bad("missing"), "ref": _bad("missing")}

        prev = cal.previous_session(s.session_date)
        age = timedelta(minutes=src.max_age_minutes)
        out = {}
        for label, instant, day in (("now", s.cutoff_at, s.session_date),
                                    ("ref", prev.scheduled_close_at, prev.session_date)):
            df = self._bars(cid, instant - ONE_MIN - age, instant, pt)
            info = {"instant": _iso(instant)}
            if not df.empty:
                bar = df.iloc[-1]
                end = bar["bar_start_at"] + ONE_MIN
                info.update(bar_start_at=_iso(bar["bar_start_at"]), bar_end_at=_iso(end),
                            age_minutes=(instant - end) / ONE_MIN, validity="valid")
                out[label] = Q(float(bar["close"]))
            else:
                if record:
                    last = self.md.latest_bar_start(cid, instant, pt)
                    if last is not None:
                        end = last + ONE_MIN
                        info.update(bar_start_at=_iso(last), bar_end_at=_iso(end),
                                    age_minutes=(instant - end) / ONE_MIN)
                info["validity"] = "stale" if info.get("bar_start_at") else "missing"
                out[label] = _bad("stale" if info.get("bar_start_at") else "missing")
            if record:
                info.update(self._availability(cid, day, pt, info.get("bar_start_at"),
                                               out[label].value))
            status["observation" if label == "now" else "reference"] = info

        if record:
            c = self.md.contract(cid) or {}
            status.update(local_symbol=c.get("local_symbol"), expiry=c.get("expiry"),
                          rule=self.contracts_used[f"{src.symbol}/{s.session_date}"]["rule"],
                          value_kind=inst.value_kind, value_unit=inst.value_unit,
                          validity="valid" if out["now"].ok and out["ref"].ok else
                          ("stale" if "stale" in (out["now"].status, out["ref"].status) else "missing"))
            self.source_status[asset] = status
        return out

    def _asset_return(self, asset: str, s: cal.Session, record=False) -> Q:
        o = self.asset_observation(asset, s, record)
        return derive(lambda n, r: ratio(n, r) - 1 if ratio(n, r) is not None else None, o["now"], o["ref"])

    # ----------------------------------------------------------------- output
    def build(self) -> SnapshotResult:
        s, prev, T = self.sess, self.prev, self.T
        q = self.q
        nq = self.contract_for(NQ, s.session_date)
        if nq is None:
            raise SnapshotError(f"No {NQ} contract is known for {s.session_date}; run the collector.")

        st = self.session_stats(nq, s)
        Pq = st["P"]
        d_prev = self.daily(nq, prev)
        cprev, pdh, pdl, popen = ((Q(d_prev[k]) for k in ("close", "high", "low", "open")) if d_prev
                                  else (_bad("missing"),) * 4)
        A = self.daily_atr(14)
        A63 = self.daily_atr(63)

        def over_a(fn, *inputs):
            return derive(lambda *v: ratio(fn(*v[:-1]), v[-1]), *inputs, A)

        # --- NQ ----------------------------------------------------------------
        q["daily_atr_fraction"] = derive(ratio, A, cprev)
        q["daily_volatility_ratio"] = derive(ratio, A, A63)
        q["atr_1m_14_fraction"] = derive(ratio, st["atr1m"], Pq)

        def atr1m_of(x):
            cid = self.contract_for(NQ, x.session_date)
            return self.session_stats(cid, x)["atr1m"] if cid is not None else _bad("missing")

        q["atr_1m_14_relative_30d"] = self._relative(st["atr1m"], "atr_1m_relative_baseline_sessions", atr1m_of)
        q["gap_signed_atr"] = over_a(lambda p, c: p - c, Pq, cprev)
        q["prior_range_position"] = derive(lambda p, lo, hi: ratio(p - lo, hi - lo), Pq, pdl, pdh)
        q["distance_pdh_atr"] = over_a(lambda p, h: p - h, Pq, pdh)
        q["distance_pdl_atr"] = over_a(lambda p, lo: p - lo, Pq, pdl)
        q["distance_onh_atr"] = over_a(lambda p, h: p - h, Pq, st["ONH"])
        q["distance_onl_atr"] = over_a(lambda p, lo: p - lo, Pq, st["ONL"])
        q["overnight_range_atr"] = over_a(lambda h, lo: h - lo, st["ONH"], st["ONL"])
        q["overnight_range_position"] = derive(lambda p, lo, hi: ratio(p - lo, hi - lo), Pq, st["ONL"], st["ONH"])
        q["distance_on_vwap_hlc3_atr"] = over_a(lambda p, v: p - v, Pq, st["on_vwap"])
        q["return_15m_atr"] = over_a(lambda p, c: p - c, Pq, st["c0913"])
        q["return_60m_atr"] = over_a(lambda p, c: p - c, Pq, st["c0828"])
        q["range_60m_atr"] = over_a(lambda h, lo: h - lo, st["high60"], st["low60"])
        q["efficiency_60m"] = st["efficiency60"]
        self._emas(nq, Pq, A)

        def vol_of(key):
            def f(x):
                cid = self.contract_for(NQ, x.session_date)
                return self.session_stats(cid, x)[key] if cid is not None else _bad("missing")
            return f

        q["rvol_overnight_30d"] = self._relative(st["on_volume"], "rvol_baseline_sessions", vol_of("on_volume"))
        q["rvol_60m_30d"] = self._relative(st["volume60"], "rvol_baseline_sessions", vol_of("volume60"))
        q["prior_rth_return_atr"] = over_a(lambda c, o: c - o, cprev, popen)
        q["prior_rth_range_atr"] = over_a(lambda h, lo: h - lo, pdh, pdl)
        q["prior_rth_close_location"] = derive(lambda c, lo, hi: ratio(c - lo, hi - lo), cprev, pdl, pdh)

        # --- Intermarket ---------------------------------------------------------
        self._intermarket(nq, st, Pq, cprev, d_prev)

        # --- Calendar and events -------------------------------------------------
        self._calendar(nq)
        self._events()

        self.ref = {
            "P": Pq.value, "Cprev": cprev.value, "PDH": pdh.value, "PDL": pdl.value,
            "prior_rth_open": popen.value, "A": A.value, "atr63": A63.value,
            "ONH": st["ONH"].value, "ONL": st["ONL"].value, "on_vwap_hlc3": st["on_vwap"].value,
            "on_volume": st["on_volume"].value, "on_coverage": st["on_coverage"].value,
            "close_0913": st["c0913"].value, "close_0828": st["c0828"].value,
            "atr_1m_14": st["atr1m"].value, "volume_60m": st["volume60"].value,
            **{k: v for k, v in self.ref.items()},
        }
        self.source_status[NQ.lower()] = self._nq_status(nq, st, d_prev)
        return self._result(nq)

    def _relative(self, current: Q, param: str, fn) -> Q:
        if not current.ok:
            return current
        base = self.baseline(param, P[param], fn)
        if base is None:
            return _bad("insufficient_history")
        return _q(ratio(current.value, float(np.mean(base))))

    def _emas(self, nq, Pq: Q, A: Q):
        q = self.q
        # Enough 1m history for 5 * 200 complete 5m buckets (~4 Globex sessions),
        # read from the snapshot contract only.
        start = cal.sessions_before(self.sess.session_date, 6)
        start_at = start[0].overnight_start_at if start else self.sess.overnight_start_at
        df = self._bars(nq, start_at, self.T)

        b5 = aggregate_clock(df, 5)
        b5 = b5[b5["complete"] & (b5["bucket_end"] <= self.T)]
        if b5.empty or b5["bucket_end"].iloc[-1] != self.T - 4 * ONE_MIN:
            status = "missing" if b5.empty else "stale"
            q["ema200_distance_5m_atr"] = q["ema9_21_spread_5m_atr"] = _bad(status)
        else:
            closes = b5["close"].tolist()
            e200, e9, e21 = (ema_trailing(closes, n, WARMUP) for n in (200, 9, 21))
            ema200 = Q(e200[-1]) if e200 else _bad("insufficient_history")
            spread = Q(e9[-1] - e21[-1]) if e9 and e21 else _bad("insufficient_history")
            q["ema200_distance_5m_atr"] = derive(lambda p, e, a: ratio(p - e, a), Pq, ema200, A)
            q["ema9_21_spread_5m_atr"] = derive(ratio, spread, A)
            self.ref["ema200_5m"] = ema200.value

        b15 = aggregate_clock(df, 15)
        b15 = b15[b15["complete"] & (b15["bucket_end"] <= self.T)]
        want = [self.T - ONE_MIN * (14 + 15 * i) for i in range(4, -1, -1)]   # 08:15 .. 09:15
        ends = b15["bucket_end"].tolist()
        if len(ends) < 5 or ends[-1] != want[-1]:
            q["ema20_slope_15m_atr"] = _bad("missing" if not ends else "stale")
        elif ends[-5:] != want:
            q["ema20_slope_15m_atr"] = _bad("missing")
        else:
            e20 = ema_trailing(b15["close"].tolist(), 20, WARMUP, tail=5)
            slope = Q(e20[-1] - e20[0]) if e20 else _bad("insufficient_history")
            q["ema20_slope_15m_atr"] = derive(ratio, slope, A)

    def _intermarket(self, nq, st, Pq, cprev, d_prev):
        q = self.q
        s = self.sess
        nq_ret = derive(lambda p, c: ratio(p, c) - 1 if ratio(p, c) is not None else None, Pq, cprev)
        q["nq_preopen_return"] = nq_ret

        values: Dict[str, Dict[str, Q]] = {}
        for asset in _ASSET_ORDER:
            values[asset] = self.asset_observation(asset, s, record=True)

        def ret(asset):
            o = values[asset]
            return derive(lambda n, r: ratio(n, r) - 1 if ratio(n, r) is not None else None, o["now"], o["ref"])

        def bps(asset):
            src = self.sources.get(asset)
            per = INSTRUMENTS[src.symbol].bps_per_unit if src and src.symbol else None
            if per is None:
                return _bad("missing")
            o = values[asset]
            return derive(lambda n, r: (n - r) * per, o["now"], o["ref"])

        for asset in ("es", "rty", "dxy", "smh", "dx_fut", "gc", "cl"):
            q[f"{asset}_preopen_return"] = ret(asset)
        for asset in ("vix", "vxn"):
            q[f"{asset}_level"] = values[asset]["now"]
            q[f"{asset}_change_points"] = derive(lambda n, r: n - r, values[asset]["now"], values[asset]["ref"])
        for asset in ("us10y", "us2y", "us10y_yield_fut", "us2y_yield_fut"):
            q[f"{asset}_change_bps"] = bps(asset)
        q["yield_curve_10y_2y_change_bps"] = derive(lambda a, b: a - b, q["us10y_change_bps"], q["us2y_change_bps"])
        q["yield_fut_curve_10y_2y_change_bps"] = derive(
            lambda a, b: a - b, q["us10y_yield_fut_change_bps"], q["us2y_yield_fut_change_bps"])
        q["nq_es_relative_return"] = derive(lambda a, b: a - b, nq_ret, q["es_preopen_return"])

        # Standardised divergence: each asset against its own prior 60 same-window returns.
        n = P["divergence_baseline_sessions"]
        if nq_ret.ok and q["es_preopen_return"].ok:
            nq_base = self.baseline("divergence_baseline_sessions", n, self._nq_preopen)
            es_base = self.baseline("divergence_baseline_sessions", n,
                                    lambda x: self._asset_return("es", x))
            if nq_base is None or es_base is None:
                q["nq_es_standardized_divergence"] = _bad("insufficient_history")
            else:
                zn, ze = zscore(nq_ret.value, nq_base), zscore(q["es_preopen_return"].value, es_base)
                q["nq_es_standardized_divergence"] = (_q(zn - ze) if zn is not None and ze is not None
                                                      else _bad("undefined"))
        else:
            q["nq_es_standardized_divergence"] = derive(lambda a, b: None, nq_ret, q["es_preopen_return"])

        # Last-60m relative return: both legs use exactly the 08:28 and 09:28 closes.
        es_cid = self.contract_for("ES", s.session_date)
        if es_cid is not None:
            es_df = self._bars(es_cid, self.T - 61 * ONE_MIN, self.T, INSTRUMENTS["ES"].what_to_show)
            es_close = dict(zip(es_df["bar_start_at"], es_df["close"]))
            es_now = es_close.get(self.T - ONE_MIN)
            es_0828 = es_close.get(self.T - 61 * ONE_MIN)
            es60 = _q(ratio(es_now, es_0828) - 1 if ratio(es_now, es_0828) is not None else None, fail="missing")
        else:
            es60 = _bad("missing")
        nq60 = derive(lambda p, c: ratio(p, c) - 1 if ratio(p, c) is not None else None, Pq, st["c0828"])
        q["nq_es_relative_return_60m"] = derive(lambda a, b: a - b, nq60, es60)

    def _calendar(self, nq):
        q, s = self.q, self.sess
        q["weekday"] = Q(("mon", "tue", "wed", "thu", "fri")[s.session_date.weekday()])
        q["monthly_opex_week"] = Q(cal.monthly_opex_week(s.session_date))
        q["is_early_close"] = Q(s.is_early_close)
        rule = INSTRUMENTS[NQ].roll
        q["roll_transition"] = Q(cal.roll_transition(s.session_date, rule.months, rule.days_before_expiry))
        c = self.md.contract(nq) or {}
        expiry = str(c.get("expiry") or "")
        if len(expiry) >= 8 and expiry[:8].isdigit():
            days = (datetime.strptime(expiry[:8], "%Y%m%d").date() - s.session_date).days
            q["days_to_nq_expiry"] = Q(days) if days >= 0 else _bad("undefined")
        else:
            q["days_to_nq_expiry"] = _bad("missing")

    def _events(self):
        q, s = self.q, self.sess
        # A live capture may only use calendar rows it could have seen by now.
        recorded_by = ((self.frozen_at or datetime.now(timezone.utc))
                       if self.data_mode == "live_capture" else None)
        coverage, events = self.md.event_calendar(s.session_date, self.T, s.scheduled_close_at, recorded_by)
        status = {"validity": "valid" if coverage else "missing", "coverage": coverage,
                  "events_in_window": len(events)}
        self.source_status["economic_events"] = status
        if coverage is None:
            for k in ("remaining_event_risk", "has_future_high_event", "minutes_to_high_event"):
                q[k] = _bad("missing")
            return
        tiers = {e["tier"] for e in events}
        q["remaining_event_risk"] = Q("high" if "high" in tiers else "moderate" if "moderate" in tiers else "none")
        highs = [e["scheduled_at"] for e in events if e["tier"] == "high"]
        q["has_future_high_event"] = Q(bool(highs))
        q["minutes_to_high_event"] = (_q((min(highs) - self.T) / ONE_MIN) if highs
                                      else _bad("not_applicable"))
        status["events"] = [{"name": e["name"], "tier": e["tier"], "scheduled_at": _iso(e["scheduled_at"]),
                             "source": e["source"]} for e in events]

    def _availability(self, cid, day, pt, bar_start_iso, value) -> Dict[str, Any]:
        """
        When the value used became available: the real-time receipt of that exact
        bar (collector/live_stream.py) if one recorded the same close, else the day's
        ledger write time - a weaker, day-level proof.
        """
        if bar_start_iso is not None and value is not None:
            start = datetime.strptime(bar_start_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            receipt = self.md.bar_receipt(cid, start, pt)
            if receipt is not None and receipt["close"] == value:
                return {"available_at": _iso(receipt["received_at"]), "evidence": "bar_receipt",
                        "receipt_revision": receipt["revision"], "finalised_by": receipt["finalised_by"]}
        led = self.md.ledger(cid, day, pt)
        return {"available_at": _iso(led["fetched_at"]) if led else None,
                "evidence": "day_ledger" if led else None}

    def _nq_status(self, nq, st, d_prev):
        c = self.md.contract(nq) or {}
        P_ok = st["P"].ok
        obs_start = _iso(self.T - ONE_MIN) if P_ok else None
        ref_start = _iso(self.prev.scheduled_close_at - ONE_MIN) if d_prev else None
        return {
            "asset": "nq", "symbol": NQ, "contract_id": nq, "local_symbol": c.get("local_symbol"),
            "expiry": c.get("expiry"), "rule": self.contracts_used[f"{NQ}/{self.sess.session_date}"]["rule"],
            "observation": {"instant": _iso(self.T),
                            "bar_start_at": obs_start,
                            "bar_end_at": _iso(self.T) if P_ok else None,
                            "age_minutes": 0.0 if P_ok else None,
                            "validity": "valid" if P_ok else "missing",
                            **self._availability(nq, self.sess.session_date, "TRADES", obs_start,
                                                 st["P"].value)},
            "reference": {"instant": _iso(self.prev.scheduled_close_at),
                          "bar_start_at": ref_start,
                          "bar_end_at": _iso(self.prev.scheduled_close_at) if d_prev else None,
                          "validity": "valid" if d_prev else "missing",
                          **self._availability(nq, self.prev.session_date, "TRADES", ref_start,
                                               d_prev["close"] if d_prev else None)},
            "overnight_coverage": st["on_coverage"].value,
            "window_60m_complete": st["high60"].ok,
            "validity": "valid" if P_ok and d_prev else "missing",
        }

    def _pit_status(self) -> str:
        """
        ``verified`` only for a live capture where every included source's
        current-session observation has a real-time receipt of the exact value
        used, received by the freeze, and every reference value was in the store
        by then. Anything else is ``unverified_historical``.
        """
        if self.data_mode != "live_capture":
            return "unverified_historical"
        frozen = _iso(self.frozen_at)
        for name, src in self.source_status.items():
            if src.get("validity") != "valid":
                continue
            for leg in ("observation", "reference"):
                info = src.get(leg)
                if info is None:
                    continue
                if info.get("available_at") is None or info["available_at"] > frozen:
                    return "unverified_historical"
                if leg == "observation" and info.get("evidence") != "bar_receipt":
                    return "unverified_historical"
        return "verified"

    def _result(self, nq) -> SnapshotResult:
        features, statuses = {}, {}
        for name in catv2.FEATURE_NAMES:
            qv = self.q.get(name, _bad("missing"))
            value, problem = catv2.check_value(name, qv.value) if qv.ok else (None, None)
            if problem:
                logger.warning(f"{self.sess.session_date} {name}: {problem}; recorded as undefined.")
                features[name], statuses[name] = None, "undefined"
            else:
                features[name] = value if qv.ok else None
                statuses[name] = qv.status
        assert set(statuses.values()) <= set(catv2.FEATURE_STATUSES), statuses

        self.source_status["calendar"] = {"version": cal.CALENDAR_VERSION, "schedule": self.sess.schedule,
                                          "validity": "valid"}
        manifest = {
            "calendar_version": cal.CALENDAR_VERSION,
            "roll_policy": P["roll_policy"],
            "asset_sources": P["asset_sources"],
            "session_date": self.sess.session_date.isoformat(),
            "contracts": dict(sorted(self.contracts_used.items())),
            "reads": dict(sorted(self.reads.items())),
        }
        revision = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:32]
        frozen = self.frozen_at or datetime.now(timezone.utc)
        self.frozen_at = frozen
        return SnapshotResult(
            session_date=self.sess.session_date.isoformat(),
            instrument_id=int(nq),
            cutoff_at=self.T,
            features_frozen_at=frozen,
            data_mode=self.data_mode,
            pit_availability_status=self._pit_status(),
            feature_version=catv2.FEATURE_VERSION,
            session_schedule=self.sess.schedule,
            scheduled_close_at=self.sess.scheduled_close_at,
            source_status=self.source_status,
            feature_status=statuses,
            data_quality_status=catv2.quality_status(statuses, catv2.DEFAULT_REQUIRED),
            reference_values={k: (finite(v) if not isinstance(v, (str, bool)) else v)
                              for k, v in self.ref.items()},
            features=features,
            manifest=manifest,
            source_revision_id=revision,
        )


def build_snapshot(md, session_date, data_mode="historical_reconstruction",
                   frozen_at: Optional[datetime] = None) -> SnapshotResult:
    """
    One nq_features_v2 snapshot. ``features_frozen_at`` is the moment the payload
    was complete (now, unless given). A ``live_capture`` must be frozen inside
    [T, 09:30 ET) of its own session; anything else is a historical
    reconstruction with unverified point-in-time availability.
    """
    s = cal.session(session_date)
    if data_mode == "live_capture" and s.is_open:
        now = frozen_at or datetime.now(timezone.utc)
        if now < s.cutoff_at:
            raise SnapshotError(f"Too early for a live capture of {s.session_date}: the 09:28 bar "
                                f"has not ended yet (T = {s.cutoff_at.isoformat()}).")
    result = SnapshotBuilder(md, session_date, data_mode, frozen_at).build()
    if data_mode == "live_capture" and not (s.cutoff_at <= result.features_frozen_at < s.rth_open_at):
        raise SnapshotError(
            f"A live capture for {s.session_date} must be frozen in [09:29, 09:30) ET; it was "
            f"complete at {result.features_frozen_at.isoformat()}. Rebuild it as a "
            f"historical_reconstruction."
        )
    return result
