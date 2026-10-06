#!/usr/bin/env python3
"""
Where a bar's delay comes from (docs/fan_experiment.md, the forward record): asks IB for live
market data on the instruments the frozen fan model reads and reports the market data type
IB actually serves (1 live, 2 frozen, 3 delayed, 4 delayed-frozen - IB's tickMarketDataType),
then requests the last 30 minutes of 1-minute bars and measures how old the newest one is at
IB itself - the provider's delay, apart from the pipeline's own collection and scheduling.

    python scripts/ib_feed_check.py                 # NQ and VXN on their stored active contracts
    python scripts/ib_feed_check.py --json logs/ib_feed_check.json

Read-only: it subscribes for a few seconds and cancels; it writes nothing to the store. Its
own client id is IB_CLIENT_ID + 3, so it never meets the collectors.
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from ibapi.client import EClient            # noqa: E402
from ibapi.contract import Contract         # noqa: E402
from ibapi.wrapper import EWrapper          # noqa: E402

from config import Config                   # noqa: E402
from database.connection import get_db_connection  # noqa: E402

TYPES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}


class Check(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.types, self.bars, self.done, self.errors = {}, {}, {}, []

    def nextValidId(self, orderId):
        self.ready.set()

    def marketDataType(self, reqId, marketDataType):
        self.types[reqId] = marketDataType

    def historicalData(self, reqId, bar):
        self.bars.setdefault(reqId, []).append(int(bar.date))

    def historicalDataEnd(self, reqId, start, end):
        self.done.setdefault(reqId, threading.Event()).set()

    def error(self, reqId, *args):
        code, msg = (args[1], args[2]) if len(args) >= 3 and isinstance(args[0], int) and isinstance(args[1], int) \
            else (args[0], args[1] if len(args) > 1 else "")
        if code not in (2104, 2106, 2107, 2108, 2158, 2119):        # farm-status notices
            self.errors.append({"req": reqId, "code": code, "message": str(msg)})


def _contract(row):
    cid, symbol, sec_type, exchange = row
    c = Contract()
    c.conId, c.symbol, c.secType, c.exchange = int(cid), symbol, sec_type, exchange
    return c


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--symbol", action="append", help="Repeat; NQ and VXN by default")
    p.add_argument("--json", help="Also write the result here")
    args = p.parse_args(argv)
    symbols = args.symbol or ["NQ", "VXN"]
    conn = get_db_connection(Config.DATABASE_URL)
    rows = {}
    for s in symbols:
        r = conn.execute("SELECT c.contract_id, c.symbol, c.sec_type, c.exchange FROM contracts c LEFT JOIN "
                         "active_contracts a ON a.contract_id = c.contract_id WHERE c.symbol = %s "
                         "ORDER BY a.trading_day DESC NULLS LAST LIMIT 1;", (s,)).fetchone()
        rows[s] = r
    conn.close()
    app = Check()
    app.connect(Config.IB_HOST, Config.IB_PORT, Config.IB_CLIENT_ID + 3)
    threading.Thread(target=app.run, daemon=True).start()
    if not app.ready.wait(10):
        print("Could not connect to IB Gateway/TWS")
        return 1
    app.reqMarketDataType(1)                                        # ask for live: IB answers with what it serves
    out = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "requested_type": "live", "instruments": {}}
    for k, (s, row) in enumerate(rows.items(), start=1):
        app.reqMktData(k, _contract(row), "", False, False, [])
    time.sleep(6)
    for k in range(1, len(rows) + 1):
        app.cancelMktData(k)
    for k, (s, row) in enumerate(rows.items(), start=100):
        app.done[k] = threading.Event()
        app.reqHistoricalData(k, _contract(row), "", "1800 S", "1 min", "TRADES", 0, 2, False, [])
        app.done[k].wait(30)
    now = time.time()
    for (k, (s, row)), h in zip(enumerate(rows.items(), start=1), range(100, 100 + len(rows))):
        bars = app.bars.get(h, [])
        newest = max(bars) if bars else None
        out["instruments"][s] = {
            "contract_id": int(row[0]), "market_data_type": TYPES.get(app.types.get(k), app.types.get(k)),
            "newest_bar_start_utc": datetime.fromtimestamp(newest, timezone.utc).isoformat() if newest else None,
            "newest_bar_closed_minutes_ago": round((now - newest - 60) / 60, 1) if newest else None}
    out["errors"] = app.errors
    app.disconnect()
    for s, v in out["instruments"].items():
        print(f"{s:4} market data: {v['market_data_type']}; newest 1-minute bar at IB closed "
              f"{v['newest_bar_closed_minutes_ago']} min ago ({v['newest_bar_start_utc']})")
    for e in out["errors"]:
        print(f"  IB {e['code']}: {e['message'][:140]}")
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
