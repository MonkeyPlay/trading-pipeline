# tests/synthetic.py
"""
Deterministic synthetic 1-minute markets for exercising the v2 snapshot builder
without IB or a database: full Globex sessions for futures (18:00 ET the prior
evening -> 17:00 ET, every scheduled session of the calendar), a cash index
printing in extended hours, and an RTH-only yield index.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from features import calendar as cal
from features.market_data import FrameMarketData

NQ_CID, ES_CID, VIX_CID, TNX_CID = 101, 102, 103, 104


def _session_minutes(s: cal.Session, start_et: str, end_et: str, prior_evening: bool) -> pd.DatetimeIndex:
    d = s.session_date
    start_day = d - timedelta(days=1) if prior_evening else d
    start = cal.NY_TZ.localize(pd.Timestamp(f"{start_day} {start_et}").to_pydatetime())
    end = cal.NY_TZ.localize(pd.Timestamp(f"{d} {end_et}").to_pydatetime())
    return pd.date_range(start, end, freq="1min", inclusive="left").tz_convert("UTC")


def _walk(index, start_price, vol, rng, volume=True):
    n = len(index)
    closes = start_price * np.exp(np.cumsum(rng.normal(0, vol, n)))
    opens = np.concatenate([[start_price], closes[:-1]])
    spread = np.abs(rng.normal(0, vol, n)) * closes
    df = pd.DataFrame({
        "bar_start_at": index,
        "open": opens,
        "high": np.maximum(opens, closes) + spread,
        "low": np.minimum(opens, closes) - spread,
        "close": closes,
        "volume": rng.integers(50, 500, n).astype(float) if volume else np.zeros(n),
    })
    return df


def make_market(last_day="2026-06-10", n_sessions=90, seed=7, drop=None, contracts_expiry="20260918"):
    """
    FrameMarketData holding ``n_sessions`` scheduled sessions ending at
    ``last_day`` (inclusive). ``drop`` is a list of (contract_id, bar_start UTC
    Timestamp) to delete, for testing missing endpoints.
    """
    rng = np.random.default_rng(seed)
    last = date.fromisoformat(last_day)
    sessions = cal.sessions_before(last + timedelta(days=1), n_sessions)
    frames = []
    prices = {NQ_CID: 20000.0, ES_CID: 5500.0, VIX_CID: 16.0, TNX_CID: 42.5}
    for s in sessions:
        for cid, vol in ((NQ_CID, 0.0004), (ES_CID, 0.0003)):
            idx = _session_minutes(s, "18:00", "17:00", prior_evening=True)
            df = _walk(idx, prices[cid], vol, rng)
            prices[cid] = float(df["close"].iloc[-1])
            frames.append(df.assign(contract_id=cid))
        idx = _session_minutes(s, "03:00", "16:15", prior_evening=False)
        df = _walk(idx, prices[VIX_CID], 0.0008, rng, volume=False)
        prices[VIX_CID] = float(df["close"].iloc[-1])
        frames.append(df.assign(contract_id=VIX_CID))
        idx = _session_minutes(s, "09:30", "15:00", prior_evening=False)
        df = _walk(idx, prices[TNX_CID], 0.0002, rng, volume=False)
        prices[TNX_CID] = float(df["close"].iloc[-1])
        frames.append(df.assign(contract_id=TNX_CID))

    bars = pd.concat(frames, ignore_index=True)
    if drop:
        keys = {(c, pd.Timestamp(t)) for c, t in drop}
        mask = [(c, t) not in keys for c, t in zip(bars["contract_id"], bars["bar_start_at"])]
        bars = bars[mask]

    days = [s.session_date.isoformat() for s in sessions]
    active = {}
    for sym, cid in (("NQ", NQ_CID), ("ES", ES_CID), ("VIX", VIX_CID), ("TNX", TNX_CID)):
        active.update({(sym, d): cid for d in days})
    contracts = {
        NQ_CID: {"symbol": "NQ", "expiry": contracts_expiry, "local_symbol": "NQU6"},
        ES_CID: {"symbol": "ES", "expiry": contracts_expiry, "local_symbol": "ESU6"},
        VIX_CID: {"symbol": "VIX", "expiry": None},
        TNX_CID: {"symbol": "TNX", "expiry": None},
    }
    fetched = {(cid, d): pd.Timestamp(d, tz="UTC") + pd.Timedelta(days=2)
               for cid in contracts for d in days}
    return FrameMarketData(bars, active=active, contracts=contracts, fetched_at=fetched), sessions
