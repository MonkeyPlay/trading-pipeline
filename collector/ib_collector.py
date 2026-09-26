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

A future with a RollRule (config.INSTRUMENTS) is collected as a chain: each
trading day is fetched from the contract that is front on that day, plus the
ROLL_WARMUP_SESSIONS (7) trading days before each contract becomes active, so its
first day has a same-contract reference close and enough same-contract history
for intraday indicators (collector/rolls.py). The next contract's warm-up days
are collected as they happen, ahead of the roll. The day -> contract choice is recorded in
active_contracts. Indices and stocks have a single contract. The logical-asset
source map (config.ASSET_SOURCES) is registered in asset_sources on every run.

Run from the project root:
    python -m collector.ib_collector --days 5
    python -m collector.ib_collector --start 2026-06-01 --end 2026-09-25
    python -m collector.ib_collector --days 30 --plan-only
    python -m collector.ib_collector --expiry 202609 --days 5 --symbol NQ   # pin one contract

Real-time bars (with a receive time per bar) come from collector/live_stream.py.
"""

import os
import sys
import argparse
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date
from statistics import median
from typing import Dict, List, Optional

# --- Make the project root importable regardless of how this script is invoked ---
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config, Instrument
from database.connection import get_db_connection, init_database
from database.queries import (
    upsert_contract,
    get_contract_by_expiry,
    list_future_chain,
    save_trading_day,
    create_collection_run,
    update_collection_run,
    set_active_contracts,
    clear_active_contracts,
    register_asset_sources,
)
from features.session_windows import convert_utc_to_ny, classify_session_scope, get_trading_day_date
from collector.pacing import IBKRPacer, format_ibkr_datetime
from collector.coverage import (
    plan_trading_days, days_to_fetch, summarise, expected_trading_days, previous_trading_day,
)
from collector.rolls import front_contracts, segments, upcoming_roll

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
PRICE_TYPE = "TRADES"   # default whatToShow; each Instrument names its own
# IB informational codes that are not errors.
_BENIGN_CODES = {2104, 2106, 2158, 2107, 2100, 2119, 2110}
# Codes that indicate server-side pacing/rate-limit violations.
_RATE_LIMIT_CODES = {162, 420}


def is_pacing_violation(error_code: int, error_string: str) -> bool:
    """
    Whether an IB error means "slow down". Code 162 is the historical-data
    service's catch-all: besides pacing violations it also reports "HMDS query
    returned no data", cancelled queries and missing permissions, none of which
    a back-off can help - those just fail their request.
    """
    if error_code == 162:
        return "pacing violation" in (error_string or "").lower()
    return error_code in _RATE_LIMIT_CODES


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


def bar_to_dict(bar) -> Optional[dict]:
    """
    An IB BarData as a bar dict (UTC start timestamp, OHLCV, session scope and
    NY trading day), or None if its date cannot be parsed. Shared by the
    historical download and the real-time stream.
    """
    try:
        timestamp_utc = _parse_ib_timestamp(bar.date)
    except Exception as e:
        logger.error(f"Skipping bar with unparseable date {bar.date!r}: {e}")
        return None

    try:
        volume = int(float(bar.volume)) if float(bar.volume) > 0 else 0
    except (TypeError, ValueError):
        volume = 0

    raw_wap = getattr(bar, "wap", getattr(bar, "average", None))
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
    return {
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
    }


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

        if is_pacing_violation(error_code, error_string):
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
        # A request without an expiry returns the whole chain, one callback per
        # contract, so details accumulate per request.
        self.resolved_contracts.setdefault(reqId, []).append({
            "con_id": con.conId,
            "symbol": con.symbol,
            # IB returns "" for an instrument with no expiry (a cash index); store
            # NULL so contract lookups can distinguish "no expiry" from a real one.
            "expiry": con.lastTradeDateOrContractMonth or None,
            "sec_type": con.secType,
            "exchange": con.exchange,
            "currency": con.currency,
            "tick_size": contractDetails.minTick,
            "multiplier": con.multiplier or None,
            "contract_month": getattr(contractDetails, "contractMonth", "") or None,
            "local_symbol": con.localSymbol or None,
            "trading_class": con.tradingClass or None,
            "primary_exchange": getattr(con, "primaryExchange", "") or None,
            "time_zone_id": getattr(contractDetails, "timeZoneId", "") or None,
            "trading_hours": getattr(contractDetails, "tradingHours", "") or None,
            "liquid_hours": getattr(contractDetails, "liquidHours", "") or None,
        })
        logger.debug(f"Contract details conId={con.conId} {con.symbol} {con.lastTradeDateOrContractMonth}")

    def contractDetailsEnd(self, reqId: int):
        super().contractDetailsEnd(reqId)
        if reqId in self.contract_events:
            self.contract_events[reqId].set()

    # --- Historical data ---
    def historicalData(self, reqId: int, bar: BarData):
        super().historicalData(reqId, bar)
        parsed = bar_to_dict(bar)
        if parsed is not None:
            self.collected_bars.setdefault(reqId, []).append(parsed)

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        super().historicalDataEnd(reqId, start, end)
        logger.info(f"Historical data complete for ReqID {reqId}: {len(self.collected_bars.get(reqId, []))} bars.")
        if reqId in self.historical_events:
            self.historical_events[reqId].set()

    # --- High-level operations ---
    def _request_contract_details(self, contract, label, timeout=30.0):
        req_id = self.get_next_req_id()
        self.contract_events[req_id] = threading.Event()
        self.reqContractDetails(req_id, contract)
        if not self.contract_events[req_id].wait(timeout=timeout):
            logger.error(f"Timeout resolving {label}")
            return None
        if req_id in self.request_failed:
            return None
        return self.resolved_contracts.get(req_id, [])

    @staticmethod
    def _ib_contract(instrument: Instrument, expiry=None):
        contract = Contract()
        contract.symbol = instrument.symbol
        contract.secType = instrument.sec_type
        contract.exchange = instrument.exchange
        contract.currency = instrument.currency
        if instrument.primary_exchange:
            contract.primaryExchange = instrument.primary_exchange
        if expiry:
            contract.lastTradeDateOrContractMonth = expiry
        if instrument.is_future:
            # Backfilling the previous quarter means asking for contracts that
            # have already expired; IB serves those only when asked explicitly.
            contract.includeExpired = True
        return contract

    @staticmethod
    def _prefer_own_class(details, symbol):
        """Some symbols list several trading classes; keep the one named after it."""
        own = [d for d in details if d.get("trading_class") in (None, symbol)]
        return own or details

    def resolve_contract(self, instrument: Instrument, expiry=None):
        """
        Resolves one contract via IB. ``expiry`` is the contract month for a
        future and None for a cash index or a stock, which have none.
        """
        label = _label(instrument.symbol, expiry)
        logger.info(f"Resolving {label} ({instrument.sec_type}) on {instrument.exchange}...")
        details = self._request_contract_details(self._ib_contract(instrument, expiry), label)
        if not details:
            return None
        details = self._prefer_own_class(details, instrument.symbol)
        if len(details) > 1:
            logger.warning(f"{label}: IB matched {len(details)} contracts; using conId "
                           f"{details[0]['con_id']}. Pin a full expiry to choose another.")
        chosen = details[0]
        logger.info(f"Resolved {label}: conId={chosen['con_id']} expiry={chosen['expiry']}")
        return chosen

    def discover_chain(self, instrument: Instrument):
        """Every listed and recently expired contract of a future, as detail dicts."""
        label = f"{instrument.symbol} chain"
        logger.info(f"Discovering the {instrument.symbol} contract chain on {instrument.exchange}...")
        details = self._request_contract_details(self._ib_contract(instrument), label, timeout=60.0)
        if not details:
            return []
        details = self._prefer_own_class(details, instrument.symbol)
        logger.info(f"{label}: {len(details)} contract(s).")
        return details

    @staticmethod
    def ib_contract_from_info(contract):
        """An IB Contract for a resolved contract dict (conId plus routing fields)."""
        ib_contract = Contract()
        ib_contract.conId = contract["con_id"]
        ib_contract.symbol = contract["symbol"]
        ib_contract.secType = contract["sec_type"]
        ib_contract.exchange = contract["exchange"]
        ib_contract.currency = contract["currency"]
        if contract.get("primary_exchange"):
            ib_contract.primaryExchange = contract["primary_exchange"]
        if contract["sec_type"] == "FUT":
            ib_contract.includeExpired = True
        return ib_contract

    def fetch_historical_bars(self, contract, end_dt, duration_str, what_to_show=PRICE_TYPE,
                              min_spacing=None, timeout=60.0):
        req_id = self.get_next_req_id()
        self.historical_events[req_id] = threading.Event()
        self.collected_bars[req_id] = []

        ib_contract = self.ib_contract_from_info(contract)
        end_str = format_ibkr_datetime(end_dt)

        self.pacer.wait_if_necessary(contract["con_id"], duration_str, BAR_SIZE, end_str, min_spacing)
        self.reqHistoricalData(
            reqId=req_id,
            contract=ib_contract,
            endDateTime=end_str,
            durationStr=duration_str,
            barSizeSetting=BAR_SIZE,
            whatToShow=what_to_show,
            useRTH=0,               # fetch full sessions; per-bar RTH/ETH is derived from the timestamp
            formatDate=2,           # epoch seconds (UTC) — unambiguous
            keepUpToDate=False,
            chartOptions=[],
        )
        self.pacer.register_request(contract["con_id"], duration_str, BAR_SIZE, end_str)

        if not self.historical_events[req_id].wait(timeout=timeout):
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


def _implausible(instrument: Instrument, day_bars):
    """
    An error message if the day's median close falls outside the instrument's
    plausible range, else None. That is a unit problem (e.g. TNX sent as plain
    percent while configured as percent x10), not market noise, so the day is
    refused rather than stored under the wrong unit.
    """
    if not instrument.plausible_range or not day_bars:
        return None
    lo, hi = instrument.plausible_range
    mid = median(float(b["close"]) for b in day_bars)
    if lo <= mid <= hi:
        return None
    return (f"{instrument.symbol}: median close {mid:g} is outside the plausible range "
            f"[{lo:g}, {hi:g}] for value_unit '{instrument.value_unit}'. Check the unit in "
            f"config.INSTRUMENTS; the day was not stored.")


def _fetch_and_store_day(app, conn, instrument, contract_info, day_str, now_utc):
    """
    Downloads one trading day and stores it atomically. Returns the number of
    bars written, or None if the request failed.
    """
    con_id = contract_info["con_id"]
    day = date.fromisoformat(day_str)
    price_type = instrument.what_to_show

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
        bars = app.fetch_historical_bars(contract_info, chunk_end, "2 D", what_to_show=price_type)
        if bars is None:
            update_collection_run(conn, run_id, "FAILED", errors="Download timeout or IB error.")
            logger.warning(f"{day_str}: request failed; the day is left untouched.")
            return None

        day_bars = _build_day_bars(bars, day_str, now_utc)
        problem = _implausible(instrument, day_bars)
        if problem:
            update_collection_run(conn, run_id, "FAILED", errors=problem)
            logger.error(f"{day_str}: {problem}")
            return None

        summary = save_trading_day(
            conn, contract_id=con_id, trading_day=day_str, bars=day_bars,
            interval=INTERVAL_LABEL, price_type=price_type, source="IBKR",
            expected_bar_count=instrument.expected_bars,
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


def _label(symbol, expiry):
    """'NQ 202612' for a future, plain 'VIX' for an index with no expiry."""
    return f"{symbol} {expiry}" if expiry else symbol


def _instrument_for(symbol):
    """The configured instrument, or a CME future for a symbol config does not know."""
    instrument = Config.instrument(symbol)
    if instrument is None:
        logger.warning(f"{symbol} is not in config.INSTRUMENTS; treating it as a CME future.")
        instrument = Instrument(symbol, symbol, "CME", 0.25, None)
    return instrument


def _contract_info(row):
    """A contracts row in the shape IB resolution returns."""
    return {
        "con_id": row["contract_id"], "symbol": row["symbol"], "expiry": row["expiry"],
        "sec_type": row["sec_type"], "exchange": row["exchange"], "currency": row["currency"],
        "primary_exchange": row["primary_exchange"] if "primary_exchange" in row.keys() else None,
    }


def _store_contract(conn, info):
    upsert_contract(
        conn, contract_id=info["con_id"], symbol=info["symbol"], expiry=info["expiry"],
        exchange=info["exchange"], currency=info["currency"], tick_size=info["tick_size"],
        multiplier=info["multiplier"], sec_type=info["sec_type"],
        contract_month=info.get("contract_month"), local_symbol=info.get("local_symbol"),
        trading_class=info.get("trading_class"), primary_exchange=info.get("primary_exchange"),
        time_zone_id=info.get("time_zone_id"), trading_hours=info.get("trading_hours"),
        liquid_hours=info.get("liquid_hours"),
    )


@dataclass
class _Work:
    """
    What one symbol needs. A single-contract symbol (index, stock, pinned
    future) has one entry in ``jobs``; a rolling future has one per contract.
    ``jobs`` is None until the contract(s) are known, which may need IB.
    """
    symbol: str
    instrument: Instrument
    expiry: Optional[str]                          # pinned contract month; None otherwise
    rolling: bool
    jobs: Optional[List[tuple]] = None             # [(contract_info, [day, ...])]
    assignment: Dict[str, int] = field(default_factory=dict)   # day -> contract_id
    uncovered: List[str] = field(default_factory=list)         # days no known contract covers
    rule: str = ""

    @property
    def label(self):
        return _label(self.symbol, self.expiry)

    @property
    def pending_days(self):
        return sum(len(days) for _, days in self.jobs) if self.jobs is not None else None


def _plan(conn, contract_id, instrument, start, end, gap_fill, extra_days=()):
    plan = plan_trading_days(
        conn, contract_id, start, end, interval=INTERVAL_LABEL,
        price_type=instrument.what_to_show, force=not gap_fill,
        expected=instrument.expected_bars, extra_days=extra_days,
    )
    _log_plan(plan)
    return days_to_fetch(plan)


def _plan_single(conn, work, row, start_day, end_day, gap_fill):
    """Plans a single-contract symbol whose contract row is known."""
    # The day before the window holds its first day's reference close.
    ref = previous_trading_day(start_day)
    targets = _plan(conn, row["contract_id"], work.instrument, start_day, end_day, gap_fill, [ref])
    work.jobs = [(_contract_info(row), targets)]
    if not work.instrument.is_future:
        work.rule = "single contract"
        work.assignment = {d.isoformat(): row["contract_id"]
                           for d in expected_trading_days(start_day, end_day)}


def _plan_rolling(conn, work, start_day, end_day, gap_fill, allow_gaps=False):
    """
    Plans a rolling future from the chain already stored. Returns False when the
    stored chain cannot cover the window, which means it must be discovered via
    IB; with ``allow_gaps`` it plans the days it can cover and names the rest.
    """
    rule = work.instrument.roll
    days = expected_trading_days(start_day, end_day)
    chain = list_future_chain(conn, work.symbol)
    assignment = front_contracts(chain, days, rule)
    missing = [d.isoformat() for d in days if d not in assignment]
    if missing and not allow_gaps:
        return False
    if missing:
        # Typically days older than the expired contracts IB still serves.
        logger.warning(f"{work.symbol}: no contract in the chain covers {len(missing)} day(s) "
                       f"({missing[0]} .. {missing[-1]}); they are skipped.")

    warmup = Config.ROLL_WARMUP_SESSIONS
    work.rule = rule.describe()
    work.assignment = {d.isoformat(): row["contract_id"] for d, row in assignment.items()}
    work.uncovered = missing
    work.jobs = []
    for seg in segments(assignment, warmup):
        row = seg.contract
        logger.info(f"{work.symbol}: {row['expiry']} active {seg.first_day} -> {seg.last_day} "
                    f"(+{len(seg.warmup_days)} warm-up day(s) from {seg.warmup_days[0]}).")
        targets = _plan(conn, row["contract_id"], work.instrument, seg.first_day, seg.last_day,
                        gap_fill, seg.warmup_days)
        work.jobs.append((_contract_info(row), targets))

    # The next contract's warm-up days that have already happened are collected
    # now, as they occur, rather than all on the roll morning.
    if assignment:
        nxt = upcoming_roll(chain, max(assignment), rule, warmup)
        if nxt is not None:
            row = nxt.contract
            logger.info(f"{work.symbol}: {row['expiry']} becomes front on {nxt.first_day}; "
                        f"collecting {len(nxt.warmup_days)} warm-up day(s) ahead of it.")
            targets = _plan(conn, row["contract_id"], work.instrument, nxt.warmup_days[0],
                            nxt.warmup_days[-1], gap_fill)
            work.jobs.append((_contract_info(row), [d for d in targets
                                                    if date.fromisoformat(d) in set(nxt.warmup_days)]))
    return True


def _record_assignment(conn, work):
    if work.assignment:
        n = set_active_contracts(conn, work.symbol, work.assignment, work.rule)
        logger.info(f"{work.symbol}: active contract recorded for {n} trading day(s) ({work.rule}).")
    if work.uncovered:
        # An earlier plan from an incomplete chain may have assigned these days.
        n = clear_active_contracts(conn, work.symbol, work.uncovered)
        if n:
            logger.info(f"{work.symbol}: cleared {n} stale active-contract assignment(s).")


def _resolve_online(app, conn, work, start_day, end_day, gap_fill):
    """Fills in ``work.jobs`` for a symbol whose contract(s) the database lacks."""
    if work.rolling:
        for info in app.discover_chain(work.instrument):
            _store_contract(conn, info)
        _plan_rolling(conn, work, start_day, end_day, gap_fill, allow_gaps=True)
        return

    info = app.resolve_contract(work.instrument, work.expiry)
    if not info:
        logger.error(f"Failed to resolve {work.label}.")
        work.jobs = []
        return
    _store_contract(conn, info)
    _plan_single(conn, work, get_contract_by_expiry(conn, work.symbol, info["expiry"]),
                 start_day, end_day, gap_fill)


def _collect_work(app, conn, work):
    """Downloads every planned day of one symbol. Returns the number of bars written."""
    now_utc = datetime.now(timezone.utc)
    total = 0
    for contract_info, targets in work.jobs:
        if not targets:
            continue
        label = _label(work.symbol, contract_info["expiry"])
        logger.info(f"{label}: fetching {len(targets)} trading day(s), one at a time.")
        stored_days, failed_days = 0, []
        for day_str in targets:
            written = _fetch_and_store_day(app, conn, work.instrument, contract_info, day_str, now_utc)
            if written is None:
                failed_days.append(day_str)
            else:
                total += written
                stored_days += 1
            time.sleep(1.0)
        logger.info(f"{label}: {stored_days}/{len(targets)} day(s) stored.")
        if failed_days:
            logger.warning(f"{label}: {len(failed_days)} day(s) failed, retried next run: {failed_days}")
    return total


def asset_source_records(collected_symbols):
    """config.ASSET_SOURCES flattened into asset_sources rows; ``collected_symbols``
    is the configured collection set."""
    collected = set(collected_symbols)
    rows = []
    for src in Config.asset_sources():
        inst = Config.instrument(src.symbol) if src.symbol else None
        rows.append({
            "asset": src.asset,
            "symbol": src.symbol,
            "sec_type": inst.sec_type if inst else None,
            "exchange": inst.exchange if inst else None,
            "what_to_show": inst.what_to_show if inst else None,
            "value_kind": inst.value_kind if inst else None,
            "value_unit": inst.value_unit if inst else None,
            "bps_per_unit": inst.bps_per_unit if inst else None,
            "max_age_minutes": src.max_age_minutes,
            "roll_rule": inst.roll.describe() if inst and inst.roll else None,
            "is_proxy": src.is_proxy,
            "optional": src.optional,
            "collected": src.symbol in collected,
            "description": src.description,
            "notes": src.notes or None,
        })
    return rows


def run_collection_workflow(dsn, host, port, client_id, instruments, days_to_download,
                            gap_fill=True, start=None, end=None, plan_only=False):
    """
    Collects one or more symbols. ``instruments`` is a sequence of
    ``(symbol, expiry)`` pairs; ``expiry`` pins a future to one contract, None
    lets a future with a RollRule follow its front contract day by day.

    Every contract shares a single IB connection, and therefore a single
    ``IBKRPacer``. That matters: one process per symbol would give each its own
    pacer, so each would undercount the others' requests and the combined volume
    could trip the HMDS rate limit that the pacer exists to respect.
    """
    init_database(dsn)
    conn = get_db_connection(dsn)

    app = None
    try:
        start_day, end_day = _resolve_window(days_to_download, start, end)
        if not plan_only:
            # "collected" reflects the configured set, not this run's --symbol subset,
            # so a one-off single-symbol run does not re-version every asset.
            register_asset_sources(conn, asset_source_records(Config.collect_symbols()))

        # --- Step 1: ask the database what is missing, before touching IB. ---
        # Contracts already known from an earlier run are planned offline. If
        # every requested symbol is fully covered, no connection is opened at all.
        work_items = []
        for symbol, expiry in instruments:
            instrument = _instrument_for(symbol)
            rolling = instrument.is_future and instrument.roll is not None and not expiry
            if instrument.is_future and not rolling and not expiry:
                expiry = Config.expiry_for(symbol)
            work = _Work(symbol, instrument, expiry if instrument.is_future else None, rolling)
            logger.info(f"Window: {start_day} -> {end_day} ({work.label}, {INTERVAL_LABEL}"
                        f"{', rolling' if rolling else ''}).")

            if rolling:
                if not _plan_rolling(conn, work, start_day, end_day, gap_fill):
                    logger.info(f"{symbol}: the stored contract chain does not cover the window; "
                                f"it must be discovered via IB first.")
            else:
                row = get_contract_by_expiry(conn, symbol, work.expiry)
                if row is not None:
                    _plan_single(conn, work, row, start_day, end_day, gap_fill)
                else:
                    logger.info(f"{work.label} is not in the database yet; it must be resolved via IB first.")

            if work.jobs is not None and not plan_only:
                _record_assignment(conn, work)
            if work.jobs is not None and work.pending_days == 0:
                logger.info(f"{work.label}: every trading day in the window is already stored.")
                continue
            work_items.append(work)

        if not work_items:
            logger.info("Nothing to download for any requested instrument.")
            return

        if plan_only:
            logger.info("--plan-only: stopping before connecting to IB.")
            return

        if not _IBAPI_AVAILABLE:
            logger.error("ibapi is not installed; cannot run a live collection.")
            sys.exit(1)

        # --- Step 2: connect once, reused by every contract. ---
        app = IBCollectorApp()
        logger.info(f"Connecting to IB at {host}:{port} (clientId={client_id})...")
        app.connect(host, port, client_id)
        threading.Thread(target=app.run, name="IBAPI_Thread", daemon=True).start()

        if not app.connect_event.wait(timeout=10.0):
            logger.error("Could not connect to IB Gateway/TWS. Is the API socket enabled?")
            sys.exit(1)

        # --- Step 3: one IB request per missing day, one atomic write per day. ---
        grand_total = 0
        for work in work_items:
            if work.jobs is None:
                _resolve_online(app, conn, work, start_day, end_day, gap_fill)
                _record_assignment(conn, work)
            grand_total += _collect_work(app, conn, work)

        logger.info(f"Collection complete across {len(work_items)} symbol(s): {grand_total} bar(s) written.")
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
    parser.add_argument("--symbol", type=str, default=",".join(Config.collect_symbols()),
                        help="Symbol, or a comma-separated list (e.g. NQ or ES,NQ,RTY,VIX). "
                             "Defaults to SYMBOLS plus CONTEXT_SYMBOLS")
    parser.add_argument("--expiry", type=str,
                        help="Pin every future to this contract month YYYYMM (e.g. 202609) "
                             "instead of following its roll rule; <SYMBOL>_EXPIRY pins one")
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

    symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]
    if not symbols:
        parser.error("--symbol must name at least one futures symbol.")

    # An explicit --expiry pins every future, <SYMBOL>_EXPIRY pins one; anything
    # else follows its roll rule (None). An index or stock has no expiry at all,
    # and --expiry must not invent one for it.
    def _expiry_for_arg(sym):
        instrument = Config.instrument(sym)
        if instrument is not None and not instrument.is_future:
            return None
        return args.expiry or Config.pinned_expiry(sym)

    instruments = [(s, _expiry_for_arg(s)) for s in symbols]

    run_collection_workflow(
        dsn=args.db, host=args.host, port=args.port, client_id=args.client_id,
        instruments=instruments, days_to_download=args.days,
        gap_fill=not args.full, start=args.start, end=args.end, plan_only=args.plan_only,
    )
