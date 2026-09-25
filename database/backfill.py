# database/backfill.py
"""
Repair pass for data the store derives rather than stores: the ``session_days``
ledger, recomputed from the bars actually held. Run it if the ledger and the
bars table ever drift apart (an interrupted import, a hand-edited database); the
collector trusts the ledger to decide what to download, so it must agree with
what is really there.

    python -m database.backfill                          # default DATABASE_URL
    python -m database.backfill --db postgresql://...
"""

import argparse
import logging

from database.connection import default_dsn, get_db_connection
from database.queries import rebuild_session_days

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Rebuild the session_days ledger from stored bars")
    parser.add_argument("--db", default=default_dsn(), help="PostgreSQL connection URL")
    # Kept so existing scripts and cron lines keep working; the ledger is the only pass now.
    parser.add_argument("--ledger-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    conn = get_db_connection(args.db)
    try:
        days = rebuild_session_days(conn)
        logger.info(f"session_days: ledger rebuilt from {days} stored day(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
