# features/market_data.py
"""
Read access to the market-data store for the v2 snapshot builder.

The builder never touches SQL: it asks a ``MarketData`` for 1-minute bars of a
contract over a half-open ``[start, end)`` range of bar *start* times, for the
contract that stood for a symbol on a day, for ledger rows (when a day was last
written) and for the economic-event calendar. ``DbMarketData`` answers from
PostgreSQL; ``FrameMarketData`` from in-memory frames (tests, notebooks).

Every bar read returns its content digest too, so the builder can record exactly
which data a snapshot saw (its source revision) without re-hashing.
"""

import hashlib
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config import Config

BAR_COLUMNS = ["bar_start_at", "open", "high", "low", "close", "volume"]


def frame_digest(df: pd.DataFrame) -> str:
    """Content hash of a bar frame (timestamps and exact OHLCV)."""
    if df is None or df.empty:
        return "empty"
    payload = df[BAR_COLUMNS].to_csv(index=False, date_format="%Y-%m-%dT%H:%M:%S")
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"bar_start_at": pd.Series(dtype="datetime64[ns, UTC]"),
                         **{c: pd.Series(dtype=float) for c in BAR_COLUMNS[1:]}})


def _iso(ts: datetime) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


class MarketData:
    """Interface plus a small LRU of bar reads shared across snapshots."""

    def __init__(self, cache_size: int = 20_000):
        self._cache: "OrderedDict[tuple, Tuple[pd.DataFrame, str]]" = OrderedDict()
        self._cache_size = cache_size

    # -- to implement -----------------------------------------------------------
    def _fetch_bars(self, contract_id: int, start: datetime, end: datetime, price_type: str) -> pd.DataFrame:
        raise NotImplementedError

    def active_contract(self, symbol: str, day) -> Optional[Dict]:
        """``{'contract_id', 'rule'}`` from active_contracts, or None."""
        raise NotImplementedError

    def fallback_contract(self, symbol: str) -> Optional[int]:
        """The configured contract for a symbol with no active_contracts row."""
        raise NotImplementedError

    def contract(self, contract_id: int) -> Optional[Dict]:
        raise NotImplementedError

    def ledger(self, contract_id: int, day, price_type: str) -> Optional[Dict]:
        """``{'fetched_at', 'status'}`` of a stored day, or None."""
        raise NotImplementedError

    def event_calendar(self, day, start: datetime, end: datetime,
                       recorded_by: Optional[datetime]) -> Tuple[Optional[Dict], List[Dict]]:
        """(coverage row vouching for ``day`` or None, events scheduled in (start, end))."""
        raise NotImplementedError

    def bar_receipt(self, contract_id: int, bar_start: datetime, price_type: str) -> Optional[Dict]:
        """The latest real-time receipt of one minute (``received_at``, ``revision``,
        ``finalised_by``, ``close``) from bar_receipts, or None if it was never streamed."""
        return None

    def latest_bar_start(self, contract_id: int, before: datetime, price_type: str,
                         lookback: timedelta = timedelta(days=7)) -> Optional[datetime]:
        """Start of the last bar with bar_start_at < ``before`` (within ``lookback``).
        Only used to report how old a stale source is."""
        df, _, _ = self.bars(contract_id, before - lookback, before, price_type)
        return None if df.empty else df["bar_start_at"].iloc[-1].to_pydatetime()

    # -- shared -----------------------------------------------------------------
    def bars(self, contract_id: int, start: datetime, end: datetime,
             price_type: str = "TRADES") -> Tuple[pd.DataFrame, str, str]:
        """1m bars with start <= bar_start_at < end, oldest first: (frame, digest, read key)."""
        key = (int(contract_id), price_type, _iso(start), _iso(end))
        hit = self._cache.get(key)
        if hit is None:
            df = self._fetch_bars(int(contract_id), start, end, price_type)
            df = df.sort_values("bar_start_at").reset_index(drop=True) if not df.empty else _empty()
            hit = (df, frame_digest(df))
            self._cache[key] = hit
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return hit[0], hit[1], "bars/{}/{}/{}/{}".format(*key)


class DbMarketData(MarketData):
    """``MarketData`` over the PostgreSQL store (a ``database.connection.Database``)."""

    def __init__(self, conn, cache_size: int = 20_000):
        super().__init__(cache_size)
        self.conn = conn

    def _fetch_bars(self, contract_id, start, end, price_type):
        rows = self.conn.execute(
            "SELECT timestamp_utc, open, high, low, close, volume FROM bars "
            "WHERE contract_id = %s AND interval = '1m' AND price_type = %s "
            "  AND timestamp_utc >= %s AND timestamp_utc < %s ORDER BY timestamp_utc;",
            (contract_id, price_type, start, end),
        ).fetchall()
        if not rows:
            return _empty()
        df = pd.DataFrame([tuple(r) for r in rows], columns=BAR_COLUMNS)
        df["bar_start_at"] = pd.to_datetime(df["bar_start_at"], utc=True)
        df["volume"] = df["volume"].astype(float)
        return df

    def latest_bar_start(self, contract_id, before, price_type, lookback=timedelta(days=7)):
        row = self.conn.execute(
            "SELECT max(timestamp_utc) FROM bars WHERE contract_id = %s AND interval = '1m' "
            "AND price_type = %s AND timestamp_utc < %s AND timestamp_utc >= %s;",
            (contract_id, price_type, before, before - lookback),
        ).fetchone()
        return pd.Timestamp(row[0], tz="UTC").to_pydatetime() if row and row[0] else None

    def bar_receipt(self, contract_id, bar_start, price_type):
        row = self.conn.execute(
            "SELECT received_at, revision, finalised_by, close FROM bar_receipts "
            "WHERE contract_id = %s AND interval = '1m' AND price_type = %s AND bar_start_at = %s "
            "ORDER BY revision DESC LIMIT 1;",
            (contract_id, price_type, bar_start),
        ).fetchone()
        if row is None:
            return None
        return {"received_at": pd.Timestamp(row["received_at"], tz="UTC").to_pydatetime(),
                "revision": int(row["revision"]), "finalised_by": row["finalised_by"],
                "close": float(row["close"])}

    def active_contract(self, symbol, day):
        row = self.conn.execute(
            "SELECT contract_id, rule FROM active_contracts WHERE symbol = %s AND trading_day = %s;",
            (symbol, str(day)),
        ).fetchone()
        return {"contract_id": int(row["contract_id"]), "rule": row["rule"]} if row else None

    def fallback_contract(self, symbol):
        from database.queries import get_contract_by_expiry
        row = get_contract_by_expiry(self.conn, symbol, Config.expiry_for(symbol))
        return int(row["contract_id"]) if row else None

    def contract(self, contract_id):
        row = self.conn.execute("SELECT * FROM contracts WHERE contract_id = %s;", (contract_id,)).fetchone()
        return dict(zip(row.keys(), row)) if row else None

    def ledger(self, contract_id, day, price_type):
        row = self.conn.execute(
            "SELECT fetched_at, status FROM session_days WHERE contract_id = %s AND interval = '1m' "
            "AND price_type = %s AND trading_day = %s;",
            (contract_id, price_type, str(day)),
        ).fetchone()
        if row is None:
            return None
        return {"fetched_at": pd.Timestamp(row["fetched_at"], tz="UTC").to_pydatetime(),
                "status": row["status"]}

    def event_calendar(self, day, start, end, recorded_by):
        rec = "" if recorded_by is None else " AND recorded_at <= %(rec)s"
        params = {"day": str(day), "start": start, "end": end, "rec": recorded_by}
        cov = self.conn.execute(
            "SELECT source, covered_from, covered_to, recorded_at FROM economic_event_coverage "
            f"WHERE covered_from <= %(day)s AND covered_to >= %(day)s{rec} ORDER BY source;",
            params,
        ).fetchall()
        if not cov:
            return None, []
        sources = [r["source"] for r in cov]
        params["sources"] = sources
        events = self.conn.execute(
            "SELECT source, event_key, scheduled_at, name, tier, recorded_at FROM economic_events "
            "WHERE source = ANY(%(sources)s) AND scheduled_at > %(start)s AND scheduled_at < %(end)s"
            f"{rec} ORDER BY scheduled_at;",
            params,
        ).fetchall()
        coverage = {"sources": sources, "recorded_at": max(str(r["recorded_at"]) for r in cov)}
        return coverage, [
            {"source": e["source"], "event_key": e["event_key"], "name": e["name"], "tier": e["tier"],
             "scheduled_at": pd.Timestamp(e["scheduled_at"], tz="UTC").to_pydatetime()}
            for e in events
        ]


class FrameMarketData(MarketData):
    """
    ``MarketData`` over in-memory data:

      bars      : DataFrame with contract_id, bar_start_at (UTC), OHLCV
                  [and price_type, default 'TRADES']
      active    : {(symbol, 'YYYY-MM-DD'): contract_id}
      contracts : {contract_id: {'symbol', 'expiry', ...}}
      fallback  : {symbol: contract_id}
      fetched_at: {(contract_id, 'YYYY-MM-DD'): datetime}; missing days report None
      receipts  : {(contract_id, bar_start UTC Timestamp): receipt dict} (see bar_receipt)
    """

    def __init__(self, bars: pd.DataFrame, active=None, contracts=None, fallback=None,
                 fetched_at=None, events=None, event_coverage=None, receipts=None):
        super().__init__()
        self._receipts = receipts or {}
        df = bars.copy()
        df["bar_start_at"] = pd.to_datetime(df["bar_start_at"], utc=True)
        if "price_type" not in df:
            df["price_type"] = "TRADES"
        self._bars = {k: g.sort_values("bar_start_at").reset_index(drop=True)
                      for k, g in df.groupby(["contract_id", "price_type"])}
        self._active = active or {}
        self._contracts = contracts or {}
        self._fallback = fallback or {}
        self._fetched = fetched_at or {}
        self._events = events or []
        self._coverage = event_coverage

    def _fetch_bars(self, contract_id, start, end, price_type):
        g = self._bars.get((contract_id, price_type))
        if g is None:
            return _empty()
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        return g[(g["bar_start_at"] >= s) & (g["bar_start_at"] < e)][BAR_COLUMNS].copy()

    def bar_receipt(self, contract_id, bar_start, price_type):
        return self._receipts.get((contract_id, pd.Timestamp(bar_start)))

    def active_contract(self, symbol, day):
        cid = self._active.get((symbol, str(day)))
        return {"contract_id": cid, "rule": "test"} if cid is not None else None

    def fallback_contract(self, symbol):
        return self._fallback.get(symbol)

    def contract(self, contract_id):
        return self._contracts.get(contract_id)

    def ledger(self, contract_id, day, price_type):
        at = self._fetched.get((contract_id, str(day)))
        return {"fetched_at": at, "status": "COMPLETE"} if at is not None else None

    def event_calendar(self, day, start, end, recorded_by):
        if self._coverage is None or not (self._coverage[0] <= str(day) <= self._coverage[1]):
            return None, []
        return ({"sources": ["test"], "recorded_at": None},
                [e for e in self._events if start < e["scheduled_at"] < end])
