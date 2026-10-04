#!/usr/bin/env python3
# database/events.py
"""
Loads the economic calendar into ``economic_events`` and
``economic_event_coverage``:

  data/economic_calendar.csv           scheduled releases as the agencies published
                                       them: FOMC decisions and minutes (fed); CPI,
                                       Employment Situation, PPI and JOLTS (bls); GDP
                                       and Personal Income and Outlays (bea); advance
                                       retail sales (census) - the 2025 shutdown
                                       rescheduling included
  data/economic_calendar_coverage.csv  per source, the days it covers
  ism_rule                             ISM Manufacturing (first business day of the
                                       month) and Services (third) at 10:00 ET, by
                                       ISM's own rule - its calendar needs a login -
                                       with exchange sessions as the business days

Within a source's coverage, a day without a row of it had none of its releases;
outside every coverage the calendar is missing, which consumers report as such,
never as "no event". BEA's past releases are at the times its release archive
shows them published (its 2025 schedule still lists the cancelled Q3 advance GDP),
later ones from its schedule; retail sales from Census's retail release schedule.
Material Nasdaq-100 earnings come from SEC EDGAR (database/earnings.py). The CSVs
are the source of truth: loading is idempotent, and a release whose date changed in
the CSV replaces its row.

    python -m database.events                 # load, then print what is covered
"""

import argparse
import csv
import os
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from features import calendar as cal  # noqa: E402

CALENDAR_CSV = os.path.join(_PROJECT_ROOT, "data", "economic_calendar.csv")
COVERAGE_CSV = os.path.join(_PROJECT_ROOT, "data", "economic_calendar_coverage.csv")
ISM_SOURCE = "ism_rule"
ISM_REPORTS = ((1, "ism-manufacturing", "ISM Manufacturing PMI", "high"),
               (3, "ism-services", "ISM Services PMI", "moderate"))


def _at(day: str, hhmm: str) -> datetime:
    """``day`` at ``hhmm`` New York time, as an aware datetime."""
    return cal.ny_instant(date.fromisoformat(day), datetime.strptime(hhmm, "%H:%M").time())


def calendar_rows(path: str = CALENDAR_CSV) -> List[Dict[str, Any]]:
    """The releases of the calendar CSV, each with its ``scheduled_at``."""
    with open(path, newline="") as f:
        return [{"source": r["source"], "event_key": r["event_key"], "scheduled_at": _at(r["date"], r["time_et"]),
                 "name": r["name"], "tier": r["tier"], "country": r.get("country") or None}
                for r in csv.DictReader(f)]


def coverage_rows(path: str = COVERAGE_CSV) -> List[Dict[str, Any]]:
    with open(path, newline="") as f:
        return [{"source": r["source"], "covered_from": date.fromisoformat(r["covered_from"]),
                 "covered_to": date.fromisoformat(r["covered_to"]), "notes": r.get("notes") or None}
                for r in csv.DictReader(f)]


def ism_rows(start: date, end: date) -> List[Dict[str, Any]]:
    """The ISM reports of every month from ``start`` to ``end``: its n-th exchange session at 10:00 ET."""
    out = []
    month = date(start.year, start.month, 1)
    while month <= end:
        nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        try:
            sessions = [s.session_date for s in cal.sessions_between(month, nxt - timedelta(days=1))]
        except cal.CalendarCoverageError:
            sessions = []
        for n, key, name, tier in ISM_REPORTS:
            if len(sessions) >= n and start <= sessions[n - 1] <= end:
                d = sessions[n - 1].isoformat()
                out.append({"source": ISM_SOURCE, "event_key": f"{key}:{month:%Y-%m}", "scheduled_at": _at(d, "10:00"),
                            "name": name, "tier": tier, "country": "US"})
        month = nxt
    return out


def load(conn, calendar: str = CALENDAR_CSV, coverage: str = COVERAGE_CSV) -> Dict[str, int]:
    """
    Loads the calendar and its coverage (and the ISM rows for the ISM coverage).
    Returns counts: 'events' inserted, 'moved' (replaced at a new time),
    'coverage' rows inserted.
    """
    cov = coverage_rows(coverage)
    rows = calendar_rows(calendar)
    for c in cov:
        if c["source"] == ISM_SOURCE:
            rows += ism_rows(c["covered_from"], c["covered_to"])
    counts = {"events": 0, "moved": 0, "coverage": 0}
    with conn:
        for r in rows:
            moved = conn.execute(
                "DELETE FROM economic_events WHERE source = %s AND event_key = %s AND scheduled_at <> %s "
                "RETURNING 1;", (r["source"], r["event_key"], r["scheduled_at"])).fetchall()
            counts["moved"] += len(moved)
            inserted = conn.execute(
                "INSERT INTO economic_events (source, event_key, scheduled_at, name, country, tier) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (source, event_key, scheduled_at) DO NOTHING "
                "RETURNING 1;",
                (r["source"], r["event_key"], r["scheduled_at"], r["name"], r["country"], r["tier"])).fetchall()
            counts["events"] += len(inserted)
        for c in cov:
            inserted = conn.execute(
                "INSERT INTO economic_event_coverage (source, covered_from, covered_to, notes) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (source, covered_from, covered_to) DO NOTHING RETURNING 1;",
                (c["source"], c["covered_from"], c["covered_to"], c["notes"])).fetchall()
            counts["coverage"] += len(inserted)
    return counts


def main(argv: Optional[List[str]] = None) -> int:
    from database.connection import get_db_connection, init_database
    parser = argparse.ArgumentParser(description="Load the economic calendar")
    parser.add_argument("--db", default=None, help="Database URL (default: DATABASE_URL)")
    parser.add_argument("--calendar", default=CALENDAR_CSV)
    parser.add_argument("--coverage", default=COVERAGE_CSV)
    args = parser.parse_args(argv)
    init_database(args.db)
    conn = get_db_connection(args.db)
    try:
        counts = load(conn, args.calendar, args.coverage)
        print(f"Loaded {counts['events']} new event(s), {counts['moved']} moved, {counts['coverage']} new coverage row(s).")
        for r in conn.execute(
                "SELECT c.source, c.covered_from, c.covered_to, count(e.*) AS events "
                "  FROM economic_event_coverage c LEFT JOIN economic_events e ON e.source = c.source "
                "   AND e.scheduled_at::date BETWEEN c.covered_from AND c.covered_to "
                " GROUP BY 1, 2, 3 ORDER BY 1, 2;").fetchall():
            print(f"  {r['source']:10} {r['covered_from']} .. {r['covered_to']}: {r['events']} event(s)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
