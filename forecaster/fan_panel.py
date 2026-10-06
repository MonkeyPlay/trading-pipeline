# forecaster/fan_panel.py
"""
The point-in-time panel of the intermarket fan experiment (docs/fan_experiment.md,
chunk 1): every collected instrument on the target's minute grid - each trading day's
1440 slots from 18:00 ET the evening before (forecaster/fan_benchmark.py) - as it was
known at the end of each minute.

  close    the close of the instrument's last bar that had closed by the end of the
           minute (a bar starting in the slot counts: it closes as the minute ends),
           carried from before the day while nothing has traded yet; NaN when no bar
           exists within CARRY_DAYS
  age      minutes since that bar closed: 0 when it closed with the minute
  volume   the volume of the bar starting in the slot, 0 without one

An instrument is read on the contract the collector made active for the day, for the
day's own minutes and for what it carries from before them (the collector stores a
future's next contract for warm-up days before it becomes front), so a value never
jumps between contracts within a day. A minute's value comes only from bars that had
closed by then: the panel cannot see ahead.

  series(start, minutes, closes, volumes)   one instrument's day from its bars (pure)
  load_panel(conn, sessions, symbols)       many sessions: one query per instrument and
                                            contract
  audit(panel, complete)                    per instrument: sessions without a bar, bars
                                            per day, the hours it trades, how old its value
                                            is at fixed times of day
  write_audit(...)                          docs/reports/fan_panel_audit_<experiment>_<first>_<last>.md
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from config import Config
from forecaster.fan_benchmark import DAY_SLOTS, day_start

CARRY_DAYS = 7            # how far back a day's first value may be carried from (weekends and holidays)
AUDIT_TIMES = ("18:00", "03:00", "08:00", "09:29", "09:45", "12:00", "16:00", "16:59")
_BARS = """
    SELECT timestamp_utc, close, volume FROM bars
     WHERE contract_id = %s AND interval = '1m' AND price_type = %s AND trading_day BETWEEN %s AND %s
       AND timestamp_utc <= %s
     ORDER BY timestamp_utc;"""
_FOREVER = datetime(9999, 1, 1, tzinfo=timezone.utc)


def _epoch_minutes(values) -> np.ndarray:
    """Timestamps (UTC text or datetimes, naive ones read as UTC) as integer minutes since the epoch."""
    if not len(values):
        return np.zeros(0, dtype=np.int64)
    return pd.to_datetime(list(values), utc=True).as_unit("s").asi8 // 60


def _slot_of(hhmm: str) -> int:
    """The grid slot of a clock time: minutes since 18:00 ET."""
    h, m = (int(x) for x in hhmm.split(":"))
    return ((h - 18) % 24) * 60 + m


def series(start: int, minutes: np.ndarray, closes: np.ndarray, volumes: np.ndarray,
           carry: int = CARRY_DAYS * DAY_SLOTS) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    ``(close, age, volume)`` over the day's slots from the bars starting at epoch ``minutes`` (ascending) with
    their ``closes`` and ``volumes`` - the day's grid starting at epoch minute ``start``. Bars from ``carry``
    minutes before it can be carried; bars from the day's end on are never read.
    """
    lo = np.searchsorted(minutes, start - carry, side="left")
    hi = np.searchsorted(minutes, start + DAY_SLOTS, side="left")
    m, c, v = minutes[lo:hi] - start, np.asarray(closes[lo:hi], dtype=float), volumes[lo:hi]
    t = np.arange(DAY_SLOTS)
    idx = np.searchsorted(m, t, side="right") - 1          # the last bar starting at or before each slot
    seen = idx >= 0
    close = np.full(DAY_SLOTS, np.nan)
    age = np.full(DAY_SLOTS, np.nan, dtype=np.float32)
    close[seen] = c[idx[seen]]
    age[seen] = t[seen] - m[idx[seen]]
    volume = np.zeros(DAY_SLOTS, dtype=np.float32)
    inside = m >= 0
    volume[m[inside]] = v[inside]
    return close, age, volume


@dataclass
class Panel:
    """Sessions x instruments x the day's 1440 slots (see the module docstring)."""
    sessions: List[str]
    symbols: List[str]
    close: np.ndarray            # (sessions, symbols, 1440) float64
    age: np.ndarray              # (sessions, symbols, 1440) float32
    volume: np.ndarray           # (sessions, symbols, 1440) float32
    contract: np.ndarray         # (sessions, symbols) the contract read; -1 without an active contract

    def get(self, session: str, symbol: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        i, j = self.sessions.index(session), self.symbols.index(symbol)
        return self.close[i, j], self.age[i, j], self.volume[i, j]


def load_panel(conn, sessions: Sequence[str], symbols: Sequence[str], carry_days: int = CARRY_DAYS,
               as_of: Optional[datetime] = None) -> Panel:
    """The panel of ``sessions`` (trading days, YYYY-MM-DD) for ``symbols``, each on its active contract; with
    ``as_of`` (aware) only the bars that had closed by then - a bar starting at minute m closes at m + 1 - as a
    forecast issued at ``as_of`` would have read the store."""
    last_start = _FOREVER if as_of is None else as_of.astimezone(timezone.utc) - timedelta(minutes=1)
    sessions = sorted(str(s)[:10] for s in sessions)
    n, k = len(sessions), len(symbols)
    close = np.full((n, k, DAY_SLOTS), np.nan)
    age = np.full((n, k, DAY_SLOTS), np.nan, dtype=np.float32)
    volume = np.zeros((n, k, DAY_SLOTS), dtype=np.float32)
    contract = np.full((n, k), -1, dtype=np.int64)
    starts = {d: int(day_start(date.fromisoformat(d)).timestamp()) // 60 for d in sessions}
    for j, symbol in enumerate(symbols):
        inst = Config.instrument(symbol)
        if inst is None:
            raise ValueError(f"unknown instrument {symbol!r}")
        active = {str(d)[:10]: int(cid) for d, cid in conn.execute(
            "SELECT trading_day, contract_id FROM active_contracts WHERE symbol = %s AND trading_day BETWEEN %s AND %s;",
            (symbol, sessions[0], sessions[-1])).fetchall()}
        by_contract: Dict[int, List[int]] = defaultdict(list)
        for i, d in enumerate(sessions):
            if d in active:
                by_contract[active[d]].append(i)
        for cid, rows in by_contract.items():
            first = date.fromisoformat(sessions[rows[0]]) - timedelta(days=carry_days + 1)
            bars = conn.execute(_BARS, (cid, inst.what_to_show, first, sessions[rows[-1]], last_start)).fetchall()
            minutes = _epoch_minutes([b[0] for b in bars])
            order = np.argsort(minutes, kind="stable")
            minutes = minutes[order]
            closes = np.array([float(b[1]) for b in bars])[order] if bars else np.zeros(0)
            volumes = np.array([float(b[2] or 0) for b in bars], dtype=np.float32)[order] if bars else np.zeros(0)
            for i in rows:
                contract[i, j] = cid
                close[i, j], age[i, j], volume[i, j] = series(starts[sessions[i]], minutes, closes, volumes,
                                                              carry_days * DAY_SLOTS)
    return Panel(sessions, list(symbols), close, age, volume, contract)


def fingerprint(conn, symbols: Sequence[str], first: str, last: str) -> str:
    """The stored data a run read, as one hash: per instrument the count, the exact decimal sum of closes and of
    volumes of its 1-minute bars from ``first`` to ``last`` (every contract). A backfill or a revised bar changes it."""
    import hashlib
    h = hashlib.sha256()
    for symbol in sorted(symbols):
        inst = Config.instrument(symbol)
        n, c, v = conn.execute(
            "SELECT count(*), coalesce(sum(round(b.close::numeric, 6)), 0), coalesce(sum(round(b.volume::numeric, 3)), 0) "
            "FROM bars b "
            "JOIN contracts k ON k.contract_id = b.contract_id WHERE k.symbol = %s AND b.interval = '1m' "
            "AND b.price_type = %s AND b.trading_day BETWEEN %s AND %s;", (symbol, inst.what_to_show, first, last)
        ).fetchone()
        h.update(f"{symbol}|{int(n)}|{c}|{v}\n".encode())                # exact decimals: no summation-order noise
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------

def _hours(share: np.ndarray, bucket: int = 15) -> str:
    """The clock times (ET) in which most sessions trade most minutes, by ``bucket``-minute steps:
    '09:30-16:15'."""
    on = share.reshape(DAY_SLOTS // bucket, bucket).mean(axis=1) >= 0.5
    if not on.any():
        return "no regular hours"
    ranges, start = [], None
    for k in range(len(on) + 1):
        now = k < len(on) and on[k]
        if now and start is None:
            start = k
        if not now and start is not None:
            ranges.append((start, k))
            start = None
    clock = lambda k: f"{(18 + k * bucket // 60) % 24:02d}:{k * bucket % 60:02d}"
    return ", ".join(f"{clock(a)}-{clock(b)}" for a, b in ranges)


def audit(panel: Panel, complete: Optional[Dict[str, Sequence[str]]] = None) -> List[Dict[str, Any]]:
    """
    Per instrument over the panel's sessions: the sessions with no bar of their own, the median bars per session,
    the hours it trades (most minutes traded in most sessions), the median age of its value at AUDIT_TIMES, and
    the sessions the store does not hold complete (``complete``: each symbol's complete sessions).
    """
    out = []
    for j, symbol in enumerate(panel.symbols):
        traded = panel.age[:, j, :] == 0
        bars = traded.sum(axis=1)
        held = set(complete.get(symbol, ())) if complete is not None else None
        ages = {}
        for hhmm in AUDIT_TIMES:
            col = panel.age[:, j, _slot_of(hhmm)]
            ages[hhmm] = float(np.nanmedian(col)) if np.isfinite(col).any() else None
        out.append({
            "symbol": symbol,
            "sessions": len(panel.sessions),
            "no_bar": [panel.sessions[i] for i in np.flatnonzero(bars == 0)],
            "no_contract": [panel.sessions[i] for i in np.flatnonzero(panel.contract[:, j] < 0)],
            "bars_per_session": float(np.median(bars)) if len(bars) else 0.0,
            "hours": _hours(traded.mean(axis=0)),
            "median_age": ages,
            "not_complete": None if held is None else [d for d in panel.sessions if d not in held],
        })
    return out


def _age(x: Optional[float]) -> str:
    if x is None:
        return "-"
    if x >= 1440:
        return f"{x / 1440:.1f} d"
    if x >= 60:
        return f"{x / 60:.1f} h"
    return f"{x:.0f}"


def _dates(days: Sequence[str]) -> str:
    return "none" if not days else f"{len(days)}: " + ", ".join(days[:4]) + (" ..." if len(days) > 4 else "")


def write_audit(rows: List[Dict[str, Any]], meta: Dict[str, Any], report_dir: str) -> str:
    """The audit as docs/reports/fan_panel_audit_<experiment>_<first>_<last>.md; returns its path."""
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"fan_panel_audit_{meta['experiment']}_{meta['first']}_{meta['last']}.md")
    lines = [f"# Point-in-time panel audit: {meta['experiment']}, {meta['first']} to {meta['last']}", "",
             f"{meta['sessions']} development sessions ({meta['role']}), every collected instrument on the target's "
             "minute grid (forecaster/fan_panel.py). A value at a minute is the close of the instrument's last bar "
             "closed by then, on the day's active contract; its age is the minutes since that bar closed.", "",
             f"Code {meta['code_revision'][:12]}; loaded in {meta['seconds']:.1f} s.", "",
             "## Coverage and trading hours", "",
             "| Instrument | Bars per session (median) | Trades (ET, most minutes in most sessions) | Sessions with no bar "
             "| Not complete in the store |",
             "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['symbol']} | {r['bars_per_session']:.0f} | {r['hours']} | {_dates(r['no_bar'])} | "
                     f"{'-' if r['not_complete'] is None else _dates(r['not_complete'])} |")
    lines += ["", "## How old the value is (median minutes since the last bar closed)", "",
              "| Instrument | " + " | ".join(AUDIT_TIMES) + " |", "|---|" + "---|" * len(AUDIT_TIMES)]
    for r in rows:
        lines.append(f"| {r['symbol']} | " + " | ".join(_age(r["median_age"][t]) for t in AUDIT_TIMES) + " |")
    lines += ["", "A stale value is carried, never filled in: features read its age beside it (chunk 4).", ""]
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    return path
