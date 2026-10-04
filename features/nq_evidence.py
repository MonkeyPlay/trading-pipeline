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
                       low, cutoff price - each with a status; Price at 09:29 and
                       premarket high / low are unavailable by convention
  atr, thresholds      frozen daily and 2-minute Wilder ATR(14) and T, B, A
  bars                 the overnight window's 1m bars and their complete 2m, 5m and
                       15m clock buckets; previous_rth_bars, daily_atr_inputs
  events, intermarket  economic calendar rows of the day, last observations of the
                       other collected instruments

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
from datetime import datetime, timedelta, timezone
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
    from time-ordered 1m bars: (start, open, high, low, close, volume, 1m bars).
    Only buckets that ended by ``cutoff``; an empty bucket produces no bar. New
    York's offset from UTC is whole hours, so UTC minute alignment is ET alignment.
    """
    out: Dict[datetime, list] = {}
    for start, o, h, low, c, v in bars:
        epoch = int(start.timestamp()) // 60
        b_start = datetime.fromtimestamp((epoch - epoch % minutes) * 60, timezone.utc)
        if b_start + timedelta(minutes=minutes) > cutoff:
            continue
        b = out.get(b_start)
        if b is None:
            out[b_start] = [o, h, low, c, v, 1]
        else:
            b[1], b[2], b[3], b[4], b[5] = max(b[1], h), min(b[2], low), c, b[4] + v, b[5] + 1
    return [(s, *out[s]) for s in sorted(out)]


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
    sessions = cal.sessions_before(session.session_date, defs.DAILY_ATR["search_sessions"])
    if len(sessions) < 2:
        return {"value": None, "status": "insufficient_history", "true_ranges": 0}, []
    try:
        sessions = [cal.previous_session(sessions[0].session_date)] + sessions
    except cal.CalendarCoverageError:
        pass
    active = _active_contracts(conn, symbol, sessions[0].session_date, sessions[-1].session_date)
    rth = _rth_aggregates(conn, set(active.values()) | {contract_id}, sessions)

    def complete(cid, s):
        row = rth.get((cid, s.session_date.isoformat()))
        return row if row is not None and row["n"] == _rth_minutes(s) else None

    used, skipped = [], 0
    for prev, s in zip(sessions, sessions[1:]):
        cid = active.get(s.session_date.isoformat())
        today = complete(cid, s) if cid is not None else None
        before = complete(cid, prev) if cid is not None else None
        if today is None or before is None:
            skipped += 1
            continue
        tr = true_range(today["high"], today["low"], before["close"])
        used.append([s.session_date.isoformat(), cid, today["high"], today["low"], today["close"],
                     before["close"], tr])
    used = used[-window:]
    if len(used) < window:
        return {"value": None, "status": "insufficient_history", "true_ranges": len(used),
                "skipped_sessions": skipped}, used
    atr = wilder_atr([u[6] for u in used], period)
    return {"value": _shown(atr), "exact": atr, "status": "valid", "period": period, "true_ranges": len(used),
            "skipped_sessions": skipped, "first_session": used[0][0], "last_session": used[-1][0]}, used


def _two_minute_atr(buckets: Sequence[tuple]) -> Dict[str, Any]:
    """The frozen 2-minute ATR from complete 2m buckets (convention two_minute_atr)."""
    period, window = defs.TWO_MINUTE_ATR["period"], defs.TWO_MINUTE_ATR["window_true_ranges"]
    if len(buckets) < window + 1:
        return {"value": None, "status": "insufficient_history", "buckets": len(buckets)}
    use = buckets[-(window + 1):]
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


def _overnight(bars: Sequence[Bar], session: cal.Session, cutoff: datetime) -> Dict[str, Any]:
    expected = _minutes(session.overnight_start_at, cutoff)
    coverage = Decimal(len(bars)) / Decimal(expected) if expected else Decimal(0)
    refs: Dict[str, Any] = {}
    if bars and bars[0][0] == session.overnight_start_at:
        refs["overnight_open"] = _ref(dec(bars[0][1]))
    else:
        refs["overnight_open"] = _ref(None, "missing", "no bar starting 18:00 ET")
    if bars and coverage >= defs.ON_MIN_COVERAGE:
        refs["on_high"] = _ref(max(dec(b[2]) for b in bars))
        refs["on_low"] = _ref(min(dec(b[3]) for b in bars))
    else:
        detail = f"{len(bars)} of {expected} overnight minutes (< {defs.ON_MIN_COVERAGE:.0%})"
        refs["on_high"] = refs["on_low"] = _ref(None, "insufficient_coverage", detail)
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
    refs["premarket_high"] = refs["premarket_low"] = _ref(None, "not_defined",
                                                          "no premarket window in this convention")
    refs["_coverage"] = {"expected_minutes": expected, "minutes": len(bars), "ratio": coverage.quantize(
        Decimal("0.0001"))}
    return refs


def _events(conn, session: cal.Session, cutoff: datetime) -> Dict[str, Any]:
    day = session.session_date
    start = cal.ny_instant(day, datetime.min.time())
    end = cal.ny_instant(day + timedelta(days=1), datetime.min.time())
    coverage = conn.execute(
        "SELECT source, covered_from, covered_to, recorded_at FROM economic_event_coverage "
        "WHERE covered_from <= %s AND covered_to >= %s ORDER BY source;", (str(day), str(day)),
    ).fetchall()
    rows = conn.execute(
        "SELECT source, event_key, scheduled_at, name, tier, recorded_at FROM economic_events "
        "WHERE scheduled_at >= %s AND scheduled_at < %s ORDER BY scheduled_at, source, event_key;",
        (start, end),
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

def build_snapshot(conn, session_date, profile: str = defs.DEFAULT_PROFILE, symbol: str = defs.SYMBOL) -> Snapshot:
    """Freezes ``symbol``'s evidence for ``session_date`` at ``profile``'s cutoff (see the module docstring)."""
    p = defs.PROFILES[profile]
    try:
        session = cal.session(session_date)
    except cal.CalendarCoverageError as e:
        raise SnapshotError(str(e)) from None
    if not session.is_open:
        raise SnapshotError(f"{session.session_date} is not a scheduled session")
    day = session.session_date.isoformat()
    cutoff = cal.ny_instant(session.session_date, p.cutoff)

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
        daily, daily_inputs = _daily_atr(conn, symbol, session, cid)
        events = _events(conn, session, cutoff)
        intermarket = _intermarket(conn, session, cutoff, symbol)

    buckets = {m: aggregate(overnight, m, cutoff) for m in (2, 5, 15)}
    two_minute = _two_minute_atr(buckets[2])
    t, b = defs.threshold_t(two_minute.get("exact")), defs.threshold_b(daily.get("exact"))
    last = overnight[-1][0] if overnight else None
    data_mode, pit = "historical_reconstruction", "unverified_historical"

    payload = {
        "identity": {"symbol": symbol, "session_date": day, "weekday": session.session_date.strftime("%A"),
                     "contract_id": cid, "expiry": contract["expiry"], "local_symbol": contract["local_symbol"],
                     "active_contract_rule": contract["rule"]},
        "schedule": {"calendar_version": cal.CALENDAR_VERSION, "schedule": session.schedule,
                     "rth_open_at": iso(session.rth_open_at), "scheduled_close_at": iso(session.scheduled_close_at),
                     "overnight_start_at": iso(session.overnight_start_at)},
        "cutoff": {"profile": p.name, "cutoff_et": p.cutoff.strftime("%H:%M"), "input_cutoff_at": iso(cutoff),
                   "last_completed_bar": None if last is None else
                   {"bar_start_at": iso(last), "bar_end_at": iso(last + MINUTE)},
                   "last_received_bar": None,
                   "last_received_note": "no receipt records: a historical reconstruction cannot show when a "
                                         "bar was received",
                   "data_mode": data_mode, "pit_availability_status": pit},
        "references": refs,
        "atr": {"daily": daily, "two_minute": two_minute},
        "thresholds": {"T": t, "B": b, "A": daily.get("exact")},
        "bars": {"window": [iso(session.overnight_start_at), iso(cutoff)], "coverage": coverage,
                 "1m": _bar_rows(overnight),
                 **{f"{m}m": [[iso(s), o, h, low, c, v, n] for s, o, h, low, c, v, n in buckets[m]]
                    for m in (2, 5, 15)}},
        "previous_rth_bars": prev_archive,
        "daily_atr_inputs": daily_inputs,
        "events": events,
        "intermarket": intermarket,
        "chart_render_hash": None,
    }
    digest = hashlib.sha256(defs.canonical_json(payload).encode()).hexdigest()
    return Snapshot(symbol=symbol, contract_id=cid, session_date=day, snapshot_version=p.snapshot_version,
                    convention_version=defs.CONVENTION_VERSION, cutoff_at=cutoff, rth_open_at=session.rth_open_at,
                    data_mode=data_mode, pit_availability_status=pit, payload=payload, source_payload_hash=digest)
