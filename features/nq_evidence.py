# features/nq_evidence.py
"""
Pre-open evidence snapshots for the NQ prompt-v2 implementation
(docs/nq_prompt_v2.md, guideline stages 1B and 1C).

``build_snapshot(conn, session_date, profile)`` freezes what was knowable at the
profile's cutoff (contracts/nq_prompt_v2.PROFILES) and archives the inputs it
used, so the snapshot can be checked without the mutable source tables:

  identity, schedule   session, active contract, calendar schedule
  cutoff               input cutoff, last completed bar, data mode and
                       point-in-time status
  references           previous-RTH high / low / close, overnight open, ON high /
                       low, premarket high / low ([08:00, cutoff), nq_conv_v4), cutoff
                       price - each with a status; Price at 09:29 is unavailable by
                       convention
  atr, thresholds      frozen daily and 2-minute Wilder ATR(14) and T, B, A
  bars                 the overnight window's 1m bars and their complete 2m, 5m and
                       15m clock buckets; previous_rth_bars, daily_atr_inputs
  prior_sessions       the RTH open / high / low / close of the five scheduled
                       sessions before, on the snapshot contract (HTB-v1, nq_conv_v3)
  events, intermarket  scheduled releases from the previous session's close to the
                       end of the day and material earnings published by the cutoff
                       (nq_conv_v2, P1 section 8), last observations of the other
                       collected instruments

Only bars that ended by the cutoff are read for the target session, so later bars
cannot change a snapshot. Every read happens in one repeatable-read transaction,
which gives a consistent view of the store - not proof that the values existed at
the market cutoff. Without real-time receipts a snapshot is therefore a
``historical_reconstruction`` with point-in-time status ``unverified_historical``.

The calculation conventions are contracts/nq_prompt_v2.CONVENTION.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import ASSET_SOURCES, Config
from contracts import nq_prompt_v2 as defs
from features import calendar as cal

MINUTE = timedelta(minutes=1)

# (start UTC, open, high, low, close, volume)
Bar = Tuple[datetime, float, float, float, float, int]


class SnapshotError(RuntimeError):
    """No snapshot can be built for the session (closed, outside the calendar, no contract)."""


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    contract_id: int
    session_date: str
    snapshot_version: str
    convention_version: str
    cutoff_at: datetime
    rth_open_at: datetime
    data_mode: str
    pit_availability_status: str
    payload: Dict[str, Any]
    source_payload_hash: str


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _utc(value) -> datetime:
    """A stored timestamp ('YYYY-MM-DD HH:MM:SS' UTC, or a datetime) as an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def dec(value) -> Optional[Decimal]:
    """A stored price as an exact Decimal (floats go through their shortest repr)."""
    return None if value is None else Decimal(str(value))


def _et(dt: datetime) -> str:
    return dt.astimezone(cal.NY_TZ).strftime("%H:%M")


def _minutes(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def _bar_rows(bars: Sequence[Bar]) -> List[list]:
    return [[iso(s), o, h, low, c, v] for s, o, h, low, c, v in bars]


def aggregate(bars: Sequence[Bar], minutes: int, cutoff: datetime) -> List[tuple]:
    """
    Clock buckets of ``minutes`` (2m on even minutes, 5m on multiples of 5, ...)
    from time-ordered 1m bars: (start, open, high, low, close, volume, 1m bars,
    complete). Only buckets that ended by ``cutoff``; an empty bucket produces no
    bar. ``complete`` (nq_conv_v5): every one of its minutes has exactly one 1m bar -
    a bucket missing a minute, or holding a duplicate, is kept but flagged, never
    filled. New York's offset from UTC is whole hours, so UTC minute alignment is ET
    alignment.
    """
    out: Dict[datetime, list] = {}
    seen: Dict[datetime, set] = {}
    for start, o, h, low, c, v in bars:
        epoch = int(start.timestamp()) // 60
        b_start = datetime.fromtimestamp((epoch - epoch % minutes) * 60, timezone.utc)
        if b_start + timedelta(minutes=minutes) > cutoff:
            continue
        b = out.get(b_start)
        if b is None:
            out[b_start] = [o, h, low, c, v, 1]
            seen[b_start] = {start}
        else:
            b[1], b[2], b[3], b[4], b[5] = max(b[1], h), min(b[2], low), c, b[4] + v, b[5] + 1
            seen[b_start].add(start)
    return [(s, *out[s], out[s][5] == minutes and len(seen[s]) == minutes) for s in sorted(out)]


def true_range(high: Decimal, low: Decimal, prev_close: Decimal) -> Decimal:
    return max(high, prev_close) - min(low, prev_close)


def wilder_atr(true_ranges: Sequence[Decimal], period: int = 14) -> Fraction:
    """
    Wilder's ATR, exactly: the mean of the first ``period`` true ranges, then
    (n-1 x ATR + TR) / n. A Fraction, because the repeated division by n does not
    terminate in decimal and a rounded value could cross an integer threshold.
    """
    if len(true_ranges) < period:
        raise ValueError(f"need at least {period} true ranges, got {len(true_ranges)}")
    n = Fraction(period)
    atr = sum((Fraction(tr) for tr in true_ranges[:period]), Fraction(0)) / n
    for tr in true_ranges[period:]:
        atr = (atr * (n - 1) + Fraction(tr)) / n
    return atr


def _shown(value: Fraction) -> Decimal:
    """An exact ATR rounded to 6 decimals, for display only."""
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.000001"),
                                                                             rounding=ROUND_HALF_EVEN)


def _ref(value, status: str = "valid", detail: Optional[str] = None, **extra) -> Dict[str, Any]:
    out = {"value": None if value is None or status != "valid" else value, "status": status}
    if detail:
        out["detail"] = detail
    out.update(extra)
    return out


# --------------------------------------------------------------------------
# Reads (all inside the caller's repeatable-read transaction)
# --------------------------------------------------------------------------

def _read_bars(conn, contract_id: int, first_start: datetime, last_start: datetime) -> List[Bar]:
    """1m TRADES bars of a contract starting in [first_start, last_start]."""
    rows = conn.execute(
        "SELECT timestamp_utc, open, high, low, close, volume FROM bars "
        "WHERE contract_id = %s AND interval = '1m' AND price_type = 'TRADES' "
        "AND timestamp_utc >= %s AND timestamp_utc <= %s ORDER BY timestamp_utc;",
        (contract_id, first_start, last_start),
    ).fetchall()
    return [(_utc(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5])) for r in rows]


def _active_contracts(conn, symbol: str, first_day, last_day) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT trading_day, contract_id FROM active_contracts WHERE symbol = %s "
        "AND trading_day BETWEEN %s AND %s;", (symbol, str(first_day), str(last_day)),
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _rth_aggregates(conn, contract_ids, sessions: Sequence[cal.Session]) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """{(contract_id, day): {'n', 'high', 'low', 'close'}} of the RTH 1m bars of the given sessions."""
    if not sessions or not contract_ids:
        return {}
    early = [s.session_date.isoformat() for s in sessions if s.is_early_close]
    rows = conn.execute(
        "SELECT contract_id, trading_day, count(*), max(high), min(low), "
        "       (array_agg(close ORDER BY timestamp_utc DESC))[1] "
        "  FROM bars "
        " WHERE contract_id = ANY(%s) AND interval = '1m' AND price_type = 'TRADES' "
        "   AND trading_day = ANY(%s::date[]) AND timestamp_utc >= %s AND timestamp_utc < %s "
        "   AND (timestamp_utc AT TIME ZONE 'America/New_York')::date = trading_day "
        "   AND (timestamp_utc AT TIME ZONE 'America/New_York')::time >= TIME '09:30' "
        "   AND (timestamp_utc AT TIME ZONE 'America/New_York')::time < "
        "       CASE WHEN trading_day = ANY(%s::date[]) THEN TIME '13:00' ELSE TIME '16:00' END "
        " GROUP BY contract_id, trading_day;",
        (list(contract_ids), [s.session_date.isoformat() for s in sessions],
         sessions[0].rth_open_at, sessions[-1].scheduled_close_at, early),
    ).fetchall()
    return {(int(r[0]), str(r[1])): {"n": int(r[2]), "high": dec(r[3]), "low": dec(r[4]), "close": dec(r[5])}
            for r in rows}


def _rth_minutes(s: cal.Session) -> int:
    return _minutes(s.rth_open_at, s.scheduled_close_at)


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------

def _daily_atr(conn, symbol: str, session: cal.Session, contract_id: int) -> Tuple[Dict[str, Any], list]:
    """The frozen daily ATR (convention daily_atr) and the true ranges it used."""
    period, window = defs.DAILY_ATR["period"], defs.DAILY_ATR["window_true_ranges"]
    sessions = cal.sessions_before(session.session_date, window)
    if len(sessions) < 2:
        return {"value": None, "status": "insufficient_history", "true_ranges": 0}, []
    try:
        sessions = [cal.previous_session(sessions[0].session_date)] + sessions
    except cal.CalendarCoverageError:
        return {"value": None, "status": "insufficient_history", "true_ranges": 0}, []
    active = _active_contracts(conn, symbol, sessions[0].session_date, sessions[-1].session_date)
    rth = _rth_aggregates(conn, set(active.values()) | {contract_id}, sessions)

    def complete(cid, s):
        row = rth.get((cid, s.session_date.isoformat()))
        return row if row is not None and row["n"] == _rth_minutes(s) else None

    # strict continuity (nq_conv_v5): the true ranges of exactly the `window` sessions before the target
    used, incomplete = [], []
    for prev, s in zip(sessions, sessions[1:]):
        cid = active.get(s.session_date.isoformat())
        today = complete(cid, s) if cid is not None else None
        before = complete(cid, prev) if cid is not None else None
        if today is None or before is None:
            incomplete.append(s.session_date.isoformat())
            continue
        tr = true_range(today["high"], today["low"], before["close"])
        used.append([s.session_date.isoformat(), cid, today["high"], today["low"], today["close"],
                     before["close"], tr])
    if len(sessions) < window + 1:
        return {"value": None, "status": "insufficient_history", "true_ranges": len(used)}, used
    if incomplete:
        return {"value": None, "status": "incomplete_history", "true_ranges": len(used),
                "detail": f"{len(incomplete)} of the {window} sessions incomplete: {', '.join(incomplete[:3])}"}, used
    atr = wilder_atr([u[6] for u in used], period)
    return {"value": _shown(atr), "exact": atr, "status": "valid", "period": period, "true_ranges": len(used),
            "first_session": used[0][0], "last_session": used[-1][0]}, used


def _last_boundary(cutoff: datetime, minutes: int) -> datetime:
    """The start of the last ``minutes`` clock bucket that ends by ``cutoff``."""
    epoch = int(cutoff.timestamp()) // 60 - minutes
    return datetime.fromtimestamp((epoch - epoch % minutes) * 60, timezone.utc)


def _consecutive(buckets: Sequence[tuple], minutes: int, last: datetime) -> Optional[str]:
    """Why ``buckets`` are not an unbroken run of complete ``minutes`` buckets ending at ``last`` (None: they are)."""
    if not buckets or buckets[-1][0] != last:
        return f"the last bucket before the cutoff ({iso(last)}) is missing"
    for a, b in zip(buckets, buckets[1:]):
        if b[0] - a[0] != timedelta(minutes=minutes):
            return f"a gap after {iso(a[0])}"
    bad = [iso(b[0]) for b in buckets if not b[7]]
    return f"incomplete bucket(s) {', '.join(bad[:3])}" if bad else None


def _two_minute_atr(buckets: Sequence[tuple], cutoff: datetime) -> Dict[str, Any]:
    """The frozen 2-minute ATR from the last 71 complete, consecutive 2m buckets (convention two_minute_atr)."""
    period, window = defs.TWO_MINUTE_ATR["period"], defs.TWO_MINUTE_ATR["window_true_ranges"]
    if len(buckets) < window + 1:
        return {"value": None, "status": "insufficient_history", "buckets": len(buckets)}
    use = buckets[-(window + 1):]
    broken = _consecutive(use, 2, _last_boundary(cutoff, 2))
    if broken:
        return {"value": None, "status": "incomplete_history", "detail": broken}
    trs = [true_range(dec(b[2]), dec(b[3]), dec(a[4])) for a, b in zip(use, use[1:])]
    atr = wilder_atr(trs, period)
    return {"value": _shown(atr), "exact": atr, "status": "valid", "period": period, "true_ranges": len(trs),
            "first_bucket": iso(use[1][0]), "last_bucket": iso(use[-1][0]),
            "last_bucket_end": iso(use[-1][0] + 2 * MINUTE)}


def _previous_rth(conn, contract_id: int, session: cal.Session) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Previous-RTH high / low / close on the snapshot contract, and the bars they came from."""
    try:
        prev = cal.previous_session(session.session_date)
    except cal.CalendarCoverageError:
        missing = _ref(None, "missing", "previous session outside the calendar")
        return {"prev_rth_high": missing, "prev_rth_low": missing, "prev_rth_close": missing}, {}
    bars = _read_bars(conn, contract_id, prev.rth_open_at, prev.scheduled_close_at - MINUTE)
    archive = {"session_date": prev.session_date.isoformat(), "schedule": prev.schedule, "1m": _bar_rows(bars)}
    expected = _rth_minutes(prev)
    if len(bars) != expected:
        bad = _ref(None, "incomplete", f"{len(bars)} of {expected} RTH minute bars on {prev.session_date}")
        return {"prev_rth_high": bad, "prev_rth_low": bad, "prev_rth_close": bad}, archive
    return {
        "prev_rth_high": _ref(max(dec(b[2]) for b in bars)),
        "prev_rth_low": _ref(min(dec(b[3]) for b in bars)),
        "prev_rth_close": _ref(dec(bars[-1][4])),
    }, archive


PRIOR_SESSIONS = 5


def _prior_sessions(conn, contract_id: int, session: cal.Session) -> Dict[str, Any]:
    """
    The RTH open / high / low / close of the PRIOR_SESSIONS scheduled sessions before
    ``session``, oldest first, on the snapshot contract (one price basis across a roll:
    the collector stores warm-up sessions before a contract becomes active). A session
    is valid only with every RTH minute bar; the hash covers every bar read.
    """
    digest = hashlib.sha256()
    out = []
    for s in cal.sessions_before(session.session_date, PRIOR_SESSIONS):
        bars = _read_bars(conn, contract_id, s.rth_open_at, s.scheduled_close_at - MINUTE)
        for b in bars:
            digest.update(repr((iso(b[0]),) + tuple(b[1:5])).encode())
        expected = _rth_minutes(s)
        row = {"session_date": s.session_date.isoformat(), "schedule": s.schedule, "minutes": len(bars),
               "expected_minutes": expected}
        if len(bars) != expected:
            row.update(status="incomplete", open=None, high=None, low=None, close=None)
        else:
            row.update(status="valid", open=dec(bars[0][1]), high=max(dec(b[2]) for b in bars),
                       low=min(dec(b[3]) for b in bars), close=dec(bars[-1][4]))
        out.append(row)
    return {"contract_id": contract_id, "sessions": out, "bars_digest": digest.hexdigest()}


def _window_extremes(bars: Sequence[Bar], expected: int, what: str, high: str, low: str) -> Dict[str, Any]:
    """High / low of a window that needs every one of its ``expected`` minutes exactly once (nq_conv_v5); a partial
    window keeps its observed extremes only as provisional diagnostics."""
    unique = len({b[0] for b in bars})
    if bars and unique == expected == len(bars):
        return {high: _ref(max(dec(b[2]) for b in bars), window_minutes=expected),
                low: _ref(min(dec(b[3]) for b in bars), window_minutes=expected)}
    detail = (f"{unique} of {expected} {what} minutes" + (f", {len(bars) - unique} duplicate(s)" if len(bars) > unique
                                                         else "") + ": the window is incomplete")
    observed = {} if not bars else {"provisional_high": max(dec(b[2]) for b in bars),
                                    "provisional_low": min(dec(b[3]) for b in bars)}
    return {high: _ref(None, "incomplete", detail, **observed), low: _ref(None, "incomplete", detail, **observed)}


def _vwap(bars: Sequence[Bar], complete: bool) -> Dict[str, Any]:
    """The cutoff VWAP, exact: sum(hlc3 x volume) / sum(volume) over a complete overnight window."""
    if not complete:
        return _ref(None, "incomplete", "the overnight window is incomplete")
    pv, vol = Fraction(0), Fraction(0)
    for _, _o, h, low, c, v in bars:
        pv += (Fraction(dec(h)) + Fraction(dec(low)) + Fraction(dec(c))) / 3 * v
        vol += v
    if vol == 0:
        return _ref(None, "no_volume", "no volume in the overnight window")
    exact = pv / vol
    return _ref(_shown(exact), exact=f"{exact.numerator}/{exact.denominator}")


def _overnight(bars: Sequence[Bar], session: cal.Session, cutoff: datetime) -> Dict[str, Any]:
    expected = _minutes(session.overnight_start_at, cutoff)
    unique = len({b[0] for b in bars})
    coverage = Decimal(unique) / Decimal(expected) if expected else Decimal(0)
    complete = bool(bars) and unique == expected == len(bars)
    refs: Dict[str, Any] = {}
    if bars and bars[0][0] == session.overnight_start_at:
        refs["overnight_open"] = _ref(dec(bars[0][1]))
    else:
        refs["overnight_open"] = _ref(None, "missing", "no bar starting 18:00 ET")
    refs.update(_window_extremes(bars, expected, "overnight", "on_high", "on_low"))
    if bars:
        last_end = bars[-1][0] + MINUTE
        age = _minutes(last_end, cutoff)
        refs["cutoff_price"] = _ref(dec(bars[-1][4]),
                                    "valid" if age <= defs.CUTOFF_PRICE_MAX_AGE_MINUTES else "stale",
                                    None if age <= defs.CUTOFF_PRICE_MAX_AGE_MINUTES else f"{age} min old",
                                    bar_start_at=iso(bars[-1][0]), age_minutes=age)
    else:
        refs["cutoff_price"] = _ref(None, "missing", "no bar in the overnight window")
    refs["price_at_0929"] = _ref(None, "not_observed",
                                 "the cutoff precedes 09:29:59; the 09:29 minute is not observed")
    # premarket (nq_conv_v4): [08:00 ET, cutoff); every minute (nq_conv_v5). Only a preview's earlier cutoff can
    # come before it has started.
    pm_start = cal.ny_instant(session.session_date, defs.PREMARKET_START)
    if cutoff <= pm_start:
        why = f"the premarket starts at {defs.PREMARKET_START:%H:%M} ET, after the cutoff"
        refs.update(premarket_high=_ref(None, "missing", why), premarket_low=_ref(None, "missing", why))
    else:
        refs.update(_window_extremes([b for b in bars if b[0] >= pm_start], _minutes(pm_start, cutoff), "premarket",
                                     "premarket_high", "premarket_low"))
    refs["vwap"] = _vwap(bars, complete)
    refs["_coverage"] = {"expected_minutes": expected, "minutes": unique, "ratio": coverage.quantize(
        Decimal("0.0001")), "complete": complete, "duplicates": len(bars) - unique}
    return refs


def _moving_averages(buckets: Sequence[tuple], session: cal.Session, cutoff: datetime) -> Dict[str, Any]:
    """
    The Long MA at the cutoff (nq_conv_v5): EMA(100) of the 2m buckets from 18:00, which must all be complete and
    consecutive up to the last bucket before the cutoff - the same calculation the structure annotation makes.
    """
    from features.calculations import calculate_moving_averages
    import pandas as pd
    on = [b for b in buckets if b[0] >= session.overnight_start_at]
    broken = (_consecutive(on, 2, _last_boundary(cutoff, 2)) if on and on[0][0] == session.overnight_start_at
              else "no 2m bucket starting 18:00 ET")
    if broken:
        return {"long_ma_at_cutoff": _ref(None, "incomplete_history", broken)}
    if len(on) < 100:
        return {"long_ma_at_cutoff": _ref(None, "insufficient_history", f"{len(on)} 2m buckets, EMA(100) needs 100")}
    ma = calculate_moving_averages(pd.DataFrame({"timestamp_utc": [b[0] for b in on], "close": [b[4] for b in on]}))
    value = Decimal(repr(float(ma["ema_trend"].iloc[-1]))).quantize(Decimal("0.000001"))
    return {"long_ma_at_cutoff": _ref(value, buckets=len(on), first_bucket=iso(on[0][0]),
                                      last_bucket=iso(on[-1][0]))}


def _first_level_candidates(refs: Dict[str, Any], moving: Dict[str, Any]) -> Dict[str, Any]:
    """The frozen first-level candidate list (nq_conv_v5): each candidate's price or why it is unavailable."""
    levels = {}
    for name in defs.FIRST_LEVEL_CANDIDATES:
        ref = moving["long_ma_at_cutoff"] if name == "long_ma" else refs.get(name) or _ref(None, "missing")
        levels[name] = {"value": ref["value"], "status": ref["status"], "exact": ref.get("exact"),
                        "source": "moving_averages.long_ma_at_cutoff" if name == "long_ma" else f"references.{name}"}
    return {"ids": list(defs.FIRST_LEVEL_CANDIDATES), "levels": levels}


EARNINGS_SOURCE = "sec_earnings"


def _events(conn, session: cal.Session, cutoff: datetime) -> Dict[str, Any]:
    """
    Scheduled releases from the previous session's scheduled close to the end of the
    session's ET day, and earnings releases (EARNINGS_SOURCE) published from that close
    to the cutoff only: a report after the cutoff is not known by it.
    """
    day = session.session_date
    start = cal.previous_session(day).scheduled_close_at
    end = cal.ny_instant(day + timedelta(days=1), datetime.min.time())
    coverage = conn.execute(
        "SELECT source, covered_from, covered_to, recorded_at FROM economic_event_coverage "
        "WHERE covered_from <= %s AND covered_to >= %s ORDER BY source;", (str(day), str(day)),
    ).fetchall()
    rows = conn.execute(
        "SELECT source, event_key, scheduled_at, name, tier, recorded_at FROM economic_events "
        "WHERE scheduled_at >= %s AND scheduled_at < %s AND (source <> %s OR scheduled_at < %s) "
        "ORDER BY scheduled_at, source, event_key;",
        (start, end, EARNINGS_SOURCE, cutoff),
    ).fetchall()
    covered = sorted({r["source"] for r in coverage})
    return {
        "covered_sources": covered,
        "coverage": [{"source": r["source"], "covered_from": str(r["covered_from"]),
                      "covered_to": str(r["covered_to"]), "recorded_at": str(r["recorded_at"])} for r in coverage],
        "events": [{"source": r["source"], "event_key": r["event_key"], "name": r["name"], "tier": r["tier"],
                    "scheduled_at": iso(_utc(r["scheduled_at"])), "time_et": _et(_utc(r["scheduled_at"])),
                    "before_cutoff": _utc(r["scheduled_at"]) < cutoff, "source_covered": r["source"] in covered,
                    "recorded_at": str(r["recorded_at"])} for r in rows],
        "note": "no coverage row vouches for this day: unknown, not 'no event'" if not covered else None,
    }


def _intermarket(conn, session: cal.Session, cutoff: datetime, symbol: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    collected = set(Config.collect_symbols())
    for src in ASSET_SOURCES.values():
        if src.symbol is None or src.symbol == symbol or src.symbol not in collected:
            continue
        row = conn.execute("SELECT contract_id FROM active_contracts WHERE symbol = %s AND trading_day = %s;",
                           (src.symbol, session.session_date.isoformat())).fetchone()
        if row is None:
            out[src.asset] = {"symbol": src.symbol, "status": "no_contract", "value": None}
            continue
        last = conn.execute(
            "SELECT timestamp_utc, close FROM bars WHERE contract_id = %s AND interval = '1m' "
            "AND price_type = 'TRADES' AND timestamp_utc <= %s AND timestamp_utc > %s "
            "ORDER BY timestamp_utc DESC LIMIT 1;",
            (row[0], cutoff - MINUTE, cutoff - timedelta(days=4)),
        ).fetchone()
        if last is None:
            out[src.asset] = {"symbol": src.symbol, "contract_id": int(row[0]), "status": "missing", "value": None}
            continue
        start = _utc(last[0])
        age = _minutes(start + MINUTE, cutoff)
        fresh = src.max_age_minutes is None or age <= src.max_age_minutes
        out[src.asset] = {"symbol": src.symbol, "contract_id": int(row[0]), "bar_start_at": iso(start),
                          "age_minutes": age, "max_age_minutes": src.max_age_minutes,
                          "status": "valid" if fresh else "stale", "value": dec(last[1]) if fresh else None}
    return out


# --------------------------------------------------------------------------
# The snapshot
# --------------------------------------------------------------------------

def _db_time(value) -> Optional[datetime]:
    """A TIMESTAMPTZ as the journal connection reads it (UTC text) - or a datetime - as an aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    t = datetime.fromisoformat(str(value).replace(" ", "T"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _availability(conn, capture_id: str, cid: int, session: cal.Session, cutoff: datetime, overnight: Sequence[Bar],
                  prev_archive: Dict[str, Any], prior: Dict[str, Any], daily_inputs: list,
                  events: Dict[str, Any]) -> Dict[str, Any]:
    """
    Point-in-time evidence of a live snapshot (the live issue policy): every overnight bar equal to a
    receipt of the capture received before the freeze, every earlier session it reads stored before the cutoff,
    every event and coverage row recorded before it. ``verified`` only when all of them hold.
    """
    frozen_at = _db_time(conn.execute("SELECT clock_timestamp();").fetchone()[0])
    receipts = {}
    for r in conn.execute("SELECT bar_start_at, open, high, low, close, volume, received_at FROM journal.bar_receipts "
                          "WHERE capture_id = %s AND contract_id = %s ORDER BY receipt_id;", (capture_id, cid)).fetchall():
        receipts[_db_time(r[0])] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5]), _db_time(r[6]))
    missing, differing, late = [], [], []
    for start, o, h, low, c, v in overnight:
        r = receipts.get(start)
        if r is None:
            missing.append(iso(start))
        elif r[:5] != (o, h, low, c, v):
            differing.append(iso(start))
        elif r[5] > frozen_at:
            late.append(iso(start))
    last = max((r for r in receipts.items() if r[0] < cutoff), key=lambda kv: kv[0], default=None)

    days = set()
    if prev_archive.get("session_date"):
        days.add((cid, prev_archive["session_date"]))
    for x in (prior or {}).get("sessions") or []:
        days.add((int(prior["contract_id"]), x["session_date"]))
    for row in daily_inputs:
        days.add((int(row[1]), row[0]))
    if daily_inputs:
        days.add((int(daily_inputs[0][1]), cal.previous_session(date.fromisoformat(daily_inputs[0][0]))
                  .session_date.isoformat()))
    stored = {}
    if days:
        for r in conn.execute("SELECT contract_id, trading_day, fetched_at FROM session_days WHERE interval = '1m' "
                              "AND price_type = 'TRADES' AND (contract_id, trading_day) IN "
                              "(SELECT * FROM unnest(%s::bigint[], %s::date[]));",
                              ([c for c, _ in days], [d for _, d in days])).fetchall():
            stored[(int(r[0]), str(r[1]))] = _db_time(r[2])
    sessions_late = sorted(f"{d} ({c})" for c, d in days if stored.get((c, d)) is None or stored[(c, d)] > cutoff)

    rows = list(events.get("events") or []) + list(events.get("coverage") or [])
    events_late = sorted(f"{e.get('source')}:{e.get('event_key') or e.get('covered_from')}" for e in rows
                         if _db_time(e.get("recorded_at")) is None or _db_time(e["recorded_at"]) > cutoff)
    verified = bool(overnight) and not (missing or differing or late or sessions_late or events_late)
    return {
        "capture_id": capture_id, "frozen_at": iso(frozen_at), "verified": verified,
        "overnight_bars": {"used": len(overnight), "received": len(overnight) - len(missing) - len(differing)
                           - len(late), "missing": missing[:5], "differing": differing[:5],
                           "received_after_freeze": late[:5]},
        "sessions": {"read": len(days), "stored_after_cutoff_or_unknown": sessions_late[:5]},
        "events": {"rows": len(rows), "recorded_after_cutoff_or_unknown": events_late[:5]},
        "last_received_bar": None if last is None else {"bar_start_at": iso(last[0]), "received_at": iso(last[1][5])},
        "rule": "live-capture verification: overnight bars equal to a receipt of the capture received before the "
                "freeze; earlier sessions stored and events recorded before the cutoff",
    }


def build_snapshot(conn, session_date, profile: str = defs.DEFAULT_PROFILE, symbol: str = defs.SYMBOL,
                   live_capture_id: Optional[str] = None, as_of: Optional[datetime] = None) -> Snapshot:
    """
    Freezes ``symbol``'s evidence for ``session_date`` at ``profile``'s cutoff (see the module docstring); with
    ``live_capture_id`` a live capture, its point-in-time evidence in the cutoff section. ``as_of`` is a preview's
    earlier cutoff (forecaster/preview.py): the evidence as it stood at that minute, marked ``preview`` in the
    cutoff section - the journal refuses to store it.
    """
    p = defs.PROFILES[profile]
    try:
        session = cal.session(session_date)
    except cal.CalendarCoverageError as e:
        raise SnapshotError(str(e)) from None
    if not session.is_open:
        raise SnapshotError(f"{session.session_date} is not a scheduled session")
    day = session.session_date.isoformat()
    cutoff = cal.ny_instant(session.session_date, p.cutoff)
    if as_of is not None:
        as_of = as_of.astimezone(timezone.utc).replace(second=0, microsecond=0)
        if not session.overnight_start_at < as_of <= cutoff:
            raise SnapshotError(f"a preview cutoff must fall after {day}'s overnight start and by its "
                                f"{p.cutoff:%H:%M} ET cutoff, not {as_of.astimezone(cal.NY_TZ):%Y-%m-%d %H:%M} ET")
        cutoff = as_of

    with conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;")
        contract = conn.execute(
            "SELECT a.rule, c.* FROM active_contracts a JOIN contracts c ON c.contract_id = a.contract_id "
            "WHERE a.symbol = %s AND a.trading_day = %s;", (symbol, day)).fetchone()
        if contract is None:
            raise SnapshotError(f"no active {symbol} contract recorded for {day}")
        cid = int(contract["contract_id"])

        overnight = _read_bars(conn, cid, session.overnight_start_at, cutoff - MINUTE)
        refs = _overnight(overnight, session, cutoff)
        coverage = refs.pop("_coverage")
        prev_refs, prev_archive = _previous_rth(conn, cid, session)
        refs.update(prev_refs)
        prior_sessions = _prior_sessions(conn, cid, session)
        daily, daily_inputs = _daily_atr(conn, symbol, session, cid)
        events = _events(conn, session, cutoff)
        intermarket = _intermarket(conn, session, cutoff, symbol)
        availability = (None if live_capture_id is None else
                        _availability(conn, live_capture_id, cid, session, cutoff, overnight, prev_archive,
                                      prior_sessions, daily_inputs, events))

    buckets = {m: aggregate(overnight, m, cutoff) for m in (2, 5, 15)}
    two_minute = _two_minute_atr(buckets[2], cutoff)
    moving = _moving_averages(buckets[2], session, cutoff)
    t, b = defs.threshold_t(two_minute.get("exact")), defs.threshold_b(daily.get("exact"))
    last = overnight[-1][0] if overnight else None
    data_mode, pit = ("preview" if as_of is not None else "historical_reconstruction"), "unverified_historical"
    if availability is not None:
        data_mode, pit = "live_capture", "verified" if availability["verified"] else "unverified_historical"

    payload = {
        "identity": {"symbol": symbol, "session_date": day, "weekday": session.session_date.strftime("%A"),
                     "contract_id": cid, "expiry": contract["expiry"], "local_symbol": contract["local_symbol"],
                     "active_contract_rule": contract["rule"]},
        "schedule": {"calendar_version": cal.CALENDAR_VERSION, "schedule": session.schedule,
                     "rth_open_at": iso(session.rth_open_at), "scheduled_close_at": iso(session.scheduled_close_at),
                     "overnight_start_at": iso(session.overnight_start_at)},
        "cutoff": {"profile": p.name, "cutoff_et": cutoff.astimezone(cal.NY_TZ).strftime("%H:%M"),
                   "input_cutoff_at": iso(cutoff), **({"preview": True} if as_of is not None else {}),
                   "last_completed_bar": None if last is None else
                   {"bar_start_at": iso(last), "bar_end_at": iso(last + MINUTE)},
                   "last_received_bar": None if availability is None else availability["last_received_bar"],
                   "last_received_note": ("no receipt records: a historical reconstruction cannot show when a "
                                          "bar was received") if availability is None else
                   f"receipts of live capture {live_capture_id} (journal.bar_receipts)",
                   "data_mode": data_mode, "pit_availability_status": pit,
                   **({} if availability is None else {"availability": availability})},
        "references": refs,
        "atr": {"daily": daily, "two_minute": two_minute},
        "thresholds": {"T": t, "B": b, "A": daily.get("exact")},
        "bars": {"window": [iso(session.overnight_start_at), iso(cutoff)], "coverage": coverage,
                 "1m": _bar_rows(overnight),
                 **{f"{m}m": [[iso(s), o, h, low, c, v, n, ok] for s, o, h, low, c, v, n, ok in buckets[m]]
                    for m in (2, 5, 15)}},
        "moving_averages": moving,
        "first_level_candidates": _first_level_candidates(refs, moving),
        "previous_rth_bars": prev_archive,
        "prior_sessions": prior_sessions,
        "daily_atr_inputs": daily_inputs,
        "events": events,
        "intermarket": intermarket,
        "chart_render_hash": None,
    }
    digest = hashlib.sha256(defs.canonical_json(payload).encode()).hexdigest()
    return Snapshot(symbol=symbol, contract_id=cid, session_date=day, snapshot_version=p.snapshot_version,
                    convention_version=defs.CONVENTION_VERSION, cutoff_at=cutoff, rth_open_at=session.rth_open_at,
                    data_mode=data_mode, pit_availability_status=pit, payload=payload, source_payload_hash=digest)
