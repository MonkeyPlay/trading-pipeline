# forecaster/fan_data.py
"""
Reads what the benchmark fan (forecaster/fan_benchmark.py) needs from the store:
one instrument's trading days on the minute grid - the active contract's 1-minute
closes, the day's completeness from session_days - and the scheduled releases with
the economic calendar's coverage.

  load_days(conn, symbol, first, last, as_of=None)

``as_of`` reads only the bars that had closed by then, so a fan can be replayed
from any past minute exactly as it would have been drawn.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import Config
from contracts import fan as F
from features import calendar as cal
from forecaster.fan_benchmark import DAY_SLOTS, MINUTE, Day, Release, day_start, end_slot, slot_at

logger = logging.getLogger(__name__)

_BARS_ACTIVE = """
    SELECT b.trading_day, b.timestamp_utc, b.close, b.contract_id
      FROM bars b
      JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day
     WHERE a.symbol = %s AND b.interval = '1m' AND b.price_type = 'TRADES'
       AND b.trading_day BETWEEN %s AND %s AND b.timestamp_utc >= %s AND b.timestamp_utc < %s
     ORDER BY b.timestamp_utc;"""
_STATUS_ACTIVE = """
    SELECT d.trading_day, d.status, d.contract_id
      FROM session_days d
      JOIN active_contracts a ON a.contract_id = d.contract_id AND a.trading_day = d.trading_day
     WHERE a.symbol = %s AND d.interval = '1m' AND d.price_type = 'TRADES' AND d.trading_day BETWEEN %s AND %s;"""
# A store without active_contracts rows for the symbol (an old or hand-built one): its contracts' days, the one
# holding the most bars each day.
_BARS_ANY = """
    SELECT b.trading_day, b.timestamp_utc, b.close, b.contract_id
      FROM bars b JOIN contracts c ON c.contract_id = b.contract_id
     WHERE c.symbol = %s AND b.interval = '1m' AND b.price_type = 'TRADES'
       AND b.trading_day BETWEEN %s AND %s AND b.timestamp_utc >= %s AND b.timestamp_utc < %s
     ORDER BY b.timestamp_utc;"""
_STATUS_ANY = """
    SELECT d.trading_day, d.status, d.contract_id
      FROM session_days d JOIN contracts c ON c.contract_id = d.contract_id
     WHERE c.symbol = %s AND d.interval = '1m' AND d.price_type = 'TRADES' AND d.trading_day BETWEEN %s AND %s;"""


def _utc(value) -> datetime:
    """A TIMESTAMPTZ as the connection returns it (UTC text or a datetime) as an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    t = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _day(value) -> str:
    return str(value)[:10]


def history_start(first: date, sessions: int) -> date:
    """The session ``sessions`` scheduled sessions before ``first`` - or the calendar's first covered day."""
    d, n = first if isinstance(first, date) else date.fromisoformat(str(first)), 0
    while n < sessions and d > cal.COVERAGE_START:
        d -= timedelta(days=1)
        if cal.session(d).is_open:
            n += 1
    return d


def releases(conn, first: date, last: date) -> Tuple[List[dict], List[Tuple[date, date]]]:
    """The economic_events rows scheduled in the sessions' span and every coverage range."""
    lo, hi = day_start(first), day_start(last) + DAY_SLOTS * MINUTE
    rows = conn.execute("SELECT scheduled_at, name, tier FROM economic_events "
                        "WHERE scheduled_at >= %s AND scheduled_at < %s ORDER BY scheduled_at;", (lo, hi)).fetchall()
    cov = conn.execute("SELECT covered_from, covered_to FROM economic_event_coverage;").fetchall()
    events = [{"at": _utc(r[0]), "name": r[1], "tier": r[2]} for r in rows]
    return events, [(date.fromisoformat(_day(a)), date.fromisoformat(_day(b))) for a, b in cov]


def load_days(conn, symbol: str, first, last, as_of: Optional[datetime] = None) -> List[Day]:
    """
    ``symbol``'s scheduled sessions from ``first`` to ``last`` on the minute grid, oldest first - every one the store
    holds bars for. With ``as_of`` only bars that had closed by then are read (a session in progress is then
    incomplete).
    """
    inst = Config.instrument(symbol)
    if inst is None:
        raise ValueError(f"unknown instrument {symbol!r}")
    if inst.sec_type not in F.DAY_END_ET:
        raise ValueError(f"no fan for {symbol} ({inst.sec_type}): futures and stocks only")
    sessions = [s for s in cal.sessions_between(first, last) if s.is_open]
    if not sessions:
        return []
    lo = day_start(sessions[0].session_date)
    hi = day_start(sessions[-1].session_date) + DAY_SLOTS * MINUTE
    if as_of is not None:
        hi = min(hi, _utc(as_of) - MINUTE + timedelta(microseconds=1))      # bar start + 1 minute <= as_of
    args = (symbol, sessions[0].session_date, sessions[-1].session_date)
    active = conn.execute("SELECT 1 FROM active_contracts WHERE symbol = %s LIMIT 1;", (symbol,)).fetchone()
    if active is None:
        logger.warning(f"{symbol}: no active_contracts rows; reading every contract of the symbol instead")
    rows = conn.execute(_BARS_ACTIVE if active else _BARS_ANY, args + (lo, hi)).fetchall()
    status = {}
    for d, st, cid in conn.execute(_STATUS_ACTIVE if active else _STATUS_ANY, args).fetchall():
        status[(_day(d), int(cid))] = st
    by_day: Dict[str, Dict[int, List[Tuple[datetime, float]]]] = defaultdict(lambda: defaultdict(list))
    for d, ts, close, cid in rows:
        by_day[_day(d)][int(cid)].append((_utc(ts), float(close)))
    events, coverage = releases(conn, sessions[0].session_date, sessions[-1].session_date)
    out = []
    for s in sessions:
        key = s.session_date.isoformat()
        if key not in by_day:
            continue
        cid, bars = max(by_day[key].items(), key=lambda kv: len(kv[1]))
        closes = np.full(DAY_SLOTS, np.nan)
        for ts, close in bars:
            slot = slot_at(s.session_date, ts)
            if 0 <= slot < DAY_SLOTS:
                closes[slot] = close
        known = any(a <= s.session_date <= b for a, b in coverage)
        rel = []
        for e in events:
            group = F.event_group(e["name"], e["tier"])
            slot = slot_at(s.session_date, e["at"])
            if group is not None and 0 <= slot < DAY_SLOTS:
                rel.append(Release(slot, group, e["name"], e["at"]))
        complete = status.get((key, cid)) == "COMPLETE"
        if as_of is not None and _utc(as_of) < day_start(s.session_date) + DAY_SLOTS * MINUTE:
            complete = False
        out.append(Day(s.session_date, s.schedule, end_slot(inst.sec_type, s.schedule), closes, complete,
                       tuple(rel), known, cid))
    return out
