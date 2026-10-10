# tests/synthetic.py
"""
Deterministic synthetic 1-minute markets for the database and dashboard tests,
without IB: full Globex sessions for the NQ and ES futures (18:00 ET the prior
evening -> 17:00 ET, every scheduled session of the calendar).
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from features import calendar as cal

NQ_CID, ES_CID, RTY_CID = 101, 102, 103


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


def make_market(last_day="2026-06-10", n_sessions=90, seed=7):
    """
    ``({contract_id: bars}, sessions)``: the bars (contract_id, bar_start_at UTC, OHLCV) of
    ``n_sessions`` scheduled sessions ending at ``last_day`` (inclusive), in time
    order, for NQ_CID and ES_CID.
    """
    rng = np.random.default_rng(seed)
    last = date.fromisoformat(last_day)
    sessions = cal.sessions_before(last + timedelta(days=1), n_sessions)
    frames = {NQ_CID: [], ES_CID: []}
    prices = {NQ_CID: 20000.0, ES_CID: 5500.0}
    for s in sessions:
        for cid, vol in ((NQ_CID, 0.0004), (ES_CID, 0.0003)):
            idx = _session_minutes(s, "18:00", "17:00", prior_evening=True)
            df = _walk(idx, prices[cid], vol, rng)
            prices[cid] = float(df["close"].iloc[-1])
            frames[cid].append(df.assign(contract_id=cid))
    bars = {cid: pd.concat(dfs, ignore_index=True) for cid, dfs in frames.items()}
    return bars, sessions


def make_instrument(sessions, cid, start_price, vol, seed):
    """One more instrument's full Globex sessions over ``sessions`` (its own random stream, so adding it never
    changes make_market's NQ and ES bars)."""
    rng = np.random.default_rng(seed)
    price, frames = start_price, []
    for s in sessions:
        df = _walk(_session_minutes(s, "18:00", "17:00", prior_evening=True), price, vol, rng)
        price = float(df["close"].iloc[-1])
        frames.append(df.assign(contract_id=cid))
    return pd.concat(frames, ignore_index=True)
