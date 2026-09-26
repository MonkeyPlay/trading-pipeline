# collector/coverage.py
"""
Day-level collection planning.

The ``session_days`` ledger records one row per NY trading day we hold. Before
the collector opens a socket to IB it asks this module which days in the
requested window are still missing, so a day that is already stored is never
downloaded twice.

A day is judged as a whole. Minute-level gap filling is deliberately not
attempted: thin overnight periods legitimately have no trades, so a missing
minute is not a reliable signal — day-level bar counts are.

The holiday list is the versioned calendar in ``features/calendar.py``; extend
it there (and bump its ``CALENDAR_VERSION``) rather than here.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, List, Optional, Tuple

from database.queries import derive_day_status, expected_bars_for, get_stored_trading_days
from features import calendar as cal
from features.calendar import RTH_HOLIDAYS

# Full holidays of the US equity cash session. One list for the whole project:
# the versioned calendar in features/calendar.py (which also knows early closes).
_CME_HOLIDAYS = RTH_HOLIDAYS

# Re-fetch the trailing N sessions every run: the vendor revises recent bars and
# the most recent session may have been partial when it was last stored.
REFRESH_TRAILING_DAYS = 2

FETCH_ACTIONS = ("fetch", "refetch")

REGULAR_SESSION_MINUTES = 390     # 09:30-16:00 ET


def scheduled_rth_minutes(d: date) -> Optional[int]:
    """Minutes of the scheduled regular session on ``d`` (210 on an early close, 0
    when it is closed), or None outside the calendar's coverage."""
    if not cal.is_covered(d):
        return None
    s = cal.session(d)
    if not s.is_open:
        return 0
    return int((s.scheduled_close_at - s.rth_open_at).total_seconds() // 60)


def day_expectation(instrument, d: date) -> Tuple[Optional[int], Optional[int]]:
    """
    ``(expected bars for the whole trading day, regular-session bars required)``
    for ``instrument`` on ``d``.

    Only an instrument flagged ``rth_complete`` - one whose every regular-session
    minute trades and that the features and labels read minute by minute - must
    hold its whole regular session; the others are judged by their day count
    alone. For those, an early close (or a closed regular session) also lowers the
    day's expected count by the regular-session minutes it loses.
    """
    expected = instrument.expected_bars
    if not getattr(instrument, "rth_complete", False):
        return expected, None
    rth = scheduled_rth_minutes(d)
    if rth is None:
        return expected, None
    return expected - (REGULAR_SESSION_MINUTES - rth), rth


@dataclass
class DayPlan:
    """What the collector intends to do about one trading day."""
    trading_day: str                  # 'YYYY-MM-DD'
    action: str                       # 'fetch' | 'refetch' | 'ok'
    reason: str
    status: Optional[str] = None      # ledger status, None if the day is unknown
    have: int = 0                     # bars currently stored
    expected: Optional[int] = None    # rough expected bar count

    @property
    def needs_fetch(self) -> bool:
        return self.action in FETCH_ACTIONS


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _CME_HOLIDAYS


def expected_trading_days(start: date, end: date) -> List[date]:
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def previous_trading_day(d: date) -> date:
    """The last trading day strictly before ``d``."""
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def previous_trading_days(d: date, n: int) -> List[date]:
    """The ``n`` trading days strictly before ``d``, oldest first."""
    out = []
    while len(out) < n:
        d = previous_trading_day(d)
        out.append(d)
    return out[::-1]


def plan_trading_days(
    conn,
    contract_id: int,
    start: date,
    end: date,
    interval: str = "1m",
    price_type: str = "TRADES",
    rth_only: bool = False,
    force: bool = False,
    trailing_days: int = REFRESH_TRAILING_DAYS,
    expected: Optional[int] = None,
    extra_days: Iterable[date] = (),
    expectation: Optional[Callable[[date], Tuple[Optional[int], Optional[int]]]] = None,
) -> List[DayPlan]:
    """
    Decides, for every expected trading day in [start, end], whether it must be
    downloaded. This is a single indexed read of ``session_days`` — no IB traffic.

      fetch    - the day is not in the database at all
      refetch  - stored but incomplete, or recent enough that IB may still revise it
      ok       - already stored; skip it

    ``force=True`` re-downloads the whole window (the collector's ``--full``).
    ``expected`` is the instrument's own bar yardstick (default: the futures one).
    ``extra_days`` are planned too although they lie outside [start, end] - the
    collector uses it for the reference day before a contract becomes active.
    A day the source had no data for is stored as 'EMPTY' and treated as ``ok``:
    we asked once, and asking again every run would just burn the pacing budget.

    ``expectation(day) -> (expected bars, regular-session bars required)`` (see
    ``day_expectation``) re-judges every stored day from its counts under the
    current rule instead of trusting the status it was stored with, so a stricter
    rule also reaches days stored before it existed.
    """
    extra = {d for d in extra_days if not (start <= d <= end)}
    ledger = get_stored_trading_days(
        conn, contract_id, interval=interval, price_type=price_type,
        start=min([start, *extra]).isoformat(), end=max([end, *extra]).isoformat(),
    )
    if expected is None:
        expected = expected_bars_for(interval, rth_only=rth_only)
    trailing_cutoff = datetime.now(timezone.utc).date() - timedelta(days=trailing_days)

    # The holiday list is hand-maintained and can be wrong (CME runs shortened
    # sessions on some of the days in it). A day already in the ledger is evidence
    # the market traded, so consider it even if the calendar excludes it —
    # otherwise a partial holiday session could never be completed.
    candidates = {d.isoformat() for d in expected_trading_days(start, end)}
    candidates.update(k for k in ledger if start.isoformat() <= k <= end.isoformat())
    candidates.update(d.isoformat() for d in extra)

    plan = []
    for key in sorted(candidates):
        d = date.fromisoformat(key)
        row = ledger.get(key)
        have = int(row["bar_count"]) if row is not None else 0
        status = row["status"] if row is not None else None

        exp_day, exp_rth = expectation(d) if expectation is not None else (expected, None)
        short = _falls_short(row, exp_day, exp_rth) if expectation is not None else None

        if row is None:
            action, reason = "fetch", "not in database"
        elif force:
            action, reason = "refetch", "forced refresh"
        elif short is not None:
            action, reason, status = "refetch", short, "PARTIAL"
        elif status == "PARTIAL":
            reason = f"incomplete ({have}/{exp_day} bars)" if exp_day else f"incomplete ({have} bars)"
            action = "refetch"
        elif d >= trailing_cutoff:
            action, reason = "refetch", "trailing session (vendor may revise)"
        elif status == "EMPTY":
            action, reason = "ok", f"no data at source (checked {row['fetched_at']})"
        else:
            action, reason = "ok", f"{have} bars stored"

        plan.append(DayPlan(key, action, reason, status, have, exp_day))
    return plan


def _falls_short(row, expected: Optional[int], expected_rth: Optional[int]) -> Optional[str]:
    """Why a day stored as COMPLETE falls short of the current rule, or None."""
    if row is None or row["status"] != "COMPLETE":
        return None
    have, rth = int(row["bar_count"]), int(row["rth_bar_count"] or 0)
    if derive_day_status(have, expected, int(row["open_bar_count"] or 0), rth, expected_rth) != "PARTIAL":
        return None
    if expected_rth and rth < expected_rth:
        return f"regular session incomplete ({rth}/{expected_rth} bars)"
    return f"incomplete ({have}/{expected} bars)"


def days_to_fetch(plan: List[DayPlan]) -> List[str]:
    """The ordered 'YYYY-MM-DD' days the collector should request from IB."""
    return [p.trading_day for p in plan if p.needs_fetch]


def summarise(plan: List[DayPlan]) -> str:
    n_fetch = sum(p.action == "fetch" for p in plan)
    n_refetch = sum(p.action == "refetch" for p in plan)
    n_ok = sum(p.action == "ok" for p in plan)
    return f"{len(plan)} trading day(s): {n_ok} already stored, {n_fetch} missing, {n_refetch} to refresh"
