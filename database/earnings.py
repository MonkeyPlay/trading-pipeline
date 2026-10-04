#!/usr/bin/env python3
# database/earnings.py
"""
Material Nasdaq-100 earnings releases for P1's Event Risk (contracts/nq_preopen.py,
EV-v1), from SEC EDGAR: every 8-K with Item 2.02 (results of operations) of the
companies in MATERIAL_EARNINGS, stored in ``economic_events`` as source
``sec_earnings`` at its EDGAR acceptance time - when the release became public.

Coverage: a session date counts as covered when the fetch ran after its cutoff
(09:29 ET), so every release published by that cutoff is in the table. A fetch
that misses any company records no coverage, so a partial fetch can never read
as "no earnings". Loading is idempotent.

    python -m database.earnings            # fetch and load, then print the coverage

SEC asks automated clients to identify themselves: set SEC_USER_AGENT to
"<name> <contact e-mail>" (https://www.sec.gov/os/accessing-edgar-data).
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from contracts.nq_preopen import MATERIAL_EARNINGS  # noqa: E402
from features import calendar as cal  # noqa: E402
from features.nq_evidence import EARNINGS_SOURCE  # noqa: E402

logger = logging.getLogger(__name__)

COVERED_FROM = date(2025, 6, 1)           # the economic calendar's own coverage start
SUBMISSIONS = "https://data.sec.gov/submissions/{name}"
DEFAULT_USER_AGENT = "trading-pipeline research (set SEC_USER_AGENT to '<name> <e-mail>')"
REQUEST_GAP = 0.2                         # seconds between requests (SEC allows 10 per second)


def _get(name: str) -> Dict[str, Any]:
    req = urllib.request.Request(SUBMISSIONS.format(name=name),
                                 headers={"User-Agent": os.getenv("SEC_USER_AGENT") or DEFAULT_USER_AGENT,
                                          "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def earnings_rows(ticker: str, filings: Dict[str, List[Any]], since: date) -> List[Dict[str, Any]]:
    """The 8-K Item 2.02 filings in one EDGAR ``filings`` block (column arrays), accepted on or after ``since``."""
    out = []
    for i, form in enumerate(filings["form"]):
        if form != "8-K" or "2.02" not in filings["items"][i].split(","):
            continue
        accepted = datetime.strptime(filings["acceptanceDateTime"][i][:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
        if accepted.astimezone(cal.NY_TZ).date() < since:
            continue
        out.append({"source": EARNINGS_SOURCE, "event_key": f"{ticker}:{filings['accessionNumber'][i]}",
                    "scheduled_at": accepted, "name": f"{ticker} earnings release (8-K 2.02)",
                    "tier": "moderate", "country": "US"})
    return out


def fetch(since: date = COVERED_FROM) -> List[Dict[str, Any]]:
    """Every material company's earnings releases since ``since``; raises if any company cannot be read."""
    rows = []
    for ticker, cik in sorted(MATERIAL_EARNINGS.items()):
        doc = _get(f"CIK{cik:010d}.json")
        if ticker not in doc.get("tickers", []):
            raise ValueError(f"EDGAR CIK {cik} lists {doc.get('tickers')}, not {ticker}")
        blocks = [doc["filings"]["recent"]]
        # Older filings live in extra files; read them only while the recent block does not reach back far enough.
        for extra in doc["filings"].get("files", []):
            if min(blocks[-1]["filingDate"] or ["9999"]) < since.isoformat():
                break
            time.sleep(REQUEST_GAP)
            blocks.append(_get(extra["name"]))
        for block in blocks:
            rows += earnings_rows(ticker, block, since)
        time.sleep(REQUEST_GAP)
    return rows


def covered_to(fetched_at: datetime) -> date:
    """The last date whose 09:29 ET cutoff the fetch ran after."""
    ny = fetched_at.astimezone(cal.NY_TZ)
    return ny.date() if ny.time() >= cal.CUTOFF else ny.date() - timedelta(days=1)


def load(conn, rows: List[Dict[str, Any]], fetched_at: datetime, since: date = COVERED_FROM) -> Dict[str, int]:
    """Stores ``rows`` (a complete fetch) and the coverage it vouches for. Returns counts."""
    counts = {"events": 0, "coverage": 0}
    with conn:
        for r in rows:
            counts["events"] += len(conn.execute(
                "INSERT INTO economic_events (source, event_key, scheduled_at, name, country, tier) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (source, event_key, scheduled_at) DO NOTHING "
                "RETURNING 1;",
                (r["source"], r["event_key"], r["scheduled_at"], r["name"], r["country"], r["tier"])).fetchall())
        until = covered_to(fetched_at)
        if until >= since:
            counts["coverage"] = len(conn.execute(
                "INSERT INTO economic_event_coverage (source, covered_from, covered_to, notes) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (source, covered_from, covered_to) DO NOTHING RETURNING 1;",
                (EARNINGS_SOURCE, since, until,
                 f"SEC EDGAR 8-K Item 2.02 of {', '.join(sorted(MATERIAL_EARNINGS))} at acceptance time; "
                 f"fetched {fetched_at.isoformat(timespec='seconds')}")).fetchall())
    return counts


def refresh(conn) -> Dict[str, int]:
    """Fetches and loads every material company's releases; raises (storing nothing) if a company fails."""
    fetched_at = datetime.now(timezone.utc)
    rows = fetch()
    counts = load(conn, rows, fetched_at)
    logger.info(f"Earnings: {len(rows)} release(s) read from EDGAR, {counts['events']} new; covered through "
                f"{covered_to(fetched_at)}.")
    return counts


def main(argv: Optional[List[str]] = None) -> int:
    from database.connection import get_db_connection, init_database
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Load material Nasdaq-100 earnings releases from SEC EDGAR")
    parser.add_argument("--db", default=None, help="Database URL (default: DATABASE_URL)")
    args = parser.parse_args(argv)
    init_database(args.db)
    conn = get_db_connection(args.db)
    try:
        refresh(conn)
        for r in conn.execute("SELECT max(covered_to) AS until, count(e.*) AS events FROM economic_event_coverage c "
                              "LEFT JOIN economic_events e ON e.source = c.source WHERE c.source = %s;",
                              (EARNINGS_SOURCE,)).fetchall():
            print(f"  {EARNINGS_SOURCE}: {COVERED_FROM} .. {r['until']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
