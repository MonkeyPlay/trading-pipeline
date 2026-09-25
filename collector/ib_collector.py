#!/usr/bin/env python3
"""
Interactive Brokers Historical Data Collector for the 'Trading Pipeline' project.

Collection is organised strictly by NY trading day. Every run:

  1. asks the database which days of the requested window are missing
     (collector/coverage.plan_trading_days -> the session_days ledger). This
     happens BEFORE any socket is opened, so if the window is already covered no
     IB connection is made at all and nothing is re-downloaded.
  2. requests one IB window per missing day, keeps only the bars belonging to
     that day, and writes the day atomically with queries.save_trading_day() —
     the day's rows are replaced as a unit and its ledger row refreshed.
  3. records a per-day entry in collection_runs, including days IB returned
     nothing for (stored as 'EMPTY' so they are not requested again).

Run from the project root:
    python -m collector.ib_collector --expiry 202609 --days 5
    python -m collector.ib_collector --expiry 202609 --start 2026-08-01 --end 2026-08-31
    python -m collector.ib_collector --expiry 202609 --days 30 --plan-only
"""

import os
import sys
import argparse
import logging
import threading
import time
from datetime import datetime, timezone, timedelta, date

# --- Make the project root importable regardless of how this script is invoked ---
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config
from database.connection import get_db_connection, init_database
from database.queries import (
    upsert_contract,
    get_contract_by_expiry,
    save_trading_day,
    create_collection_run,
    update_collection_run,
    expected_bars_for,
)
from features.session_windows import convert_utc_to_ny, classify_session_scope, get_trading_day_date
from collector.pacing import IBKRPacer, format_ibkr_datetime
from collector.coverage import plan_trading_days, days_to_fetch, summarise

# Bars whose timestamp is within this window of "now" may still be revised by IB
# or belong to a session that has not fully closed -> stored as is_completed=0.
_PARTIAL_BAR_WINDOW = timedelta(hours=2)

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
    from ibapi.common import BarData, TickerId

    _IBAPI_AVAILABLE = True
except ImportError:
    print("WARNING: 'ibapi' is not installed. Install the official Interactive Brokers API.",
          file=sys.stderr)

    class EWrapper:  # minimal stubs so the module still imports offline
        pass

    class EClient:
        def __init__(self, wrapper):
            pass

    class Contract:
        pass

    class BarData:
        pass

    TickerId = int
    _IBAPI_AVAILABLE = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("IBCollector")

INTERVAL_LABEL = "1m"
BAR_SIZE = "1 min"
PRICE_TYPE = "TRADES"
# IB informational codes that are not errors.
_BENIGN_CODES = {2104, 2106, 2158, 2107, 2100, 2119, 2110}
# Codes that indicate server-side pacing/rate-limit violations.
_RATE_LIMIT_CODES = {162, 420}


def _parse_ib_timestamp(raw) -> str:
    """
    Normalises an IB bar date into a 'YYYY-MM-DD HH:MM:SS' UTC string.

    With formatDate=2, IB returns epoch seconds (UTC). We keep string parsers as
    a fallback for gateways/config that still return formatted strings.
    """
    s = str(raw).strip()

    # 8 all-digit chars -> "YYYYMMDD" daily bar; a longer all-digit string -> epoch seconds.
    if s.isdigit() and len(s) != 8:
        dt = datetime.fromtimestamp(int(s), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    # Strip any trailing timezone token ("... UTC", "... US/Eastern").
    cleaned = s.replace("  ", " ")
    for token in (" UTC", " US/Eastern", " America/New_York"):
        if cleaned.endswith(token):
            cleaned = cleaned[: -len(token)].strip()

    parts = cleaned.split()
    if len(parts) == 1 and len(parts[0]) == 8:          # daily bar "YYYYMMDD"
        dt = datetime.strptime(parts[0], "%Y%m%d").replace(tzinfo=timezone.utc)
    elif len(parts) >= 2:                                # "YYYYMMDD HH:MM:SS"
        dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d %H:%M:%S").replace(tzinfo=timezone.utc)
    else:
        raise ValueError(f"Unrecognised IB timestamp format: {raw!r}")

    return dt.strftime("%Y-%m-%d %H:%M:%S")


class IBCollectorApp(EWrapper, EClient):
    """IB API wrapper/client: connection, contract resolution, historical data."""

    def __init__(self):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        self.next_req_id = 1
        self.connect_event = threading.Event()
        self.contract_events = {}
        self.historical_events = {}
        self.request_failed = set()

        self.resolved_contracts = {}
        self.collected_bars = {}

        self.pacer = IBKRPacer()

    def get_next_req_id(self):
        req_id = self.next_req_id
        self.next_req_id += 1
        return req_id

    # --- Connection callbacks ---
    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.next_req_id = orderId
        logger.info(f"Connected to IB. Valid initial Request ID: {orderId}")
        self.connect_event.set()

    def error(self, reqId, *args):
        """
        Flexible signature: ibapi >= 10.19 passes
        (reqId, errorTime, errorCode, errorString, advancedOrderRejectJson),
        older versions pass (reqId, errorCode, errorString, advancedOrderRejectJson).
        """
        if len(args) >= 3 and isinstance(args[0], int) and isinstance(args[1], int):
            _err_time, error_code, error_string = args[0], args[1], args[2]
        elif len(args) >= 2:
            error_code, error_string = args[0], args[1]
        else:
            error_code, error_string = -1, str(args)

        try:
            super().error(reqId, error_code, error_string)
        except TypeError:
            pass

        if error_code in _BENIGN_CODES:
            return

        if error_code in _RATE_LIMIT_CODES:
            logger.warning(f"IB pacing violation [code {error_code}]: {error_string}")
            self.pacer.handle_rate_limit_error()

        # Only treat request-scoped, non-benign messages as failures.
        if isinstance(reqId, int) and reqId > 0:
            logger.error(f"IB Error [ReqID {reqId}, Code {error_code}]: {error_string}")
            self.request_failed.add(reqId)
            if reqId in self.contract_events:
                self.contract_events[reqId].set()
            if reqId in self.historical_events:
                self.historical_events[reqId].set()
        else:
            logger.info(f"IB message [Code {error_code}]: {error_string}")

    # --- Contract resolution ---
    def contractDetails(self, reqId: int, contractDetails):
        super().contractDetails(reqId, contractDetails)
        con = contractDetails.contract
        self.resolved_contracts[reqId] = {
            "con_id": con.conId,
            "symbol": con.symbol,
            "expiry": con.lastTradeDateOrContractMonth,
            "sec_type": con.secType,
            "exchange": con.exchange,
            "currency": con.currency,
            "tick_size": contractDetails.minTick,
            "multiplier": con.multiplier,
        }
        logger.info(f"Resolved contract conId={con.conId} {con.symbol} {con.lastTradeDateOrContractMonth}")

    def contractDetailsEnd(self, reqId: int):
        super().contractDetailsEnd(reqId)
        if reqId in self.contract_events:
            self.contract_events[reqId].set()

    # --- Historical data ---
    def historicalData(self, reqId: int, bar: BarData):
        super().historicalData(reqId, bar)
        try:
            timestamp_utc = _parse_ib_timestamp(bar.date)
        except Exception as e:
            logger.error(f"Skipping bar with unparseable date {bar.date!r}: {e}")
            return

        try:
            volume = int(float(bar.volume)) if float(bar.volume) > 0 else 0
        except (TypeError, ValueError):
            volume = 0

        raw_wap = getattr(bar, "wap", None)
        try:
            wap = float(raw_wap) if raw_wap not in (None, "", -1) and float(raw_wap) > 0 else None
        except (TypeError, ValueError):
            wap = None

        raw_count = getattr(bar, "barCount", getattr(bar, "count", None))
        try:
            bar_count = int(raw_count) if raw_count not in (None, "", -1) else None
        except (TypeError, ValueError):
            bar_count = None

        ny = convert_utc_to_ny(timestamp_utc)
        self.collected_bars.setdefault(reqId, []).append({
            "timestamp_utc": timestamp_utc,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": volume,
            "wap": wap,
            "bar_count": bar_count,
            "session_scope": classify_session_scope(ny),
            "trading_day": get_trading_day_date(ny),
        })

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        super().historicalDataEnd(reqId, start, end)
        logger.info(f"Historical data complete for ReqID {reqId}: {len(self.collected_bars.get(reqId, []))} bars.")
        if reqId in self.historical_events:
            self.historical_events[reqId].set()

    # --- High-level operations ---
    def resolve_futures_contract(self, symbol, expiry, exchange="CME", currency="USD"):
        req_id = self.get_next_req_id()
        self.contract_events[req_id] = threading.Event()

        contract = Contract()
        contract.symbol = symbol
        contract.secType = "FUT"
        contract.exchange = exchange
        contract.currency = currency
        contract.lastTradeDateOrContractMonth = expiry

        logger.info(f"Resolving {symbol} {expiry} on {exchange}...")
        self.reqContractDetails(req_id, contract)

        if not self.contract_events[req_id].wait(timeout=15.0):
            logger.error(f"Timeout resolving {symbol} {expiry}")
            return None
        if req_id in self.request_failed:
            return None
        return self.resolved_contracts.get(req_id)

    def fetch_historical_bars(self, contract, end_dt, duration_str):
        req_id = self.get_next_req_id()
        self.historical_events[req_id] = threading.Event()
        self.collected_bars[req_id] = []

        ib_contract = Contract()
        ib_contract.conId = contract["con_id"]
        ib_contract.symbol = contract["symbol"]
        ib_contract.secType = contract["sec_type"]
        ib_contract.exchange = contract["exchange"]
        ib_contract.currency = contract["currency"]

        end_str = format_ibkr_datetime(end_dt)

        self.pacer.wait_if_necessary(contract["con_id"], duration_str, BAR_SIZE, end_str)
        self.reqHistoricalData(
            reqId=req_id,
            contract=ib_contract,
            endDateTime=end_str,
            durationStr=duration_str,
            barSizeSetting=BAR_SIZE,
            whatToShow=PRICE_TYPE,
            useRTH=0,               # fetch full sessions; per-bar RTH/ETH is derived from the timestamp
            formatDate=2,           # epoch seconds (UTC) — unambiguous
            keepUpToDate=False,
            chartOptions=[],
        )
        self.pacer.register_request(contract["con_id"], duration_str, BAR_SIZE, end_str)

        if not self.historical_events[req_id].wait(timeout=60.0):
            logger.error(f"Timeout waiting for historical data (ReqID {req_id})")
            return None
        if req_id in self.request_failed:
            return None
        return self.collected_bars.get(req_id, [])


def _build_day_bars(bars, day_str, now_utc):
    """
    Keeps only the bars that belong to ``day_str`` and flags the ones IB may
    still revise. A bar within ``_PARTIAL_BAR_WINDOW`` of now is stored with
    is_completed = 0, which keeps its day 'PARTIAL' so a later run finalises it.
    """
    partial_cutoff = now_utc - _PARTIAL_BAR_WINDOW
    rows = []
    for b in bars:
        if b.get("trading_day") != day_str:
            continue
        try:
            bar_dt = datetime.strptime(b["timestamp_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            is_completed = 0 if bar_dt >= partial_cutoff else 1
        except ValueError:
            is_completed = 1
        rows.append({**b, "source": "IBKR", "is_completed": is_completed})
    return rows


def _log_plan(plan):
    logger.info("Coverage: " + summarise(plan))
    for p in plan:
        level = logger.info if p.needs_fetch else logger.debug
        level(f"  {p.trading_day}: {p.action} ({p.reason})")


def _resolve_window(days_to_download, start=None, end=None):
    """The [start, end] calendar window to consider, as dates."""
    today = datetime.now(timezone.utc).date()
    end_day = date.fromisoformat(end) if end else today
    start_day = date.fromisoformat(start) if start else end_day - timedelta(days=days_to_download)
    if start_day > end_day:
        raise ValueError(f"start ({start_day}) is after end ({end_day}).")
    return start_day, end_day


def _fetch_and_store_day(app, conn, contract_info, day_str, now_utc):
    """
    Downloads one trading day and stores it atomically. Returns the number of
    bars written, or None if the request failed.
    """
    con_id = contract_info["con_id"]
    day = date.fromisoformat(day_str)

    # A 48h window ending the morning after the session fully contains the NY
    # trading day including its prior-evening Globex open. Bars outside the day
    # are discarded; the overlap only exists to guarantee full coverage.
    chunk_end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1, hours=6)
    chunk_end = min(chunk_end, now_utc)   # IB rejects an endDateTime in the future

    run_id = create_collection_run(
        conn, contract_id=con_id, trading_day=day_str, interval=INTERVAL_LABEL,
        requested_start_utc=(chunk_end - timedelta(days=2)).isoformat(),
        requested_end_utc=chunk_end.isoformat(),
    )

    try:
        bars = app.fetch_historical_bars(contract_info, chunk_end, "2 D")
        if bars is None:
            update_collection_run(conn, run_id, "FAILED", errors="Download timeout or IB error.")
            logger.warning(f"{day_str}: request failed; the day is left untouched.")
            return None

        day_bars = _build_day_bars(bars, day_str, now_utc)
        summary = save_trading_day(
            conn, contract_id=con_id, trading_day=day_str, bars=day_bars,
            interval=INTERVAL_LABEL, price_type=PRICE_TYPE, source="IBKR",
            expected_bar_count=expected_bars_for(INTERVAL_LABEL),
        )
        update_collection_run(
            conn, run_id, "COMPLETED",
            missing_intervals=None if day_bars else ["IB returned no bars for this trading day"],
            bars_written=summary["bar_count"],
        )
        return summary["bar_count"]
    except Exception as e:
        logger.exception(f"Day {day_str} failed.")
        update_collection_run(conn, run_id, "FAILED", errors=str(e))
        return None


def run_collection_workflow(dsn, host, port, client_id, symbol, expiry, days_to_download,
                            gap_fill=True, start=None, end=None, plan_only=False):
    init_database(dsn)
    conn = get_db_connection(dsn)

    app = None
    try:
        start_day, end_day = _resolve_window(days_to_download, start, end)
        logger.info(f"Window: {start_day} -> {end_day} ({symbol} {expiry}, {INTERVAL_LABEL}).")

        # --- Step 1: ask the database what is missing, before touching IB. ---
        # The contract may already be known from an earlier run, in which case the
        # whole plan can be built offline and a fully-covered window costs no
        # connection at all.
        contract_row = get_contract_by_expiry(conn, symbol, expiry)
        targets = None
        if contract_row is not None:
            plan = plan_trading_days(
                conn, contract_row["contract_id"], start_day, end_day,
                interval=INTERVAL_LABEL, price_type=PRICE_TYPE, force=not gap_fill,
            )
            _log_plan(plan)
            targets = days_to_fetch(plan)
            if not targets:
                logger.info("Every trading day in the window is already stored — nothing to download.")
                return
        else:
            logger.info(f"{symbol} {expiry} is not in the database yet; it must be resolved via IB first.")

        if plan_only:
            logger.info("--plan-only: stopping before connecting to IB.")
            return

        if not _IBAPI_AVAILABLE:
            logger.error("ibapi is not installed; cannot run a live collection.")
            sys.exit(1)

        # --- Step 2: connect and resolve the contract. ---
        app = IBCollectorApp()
        logger.info(f"Connecting to IB at {host}:{port} (clientId={client_id})...")
        app.connect(host, port, client_id)
        threading.Thread(target=app.run, name="IBAPI_Thread", daemon=True).start()

        if not app.connect_event.wait(timeout=10.0):
            logger.error("Could not connect to IB Gateway/TWS. Is the API socket enabled?")
            sys.exit(1)

        contract_info = app.resolve_futures_contract(symbol, expiry)
        if not contract_info:
            logger.error(f"Failed to resolve {symbol} {expiry}.")
            return

        con_id = contract_info["con_id"]
        upsert_contract(
            conn, contract_id=con_id, symbol=contract_info["symbol"],
            expiry=contract_info["expiry"], exchange=contract_info["exchange"],
            currency=contract_info["currency"], tick_size=contract_info["tick_size"],
            multiplier=contract_info["multiplier"], sec_type=contract_info["sec_type"],
        )

        if targets is None:
            # First run for this contract: now that con_id is known, plan the days.
            plan = plan_trading_days(
                conn, con_id, start_day, end_day,
                interval=INTERVAL_LABEL, price_type=PRICE_TYPE, force=not gap_fill,
            )
            _log_plan(plan)
            targets = days_to_fetch(plan)
            if not targets:
                logger.info("Every trading day in the window is already stored — nothing to download.")
                return

        # --- Step 3: one IB request per missing day, one atomic write per day. ---
        logger.info(f"Fetching {len(targets)} trading day(s), one at a time.")
        now_utc = datetime.now(timezone.utc)
        total, stored_days, failed_days = 0, 0, []

        for day_str in targets:
            written = _fetch_and_store_day(app, conn, contract_info, day_str, now_utc)
            if written is None:
                failed_days.append(day_str)
            else:
                total += written
                stored_days += 1
            time.sleep(1.0)

        logger.info(f"Collection complete: {stored_days}/{len(targets)} day(s) stored, {total} bar(s) written.")
        if failed_days:
            logger.warning(f"{len(failed_days)} day(s) failed and will be retried next run: {failed_days}")
    finally:
        if app is not None:
            logger.info("Disconnecting from IB...")
            app.disconnect()
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trading Pipeline IBKR Historical Data Collector")
    parser.add_argument("--db", type=str, default=Config.DATABASE_URL, help="PostgreSQL connection URL")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="IP address of IB Gateway or TWS")
    parser.add_argument("--port", type=int, default=4002, help="API port (4002 Gateway paper, 4001 Gateway live, 7497 TWS paper)")
    parser.add_argument("--client-id", type=int, default=1, help="Client ID for the socket API connection")
    parser.add_argument("--symbol", type=str, default="NQ", help="Futures symbol (e.g., NQ)")
    parser.add_argument("--expiry", type=str, help="Contract expiry month YYYYMM (e.g., 202609)")
    parser.add_argument("--days", type=int, default=20, help="How many calendar days back to consider")
    parser.add_argument("--start", type=str, help="First calendar day of the window (YYYY-MM-DD); overrides --days")
    parser.add_argument("--end", type=str, help="Last calendar day of the window (YYYY-MM-DD); defaults to today")
    parser.add_argument("--full", action="store_true",
                        help="Re-download every trading day in the window (default: only missing/stale days)")
    parser.add_argument("--plan-only", action="store_true",
                        help="Report which days would be downloaded, without connecting to IB")
    parser.add_argument("--init-only", action="store_true", help="Only initialise the database and exit")

    args = parser.parse_args()

    if args.init_only:
        init_database(args.db)
        print("Database initialized successfully. Exiting.")
        sys.exit(0)

    if not args.expiry:
        parser.error("--expiry is required unless --init-only is specified.")

    run_collection_workflow(
        dsn=args.db, host=args.host, port=args.port, client_id=args.client_id,
        symbol=args.symbol, expiry=args.expiry, days_to_download=args.days,
        gap_fill=not args.full, start=args.start, end=args.end, plan_only=args.plan_only,
    )
