# database/backfill.py
"""
Repair passes for data a migration derives rather than stores.

  * ``bars.trading_day`` (NY session date) for rows written before migration
    0002. Idempotent — only touches rows where it is still NULL. Migration 0003
    runs this automatically, since it makes the column NOT NULL.
  * the ``session_days`` ledger, recomputed from the bars actually stored. Run it
    if the ledger and the bars table ever drift apart (an interrupted migration,
    a hand-edited database); the collector trusts the ledger to decide what to
    download, so it must agree with what is really there.

    python -m database.backfill                    # default DB, both passes
    python -m database.backfill --db path.db
    python -m database.backfill --ledger-only
"""

import argparse
import logging

from database.connection import get_db_connection, _DEFAULT_DB_PATH
from database.queries import rebuild_session_days
from features.session_windows import convert_utc_to_ny, get_trading_day_date

logger = logging.getLogger(__name__)


def backfill_trading_day(conn, batch_size: int = 5000) -> int:
    total = 0
    while True:
        rows = conn.execute(
            "SELECT rowid, timestamp_utc FROM bars WHERE trading_day IS NULL LIMIT ?;",
            (batch_size,),
        ).fetchall()
        if not rows:
            break
        updates = [
            (get_trading_day_date(convert_utc_to_ny(r["timestamp_utc"])), r["rowid"])
            for r in rows
        ]
        with conn:
            conn.executemany("UPDATE bars SET trading_day = ? WHERE rowid = ?;", updates)
        total += len(updates)
        logger.info(f"Backfilled trading_day for {total} bar(s)...")
    return total


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Backfill derived bar columns and the day ledger")
    parser.add_argument("--db", default=_DEFAULT_DB_PATH)
    parser.add_argument("--ledger-only", action="store_true",
                        help="Only recompute session_days from the stored bars")
    args = parser.parse_args()

    conn = get_db_connection(args.db)
    try:
        if not args.ledger_only:
            n = backfill_trading_day(conn)
            logger.info(f"trading_day: updated {n} row(s).")
        days = rebuild_session_days(conn)
        logger.info(f"session_days: ledger rebuilt from {days} stored day(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
