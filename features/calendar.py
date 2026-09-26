# features/calendar.py
"""
Versioned RTH session calendar for the v2 feature contract.

Exchange calendars are source data, not something OHLCV can tell us, so the
schedule lives here as an explicit, versioned table rather than being inferred
from which bars happen to exist. ``CALENDAR_VERSION`` is recorded with every
snapshot; changing any entry below means bumping it.

Conventions (all instants are returned as tz-aware UTC datetimes):

  * A session is identified by its ET date. RTH is normally [09:30, 16:00) ET;
    on an early-close day the scheduled close is 13:00 ET. This is the cash
    (NYSE/Nasdaq) schedule the NQ RTH features refer to - not CME settlement.
  * The feature cutoff T is 09:29:00 ET. Only bars with bar_end_at <= T are
    inputs, so the latest input bar starts at 09:28.
  * The overnight (ON) window of session D runs from 18:00 ET on the calendar
    day before D through T. For a Monday that is Sunday evening. CME Globex
    reopens at 18:00 ET before every trading session, so the window contains
    no scheduled closure.
  * ``closed`` sessions (weekends, full holidays) have no RTH and no close.

The coverage range is explicit: a date outside it raises
``CalendarCoverageError`` rather than being silently assumed a full session.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import List, Optional

import pytz

NY_TZ = pytz.timezone("America/New_York")

CALENDAR_VERSION = "us_equity_rth_2024_2027_v1"
COVERAGE_START = date(2024, 1, 1)
COVERAGE_END = date(2027, 12, 31)

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
CUTOFF = time(9, 29)
OVERNIGHT_START = time(18, 0)

SCHEDULES = ("full", "early_close", "closed")

# Full-day closures of the US equity cash session (NYSE published schedules).
RTH_HOLIDAYS = frozenset({
    date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
    date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
    date(2024, 11, 28), date(2024, 12, 25),
    date(2025, 1, 1),
    date(2025, 1, 9),    # National Day of Mourning (President Carter): NYSE closed
    date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
})

# Scheduled early closes (13:00 ET).
RTH_EARLY_CLOSES = frozenset({
    date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24),
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
})


class CalendarCoverageError(ValueError):
    """The date lies outside the range this calendar version describes."""


def _as_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def ny_instant(d: date, t: time) -> datetime:
    """The UTC instant of wall-clock ``t`` in New York on ``d`` (DST-aware)."""
    return NY_TZ.localize(datetime.combine(d, t)).astimezone(pytz.utc)


def is_covered(d) -> bool:
    return COVERAGE_START <= _as_date(d) <= COVERAGE_END


@dataclass(frozen=True)
class Session:
    """The calendar-derived schedule of one ET session date."""
    session_date: date
    schedule: str                          # full | early_close | closed
    cutoff_at: datetime                    # T = 09:29 ET
    overnight_start_at: datetime           # 18:00 ET the calendar day before
    rth_open_at: Optional[datetime]        # None when closed
    scheduled_close_at: Optional[datetime]  # None when closed

    @property
    def is_open(self) -> bool:
        return self.schedule != "closed"

    @property
    def is_early_close(self) -> bool:
        return self.schedule == "early_close"


def session(d) -> Session:
    """The schedule of session date ``d``. Raises outside the calendar's coverage."""
    d = _as_date(d)
    if not is_covered(d):
        raise CalendarCoverageError(
            f"{d} is outside calendar {CALENDAR_VERSION} ({COVERAGE_START}..{COVERAGE_END})"
        )
    if d.weekday() >= 5 or d in RTH_HOLIDAYS:
        schedule = "closed"
    elif d in RTH_EARLY_CLOSES:
        schedule = "early_close"
    else:
        schedule = "full"

    close_t = EARLY_CLOSE if schedule == "early_close" else RTH_CLOSE
    return Session(
        session_date=d,
        schedule=schedule,
        cutoff_at=ny_instant(d, CUTOFF),
        overnight_start_at=ny_instant(d - timedelta(days=1), OVERNIGHT_START),
        rth_open_at=ny_instant(d, RTH_OPEN) if schedule != "closed" else None,
        scheduled_close_at=ny_instant(d, close_t) if schedule != "closed" else None,
    )


def previous_session(d) -> Session:
    """The last scheduled (non-closed) session strictly before ``d``."""
    d = _as_date(d) - timedelta(days=1)
    while True:
        s = session(d)
        if s.is_open:
            return s
        d -= timedelta(days=1)


def sessions_before(d, n: int) -> List[Session]:
    """The ``n`` scheduled sessions strictly before ``d``, oldest first.

    Stops early (returning fewer) at the calendar's coverage start."""
    out: List[Session] = []
    cur = _as_date(d)
    while len(out) < n:
        cur -= timedelta(days=1)
        if not is_covered(cur):
            break
        s = session(cur)
        if s.is_open:
            out.append(s)
    out.reverse()
    return out


def sessions_between(start, end) -> List[Session]:
    """Scheduled sessions with start <= date <= end, oldest first."""
    out, cur, end = [], _as_date(start), _as_date(end)
    while cur <= end:
        s = session(cur)
        if s.is_open:
            out.append(s)
        cur += timedelta(days=1)
    return out


def session_distance(a, b) -> int:
    """Number of scheduled sessions stepped from ``a`` to ``b`` (0 when equal)."""
    a, b = _as_date(a), _as_date(b)
    lo, hi = min(a, b), max(a, b)
    if lo == hi:
        return 0
    return len(sessions_between(lo + timedelta(days=1), hi))


# --------------------------------------------------------------------------
# Expirations and rolls
# --------------------------------------------------------------------------

def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _business_day_on_or_before(d: date) -> date:
    while d.weekday() >= 5 or d in RTH_HOLIDAYS:
        d -= timedelta(days=1)
    return d


def monthly_opex_date(year: int, month: int) -> date:
    """The designated monthly equity/index options expiration: the third Friday,
    or the business day before it when that Friday is an exchange holiday."""
    return _business_day_on_or_before(_third_friday(year, month))


def monthly_opex_week(d) -> bool:
    """True when ``d`` falls in the Monday-Friday week containing that month's
    designated monthly options expiration."""
    d = _as_date(d)
    opex = monthly_opex_date(d.year, d.month)
    monday = d - timedelta(days=d.weekday())
    return monday <= opex <= monday + timedelta(days=4)


def equity_index_future_expiry(year: int, month: int) -> date:
    """Last trading day of a quarterly CME equity-index future (third Friday,
    earlier if that Friday is a holiday)."""
    return _business_day_on_or_before(_third_friday(year, month))


def equity_roll_date(expiry: date, days_before_expiry: int) -> date:
    """
    The first session on which the next contract is front under a ``RollRule``
    with this ``days_before_expiry``: collector.rolls keeps a contract active
    while ``expiry - days_before_expiry > day``.
    """
    d = expiry - timedelta(days=days_before_expiry)
    while not session(d).is_open:
        d += timedelta(days=1)
    return d


def roll_transition(d, months: str, days_before_expiry: int, sessions_either_side: int = 2) -> bool:
    """
    True on a calendar-defined roll date and ``sessions_either_side`` scheduled
    sessions either side of it. ``months`` are the IB month codes of the cycle.
    """
    from config import MONTH_CODES  # local: config imports nothing from features

    d = _as_date(d)
    for year in (d.year - 1, d.year, d.year + 1):
        for code in months:
            month = MONTH_CODES.index(code) + 1
            expiry = equity_index_future_expiry(year, month)
            if abs((expiry - d).days) > 60:
                continue
            try:
                roll = equity_roll_date(expiry, days_before_expiry)
                if session_distance(d, roll) <= sessions_either_side:
                    return True
            except CalendarCoverageError:
                continue
    return False
