# forecaster/preopen.py
"""
What is known before the open, from the stored 1-minute bars of each day's
active contract (and spot VIX, and the economic calendar) - the inputs of the
first-hour model (forecaster/first_hour_model.py). No feature snapshot is
needed, so it works for any forecast instrument.

Sessions
    ``Session``: one day's regular session so far (a live day: the minutes held,
    or none yet before the open) and its pre-open figures, from the whole
    Globex day (18:00 ET the evening before on). P, the price the forecast
    starts from, is the close of the 09:28 bar - the last one complete at 09:29.

Inputs (``pre_inputs``), each against its usual - the median over the previous
USUAL_SESSIONS sessions - as a clipped log ratio unless noted:
    volatility  previous RTH range; mean RTH range of the last 5 and 22
                sessions; previous first-hour range; mean first-hour range of the
                last 5; overnight range and volume (18:00 to 09:29); range and
                volume of the last hour before 09:29; spot VIX (its log level,
                against its usual, and its change since the previous day's last
                print); Monday; Friday
    direction   returns to P from the overnight open, from 08:28 and from 09:13,
                and the previous session's open-to-close return - each in usual
                RTH ranges; where P sits in the overnight range, and where the
                previous session closed in its own range (both centred on 0)

Scheduled releases (``Events``): the economic calendar loaded by
database/events.py, for display. Tested as inputs to the range forecast they
added nothing measurable (see README, "Economic calendar").
"""

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from features import calendar as cal

USUAL_SESSIONS = 40      # the usual: median over this many previous sessions
RATIO_CLIP = (0.1, 10.0)
MAX_MISSING = 5          # regular-session minutes a stored day may lack and still count as complete
SESSION_MINUTES = 390
VIX_FRESH_MINUTES = 20

VOLATILITY_INPUTS = ("prev_rth_range", "rth_range_5d", "rth_range_22d", "prev_first_hour_range",
                     "first_hour_range_5d", "overnight_range", "overnight_volume", "last_hour_range",
                     "last_hour_volume", "vix_log_level", "vix_vs_usual", "vix_change", "monday", "friday")
DIRECTION_INPUTS = ("overnight_return", "last_hour_return", "last_15m_return", "prev_session_return",
                    "overnight_position", "prev_close_location")
PRE_INPUTS = VOLATILITY_INPUTS + DIRECTION_INPUTS


# --------------------------------------------------------------------------
# Sessions from bars
# --------------------------------------------------------------------------

@dataclass
class Session:
    """One day's regular session so far, and what was known before its open."""
    day: str
    minutes: int                          # scheduled regular-session minutes (390; 210 on an early close)
    open_at: pd.Timestamp                 # 09:30 ET
    open: float                           # the 09:30 bar's open (before the open: P)
    close: np.ndarray                     # per minute held; a minute without a print repeats the previous close
    high: np.ndarray
    low: np.ndarray
    bars: int = 0                         # regular-session bars actually stored
    pre_price: Optional[float] = None     # P: the 09:28 bar's close
    overnight_open: Optional[float] = None
    overnight_high: Optional[float] = None     # 18:00 -> 09:29
    overnight_low: Optional[float] = None
    overnight_range: Optional[float] = None    # ln high - ln low of the same
    overnight_volume: Optional[float] = None
    last_hour_range: Optional[float] = None    # the same over [08:29, 09:29)
    last_hour_volume: Optional[float] = None
    close_0828: Optional[float] = None    # closes 60 and 15 minutes before P's bar
    close_0913: Optional[float] = None
    vix: Optional[float] = None           # spot VIX before 09:29
    vix_close: Optional[float] = None     # the day's last VIX print
    contract_id: Optional[int] = None

    @property
    def held(self) -> int:
        """Regular-session minutes held: the latest bar's minute + 1 (a live day: so far)."""
        return len(self.close)

    @property
    def complete(self) -> bool:
        return self.held >= self.minutes - MAX_MISSING and self.bars >= self.minutes - MAX_MISSING

    def time_at(self, t: int) -> pd.Timestamp:
        return self.open_at + pd.Timedelta(minutes=int(t))


def _session(day: str, ts: pd.DatetimeIndex, o, h, l, c, v, contract_id=None) -> Optional[Session]:
    try:
        sched = cal.session(day)
    except cal.CalendarCoverageError:
        return None
    if sched.rth_open_at is None or sched.scheduled_close_at is None:
        return None
    open_at = pd.Timestamp(sched.rth_open_at).tz_convert("UTC")
    minutes = int((pd.Timestamp(sched.scheduled_close_at) - pd.Timestamp(sched.rth_open_at)).total_seconds() // 60)
    m = np.asarray((ts - open_at).total_seconds() // 60, dtype=int)
    first = np.flatnonzero(m == 0)
    rth = (m >= 0) & (m < minutes)
    pre = m < -1                     # bars starting before 09:29: the last is the 09:28 bar
    last_hour = pre & (m >= -61)     # [08:29, 09:29)
    if len(first):
        held = int(m[rth].max()) + 1
        opening = float(o[first[0]])
    elif not rth.any() and pre.any():
        held, opening = 0, float(c[pre][-1])      # before the open: the last pre-open price stands in
    else:
        return None
    close, high, low = (np.full(held, np.nan) for _ in range(3))
    close[m[rth]], high[m[rth]], low[m[rth]] = c[rth], h[rth], l[rth]
    close = pd.Series(close, dtype=float).ffill().to_numpy()
    high = np.where(np.isnan(high), close, high)
    low = np.where(np.isnan(low), close, low)

    def rng(mask):
        return float(np.log(h[mask].max()) - np.log(l[mask].min())) if mask.any() else None

    def close_by(minute):            # the close of the last bar starting at or before ``minute``
        k = np.flatnonzero(pre & (m <= minute))
        return float(c[k[-1]]) if len(k) else None

    return Session(day=day, minutes=minutes, open_at=open_at, open=opening, close=close, high=high,
                   low=low, bars=int(rth.sum()), pre_price=float(c[pre][-1]) if pre.any() else None,
                   overnight_open=float(o[pre][0]) if pre.any() else None,
                   overnight_high=float(h[pre].max()) if pre.any() else None,
                   overnight_low=float(l[pre].min()) if pre.any() else None,
                   overnight_range=rng(pre), overnight_volume=float(v[pre].sum()) if pre.any() else None,
                   last_hour_range=rng(last_hour),
                   last_hour_volume=float(v[last_hour].sum()) if last_hour.any() else None,
                   close_0828=close_by(-62), close_0913=close_by(-17), contract_id=contract_id)


def sessions_from_bars(bars: pd.DataFrame) -> List[Session]:
    """
    Sessions from 1-minute bars (``trading_day``, ``timestamp_utc``, OHLC,
    ``volume``; optionally ``contract_id``) holding one contract per day - the
    whole Globex day, from 18:00 ET the evening before. A day without a
    scheduled regular session, or with regular-session bars but not the 09:30
    one, is left out; a day with only pre-open bars yet holds no minute, P
    standing in for the open.
    """
    if bars is None or bars.empty:
        return []
    df = bars.assign(ts=pd.to_datetime(bars["timestamp_utc"], utc=True, format="ISO8601"),
                     trading_day=bars["trading_day"].astype(str))
    df = df.sort_values(["trading_day", "ts"], kind="stable")
    days = df["trading_day"].to_numpy()
    ts = pd.DatetimeIndex(df["ts"])
    cols = {k: df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume")}
    cids = df["contract_id"].to_numpy() if "contract_id" in df.columns else None
    edges = np.flatnonzero(days[1:] != days[:-1]) + 1
    out = []
    for a, b in zip(np.concatenate([[0], edges]), np.concatenate([edges, [len(days)]])):
        s = _session(str(days[a]), ts[a:b], *(cols[k][a:b] for k in ("open", "high", "low", "close", "volume")),
                     contract_id=int(cids[a]) if cids is not None else None)
        if s is not None:
            out.append(s)
    return out


def _padded(s: Session) -> Session:
    """A complete session with its last few missing minutes repeating the last close."""
    if s.held >= s.minutes:
        return s
    pad = s.minutes - s.held
    last = s.close[-1]
    return Session(**{**s.__dict__, "close": np.concatenate([s.close, np.full(pad, last)]),
                      "high": np.concatenate([s.high, np.full(pad, last)]),
                      "low": np.concatenate([s.low, np.full(pad, last)])})


def complete_sessions(sessions: Sequence[Session], vix: Optional[Dict[str, Tuple]] = None) -> List[Session]:
    """The complete full-day sessions with a pre-open price, oldest first, padded, spot VIX attached."""
    vix = vix or {}
    kept = []
    for s in sessions:
        if s.minutes == SESSION_MINUTES and s.complete and s.pre_price:
            s = _padded(s)
            s.vix, s.vix_close = (vix.get(s.day) or (None, None))[:2]
            kept.append(s)
    return sorted(kept, key=lambda s: s.day)


def load_sessions(conn, symbol: str) -> List[Session]:
    """Every stored session of ``symbol``'s active contracts (``complete_sessions`` of them: the training set)."""
    from database.queries import active_contract_bars
    bars = pd.DataFrame([dict(r) for r in active_contract_bars(conn, symbol)])
    return complete_sessions(sessions_from_bars(bars), load_vix(conn))


def load_vix(conn, day: Optional[str] = None) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    """{day: (spot VIX before 09:29, the day's last VIX print)}; {} when VIX is not collected."""
    from config import Config
    from database.queries import get_contract_by_expiry, preopen_levels
    if "VIX" not in Config.CONTEXT_SYMBOLS:
        return {}
    contract = get_contract_by_expiry(conn, "VIX", Config.expiry_for("VIX"))
    if contract is None:
        return {}
    f = lambda v: None if v is None else float(v)
    return {str(r["trading_day"]): (f(r["pre"]), f(r["last"]))
            for r in preopen_levels(conn, contract["contract_id"], VIX_FRESH_MINUTES, day)}


def day_session(conn, rows: Sequence[Any]) -> Optional[Session]:
    """
    One day's Session from its stored 1-minute bars on one contract
    (``get_day_bars`` rows: the contract shown, whichever it is), with spot VIX.
    """
    found = sessions_from_bars(pd.DataFrame([dict(r) for r in rows])) if rows else []
    if not found:
        return None
    s = found[0]
    s.vix, s.vix_close = load_vix(conn, s.day).get(s.day) or (None, None)
    return s


# --------------------------------------------------------------------------
# Scheduled releases (economic_events)
# --------------------------------------------------------------------------

@dataclass
class Events:
    """
    Scheduled releases by session day - (minute from the day's 09:30, tier,
    name, New York time) - and the days the calendar covers. On a covered day
    without releases there were none; on any other day the calendar is missing.
    """
    by_day: Dict[str, List[Tuple[int, str, str, str]]] = field(default_factory=dict)
    covered: set = field(default_factory=set)

    def of(self, day: str) -> Optional[List[Tuple[int, str, str, str]]]:
        """The day's releases, or None when the calendar does not cover it."""
        return self.by_day.get(day, []) if day in self.covered else None


def events_from_rows(events: Sequence[Any], coverage: Sequence[Any]) -> Events:
    """
    ``Events`` from calendar rows (source, scheduled_at, name, tier) and coverage
    rows (source, covered_from, covered_to). As in the v2 snapshot, a day counts
    the releases of the sources covering it.
    """
    out = Events()
    by_source: Dict[str, List[Tuple[date, date]]] = {}
    for c in coverage:
        a, b = (date.fromisoformat(str(c[k])[:10]) for k in ("covered_from", "covered_to"))
        by_source.setdefault(c["source"], []).append((a, b))
    if not by_source:
        return out
    first, last = min(a for w in by_source.values() for a, _ in w), max(b for w in by_source.values() for _, b in w)
    for s in cal.sessions_between(first, last):
        d = s.session_date
        if any(a <= d <= b for w in by_source.values() for a, b in w):
            out.covered.add(d.isoformat())
    for e in events:
        at = pd.Timestamp(e["scheduled_at"])
        at = at.tz_localize("UTC") if at.tzinfo is None else at
        ny = at.tz_convert("America/New_York")
        day = ny.date()
        if not any(a <= day <= b for a, b in by_source.get(e["source"], [])):
            continue
        try:
            s = cal.session(day)
        except cal.CalendarCoverageError:
            continue
        if s.rth_open_at is None:
            continue
        minute = int((at - pd.Timestamp(s.rth_open_at)).total_seconds() // 60)
        out.by_day.setdefault(day.isoformat(), []).append((minute, e["tier"], e["name"], ny.strftime("%H:%M")))
    for v in out.by_day.values():
        v.sort()
    return out


def load_events(conn) -> Events:
    """The loaded economic calendar (``economic_events`` within ``economic_event_coverage``)."""
    from database.queries import economic_calendar
    events, coverage = economic_calendar(conn)
    return events_from_rows(events, coverage)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def _log_ratio(value, usual) -> float:
    if value is None or usual is None or not np.isfinite(value) or not np.isfinite(usual) or usual <= 0 or value < 0:
        return math.nan
    return math.log(min(max(value / usual, RATIO_CLIP[0]), RATIO_CLIP[1]))


def nanmedian(a) -> float:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    return float(np.median(a)) if len(a) else math.nan


def _nanmean(a) -> float:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    return float(a.mean()) if len(a) else math.nan


def session_ranges(s: Session) -> Tuple[float, float]:
    """(regular-session range, first-hour range) of a complete session: ln high - ln low, open included."""
    def rng(n):
        return float(np.log(max(s.open, s.high[:n].max())) - np.log(min(s.open, s.low[:n].min())))
    return rng(s.minutes), rng(min(60, s.minutes))


def daily(sessions: Sequence[Session]) -> Dict[str, np.ndarray]:
    """Per-session figures the pre-open inputs compare against, oldest first."""
    ranges = [session_ranges(s) for s in sessions]
    f = lambda v: np.nan if v is None else float(v)
    return {"rth_range": np.array([r[0] for r in ranges]), "first_hour_range": np.array([r[1] for r in ranges]),
            "overnight_range": np.array([f(s.overnight_range) for s in sessions]),
            "overnight_volume": np.array([f(s.overnight_volume) for s in sessions]),
            "last_hour_range": np.array([f(s.last_hour_range) for s in sessions]),
            "last_hour_volume": np.array([f(s.last_hour_volume) for s in sessions]),
            "vix": np.array([f(s.vix) for s in sessions]), "vix_close": np.array([f(s.vix_close) for s in sessions]),
            "session_return": np.array([math.log(s.close[-1] / s.open) for s in sessions]),
            "close_location": np.array([(s.close[-1] - s.low.min()) / (s.high.max() - s.low.min())
                                        if s.high.max() > s.low.min() else 0.5 for s in sessions])}


def _ret(a: Optional[float], b: Optional[float], unit: float) -> float:
    """ln(a / b) in ``unit``s; NaN when either is missing."""
    if not a or not b or not np.isfinite(unit) or unit <= 0:
        return math.nan
    return math.log(a / b) / unit


def pre_inputs(prev: Dict[str, np.ndarray], today: Session) -> np.ndarray:
    """
    PRE_INPUTS of ``today`` from the figures of the sessions before it
    (``daily``, oldest first) - all NaN with fewer than USUAL_SESSIONS of them.
    """
    if len(prev["rth_range"]) < USUAL_SESSIONS:
        return np.full(len(PRE_INPUTS), np.nan)
    usual = {k: nanmedian(v[-USUAL_SESSIONS:]) for k, v in prev.items()}
    rth, fh = prev["rth_range"], prev["first_hour_range"]
    weekday = date.fromisoformat(today.day).weekday()
    vix = today.vix if today.vix and today.vix > 0 else None
    P, u = today.pre_price, usual["rth_range"]
    hi, lo = today.overnight_high, today.overnight_low
    return np.array([
        _log_ratio(rth[-1], u),
        _log_ratio(_nanmean(rth[-5:]), u),
        _log_ratio(_nanmean(rth[-22:]), u),
        _log_ratio(fh[-1], usual["first_hour_range"]),
        _log_ratio(_nanmean(fh[-5:]), usual["first_hour_range"]),
        _log_ratio(today.overnight_range, usual["overnight_range"]),
        _log_ratio(today.overnight_volume, usual["overnight_volume"]),
        _log_ratio(today.last_hour_range, usual["last_hour_range"]),
        _log_ratio(today.last_hour_volume, usual["last_hour_volume"]),
        math.log(vix) if vix else math.nan,
        _log_ratio(vix, usual["vix"]),
        _log_ratio(vix, prev["vix_close"][-1]),
        float(weekday == 0), float(weekday == 4),
        _ret(P, today.overnight_open, u),
        _ret(P, today.close_0828, u),
        _ret(P, today.close_0913, u),
        prev["session_return"][-1] / u if u > 0 else math.nan,
        (P - lo) / (hi - lo) - 0.5 if P and hi and lo and hi > lo else math.nan,
        prev["close_location"][-1] - 0.5,
    ])
