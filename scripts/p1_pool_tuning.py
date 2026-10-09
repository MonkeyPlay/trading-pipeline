#!/usr/bin/env python3
"""p1_pool_tuning_v1 (contracts/p1_pool_tuning.py): scores the walk-forward-tuned analogue probabilities against arms A
and B on the development sessions and writes docs/reports/p1_pool_tuning_v1.md. Reads only."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import Config
from database.connection import get_db_connection, init_database
from forecaster import p1_pool_tuning as pt

if __name__ == "__main__":
    init_database(Config.DATABASE_URL)
    conn = get_db_connection(Config.DATABASE_URL)
    try:
        text = pt.report(pt.run(conn))
    finally:
        conn.close()
    path = os.path.join(_ROOT, "docs", "reports", "p1_pool_tuning_v1.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
