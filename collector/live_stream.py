#!/usr/bin/env python3
"""
Real-time 1-minute bars with a receive time per bar.

The historical collector (collector/ib_collector.py) re-downloads whole days and
cannot land the 09:28 bars of every instrument between 09:29:00 and the v2
forecast deadline. This streamer keeps one IB keep-up-to-date 1-minute stream per
instrument open (``reqHistoricalData(..., keepUpToDate=True)``: the same bars the
historical service builds, updated every few seconds) and stores each minute the
moment it is final:

  * ``bar_receipts`` (append-only) gets the bar with ``received_at`` - the time the
    pipeline knew that minute's final value - and how it was finalised;
  * ``bars`` gets the same bar through ``queries.save_live_bars``, so the feature
    builder reads it like any other. It is stored ``is_completed = 0``: the day
    stays PARTIAL and the regular collector re-downloads it once settled.

A minute is final when the stream moves on to a later minute (``next_bar``), or
``--grace`` seconds after it ended with no later update (``timer``; a thin
instrument may not trade for minutes). A value IB changes afterwards is stored as
a new revision (``late_update``). Minutes without a print produce no bar, as in
the historical store.

IB updates a keep-up-to-date bar every ~5 seconds, so a streamed close can miss
the last seconds of its minute. For the minutes a forecast depends on
(``--confirm-at``, default 09:28 ET) the streamer re-reads the finished minute
with one short historical request per instrument right after it ends; that
``confirm_fetch`` revision is IB's own historical bar.

Run it before the open, next to the forecast (both in New York time):

    python -m collector.live_stream                      # all collected symbols, until 09:31 ET
    python -m collector.live_stream --symbol NQ,ES --until 16:15 --confirm-at 09:28,15:59

It uses its own IB client id (IB_CLIENT_ID + 1 by default) so it can run
alongside the historical collector.
"""

import argparse
import logging
import os
import queue
import socket
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config
from collector.ib_collector import (
    BAR_SIZE, INTERVAL_LABEL, IBCollectorApp, _IBAPI_AVAILABLE, _contract_info, _instrument_for,
    _store_contract, bar_to_dict,
)
from collector.pacing import format_ibkr_datetime
from collector.rolls import front_contracts
from database.connection import get_db_connection, init_database
from database.queries import (
    get_active_contract, get_contract_by_expiry, list_future_chain, save_live_bars,
)
from features.session_windows import NY_TZ

logger = logging.getLogger("LiveStream")

ONE_MIN = timedelta(minutes=1)
FINALISE_GRACE = timedelta(seconds=5)
_VALUES = ("open", "high", "low", "close", "volume", "wap", "bar_count")
# IB: "connectivity restored - data lost"; every stream must be requested again.
_RESUBSCRIBE_CODES = {1101}

Key = Tuple[int, str]            # (contract_id, price_type)


def _start_of(bar) -> datetime:
    return datetime.strptime(str(bar["timestamp_utc"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _values(bar) -> tuple:
    return tuple(bar.get(k) for k in _VALUES)


class LiveBarAssembler:
    """
    Turns the stream of updates to each instrument's forming minute into final
    bars. Thread-safe: IB callbacks call ``update``, the main loop calls
    ``tick`` and ``confirm``. Every method returns the bars it finalised, each a
    bar dict plus ``key``, ``received_at`` and ``finalised_by``.

    ``require_volume`` keys (futures, stocks) never finalise a zero-volume bar:
    that minute had no print. Cash indices report no volume at all.
    """

    def __init__(self, grace: timedelta = FINALISE_GRACE, keep: timedelta = timedelta(hours=2)):
        self.grace = grace
        self.keep = keep
        self._lock = threading.Lock()
        self._current: Dict[Key, dict] = {}          # key -> {"bar", "start", "emitted"}
        self._emitted: Dict[Tuple[Key, datetime], tuple] = {}
        self.require_volume: set = set()

    def _emit(self, key, bar, start, now, how) -> Optional[dict]:
        if key in self.require_volume and not bar.get("volume"):
            return None
        values = _values(bar)
        self._emitted[(key, start)] = values
        cur = self._current.get(key)
        if cur is not None and cur["start"] == start:
            cur["emitted"] = values
        return dict(bar, key=key, received_at=now, finalised_by=how)

    def update(self, key: Key, bar: dict, now: datetime, initial: bool = False) -> List[dict]:
        start = _start_of(bar)
        out = []
        with self._lock:
            cur = self._current.get(key)
            if cur is None or start > cur["start"]:
                if cur is not None and cur["emitted"] is None:
                    out.append(self._emit(key, cur["bar"], cur["start"], now,
                                          "initial_fill" if initial else "next_bar"))
                self._current[key] = {"bar": bar, "start": start, "emitted": None}
            elif start == cur["start"]:
                cur["bar"] = bar
                if cur["emitted"] is not None and _values(bar) != cur["emitted"]:
                    out.append(self._emit(key, bar, start, now, "late_update"))
            else:
                # An update to a minute that is already behind the current one.
                prev = self._emitted.get((key, start))
                if prev is None or prev != _values(bar):
                    out.append(self._emit(key, bar, start, now, "late_update"))
        return [b for b in out if b is not None]

    def tick(self, now: datetime) -> List[dict]:
        """Finalises every forming minute that ended more than ``grace`` ago."""
        out = []
        with self._lock:
            for key, cur in self._current.items():
                if cur["emitted"] is None and cur["start"] + ONE_MIN + self.grace <= now:
                    out.append(self._emit(key, cur["bar"], cur["start"], now, "timer"))
                    if cur["emitted"] is None:      # a no-print minute: do not retry it
                        cur["emitted"] = ()
            horizon = now - self.keep
            for k in [k for k in self._emitted if k[1] < horizon]:
                del self._emitted[k]
        return [b for b in out if b is not None]

    def confirm(self, key: Key, bar: dict, now: datetime) -> List[dict]:
        """Records the historical service's value of a finished minute, when it differs."""
        start = _start_of(bar)
        with self._lock:
            if self._emitted.get((key, start)) == _values(bar):
                return []
            b = self._emit(key, bar, start, now, "confirm_fetch")
        return [b] if b is not None else []


class LiveStreamApp(IBCollectorApp):
    """IBCollectorApp plus keep-up-to-date streams routed into a LiveBarAssembler."""

    def __init__(self, assembler: LiveBarAssembler):
        super().__init__()
        self.assembler = assembler
        self.streams: Dict[int, Key] = {}
        self.filling: set = set()
        self.finalised: "queue.Queue[dict]" = queue.Queue()
        self.resubscribe = threading.Event()

    def _put(self, bars):
        for b in bars:
            self.finalised.put(b)

    def historicalData(self, reqId, bar):
        if reqId not in self.streams:
            return super().historicalData(reqId, bar)
        parsed = bar_to_dict(bar)
        if parsed is not None:
            self._put(self.assembler.update(self.streams[reqId], parsed, datetime.now(timezone.utc),
                                            initial=True))

    def historicalDataEnd(self, reqId, start, end):
        if reqId not in self.streams:
            return super().historicalDataEnd(reqId, start, end)
        self.filling.discard(reqId)
        logger.info(f"Stream {self.streams[reqId]} is live.")

    def historicalDataUpdate(self, reqId, bar):
        key = self.streams.get(reqId)
        if key is None:
            return
        parsed = bar_to_dict(bar)
        if parsed is not None:
            self._put(self.assembler.update(key, parsed, datetime.now(timezone.utc)))

    def error(self, reqId, *args):
        codes = [a for a in args[:2] if isinstance(a, int)]
        if any(c in _RESUBSCRIBE_CODES for c in codes):
            self.resubscribe.set()
        super().error(reqId, *args)

    def start_stream(self, info: dict, what_to_show: str, duration: str) -> int:
        req_id = self.get_next_req_id()
        key = (int(info["con_id"]), what_to_show)
        self.streams[req_id] = key
        self.filling.add(req_id)
        self.pacer.wait_if_necessary(info["con_id"], duration, BAR_SIZE, "stream", min_spacing=0.25)
        self.reqHistoricalData(
            reqId=req_id, contract=self.ib_contract_from_info(info), endDateTime="",
            durationStr=duration, barSizeSetting=BAR_SIZE, whatToShow=what_to_show,
            useRTH=0, formatDate=2, keepUpToDate=True, chartOptions=[],
        )
        self.pacer.register_request(info["con_id"], duration, BAR_SIZE, "stream")
        return req_id

    def stop_streams(self):
        for req_id in list(self.streams):
            try:
                self.cancelHistoricalData(req_id)
            except Exception:   # disconnecting anyway
                pass
        self.streams.clear()


def live_contract(conn, app, symbol: str, day: date) -> Optional[dict]:
    """
    The contract to stream for ``symbol`` today: the collector's active_contracts
    assignment, else the pinned/configured contract, else the roll rule over the
    stored chain, else IB resolution (stored for next time).
    """
    instrument = _instrument_for(symbol)
    row = get_active_contract(conn, symbol, day.isoformat())
    if row is None:
        if not instrument.is_future:
            row = get_contract_by_expiry(conn, symbol, None)
        elif Config.pinned_expiry(symbol) or instrument.roll is None:
            row = get_contract_by_expiry(conn, symbol, Config.pinned_expiry(symbol) or Config.expiry_for(symbol))
        else:
            row = front_contracts(list_future_chain(conn, symbol), [day], instrument.roll).get(day)
    if row is not None:
        return _contract_info(row)

    logger.info(f"{symbol}: no stored contract for {day}; resolving via IB.")
    if instrument.is_future and instrument.roll is not None and not Config.pinned_expiry(symbol):
        for info in app.discover_chain(instrument):
            _store_contract(conn, info)
        row = front_contracts(list_future_chain(conn, symbol), [day], instrument.roll).get(day)
        return _contract_info(row) if row is not None else None
    expiry = Config.pinned_expiry(symbol) or (Config.expiry_for(symbol) if instrument.is_future else None)
    info = app.resolve_contract(instrument, expiry)
    if info:
        _store_contract(conn, info)
    return info


def _et_instant(day: date, hhmm: str) -> datetime:
    t = datetime.strptime(hhmm, "%H:%M").time()
    return NY_TZ.localize(datetime.combine(day, t)).astimezone(timezone.utc)


def _store(conn, finalised: List[dict], expected: Dict[Key, int], stream_id: str) -> int:
    by_key: Dict[Key, List[dict]] = {}
    for b in finalised:
        by_key.setdefault(b["key"], []).append(b)
    stored = 0
    for (cid, price_type), bars in by_key.items():
        counts = save_live_bars(conn, cid, bars, interval=INTERVAL_LABEL, price_type=price_type,
                                expected_bar_count=expected.get((cid, price_type)), stream_id=stream_id)
        stored += counts["receipts"]
    return stored


def _confirm(app, conn, infos, minute_start: datetime, spacing: float) -> List[dict]:
    """One short historical request per stream for a just-finished minute."""
    out = []
    now = datetime.now(timezone.utc)
    for key, (info, what_to_show) in infos.items():
        bars = app.fetch_historical_bars(info, now, "180 S", what_to_show, min_spacing=spacing, timeout=10.0)
        for b in bars or []:
            if _start_of(b) == minute_start:
                out.extend(app.assembler.confirm(key, b, datetime.now(timezone.utc)))
    return out


def run_stream(dsn, host, port, client_id, symbols, until, confirm_at, duration, grace, confirm_delay,
               confirm_spacing):
    init_database(dsn)
    conn = get_db_connection(dsn)
    today = datetime.now(NY_TZ).date()
    until_at = _et_instant(today, until)
    confirms = sorted(_et_instant(today, t) for t in confirm_at)
    stream_id = f"{socket.gethostname()}:{os.getpid()}:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

    if not _IBAPI_AVAILABLE:
        logger.error("ibapi is not installed; cannot stream.")
        return 1

    assembler = LiveBarAssembler(grace=timedelta(seconds=grace))
    app = LiveStreamApp(assembler)
    logger.info(f"Connecting to IB at {host}:{port} (clientId={client_id})...")
    app.connect(host, port, client_id)
    threading.Thread(target=app.run, name="IBAPI_Thread", daemon=True).start()
    if not app.connect_event.wait(timeout=10.0):
        logger.error("Could not connect to IB Gateway/TWS. Is the API socket enabled?")
        return 1

    infos: Dict[Key, tuple] = {}
    expected: Dict[Key, int] = {}
    try:
        for symbol in symbols:
            instrument = _instrument_for(symbol)
            info = live_contract(conn, app, symbol, today)
            if info is None:
                logger.error(f"{symbol}: no contract to stream for {today}; skipped.")
                continue
            key = (int(info["con_id"]), instrument.what_to_show)
            infos[key] = (info, instrument.what_to_show)
            expected[key] = instrument.expected_bars
            if instrument.sec_type != "IND":
                assembler.require_volume.add(key)

        def subscribe():
            app.stop_streams()
            for info, what_to_show in infos.values():
                app.start_stream(info, what_to_show, duration)

        subscribe()
        logger.info(f"Streaming {len(infos)} instrument(s) until {until} ET "
                    f"(confirming {', '.join(confirm_at) or 'nothing'}).")

        while datetime.now(timezone.utc) < until_at:
            now = datetime.now(timezone.utc)
            for b in assembler.tick(now):
                app.finalised.put(b)
            while confirms and now >= confirms[0] + ONE_MIN + timedelta(seconds=confirm_delay):
                minute = confirms.pop(0)
                if now - minute < timedelta(minutes=10):
                    for b in _confirm(app, conn, infos, minute, confirm_spacing):
                        app.finalised.put(b)
            if app.resubscribe.is_set():
                logger.warning("IB reported lost market data; re-subscribing every stream.")
                app.resubscribe.clear()
                subscribe()

            batch = []
            while True:
                try:
                    batch.append(app.finalised.get_nowait())
                except queue.Empty:
                    break
            if batch:
                n = _store(conn, batch, expected, stream_id)
                logger.debug(f"Stored {n} new receipt(s) from {len(batch)} finalised bar(s).")
            time.sleep(0.25)
        return 0
    finally:
        app.stop_streams()
        rest = []
        while not app.finalised.empty():
            rest.append(app.finalised.get_nowait())
        if rest:
            _store(conn, rest, expected, stream_id)
        app.disconnect()
        conn.close()


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Stream real-time 1-minute bars from IB into the store")
    parser.add_argument("--db", default=Config.DATABASE_URL)
    parser.add_argument("--host", default=Config.IB_HOST)
    parser.add_argument("--port", type=int, default=Config.IB_PORT)
    parser.add_argument("--client-id", type=int, default=Config.IB_CLIENT_ID + 1,
                        help="IB client id (default IB_CLIENT_ID + 1, so the collector can run too)")
    parser.add_argument("--symbol", default=",".join(Config.collect_symbols()),
                        help="Comma-separated symbols (default: SYMBOLS plus CONTEXT_SYMBOLS)")
    parser.add_argument("--until", default="09:31", help="ET time (HH:MM) to stop streaming")
    parser.add_argument("--confirm-at", default="09:28",
                        help="Comma-separated ET bar starts (HH:MM) to re-read once finished; '' for none")
    parser.add_argument("--duration", default="3600 S",
                        help="History IB sends when a stream opens (fills gaps since the last download)")
    parser.add_argument("--grace", type=float, default=FINALISE_GRACE.total_seconds(),
                        help="Seconds after a minute ends before it is final without a later update")
    parser.add_argument("--confirm-delay", type=float, default=1.0,
                        help="Seconds after a confirm minute ends before re-reading it")
    parser.add_argument("--confirm-spacing", type=float, default=0.25,
                        help="Seconds between the per-instrument confirm requests")
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]
    confirm_at = [t.strip() for t in args.confirm_at.split(",") if t.strip()]
    return run_stream(args.db, args.host, args.port, args.client_id, symbols, args.until, confirm_at,
                      args.duration, args.grace, args.confirm_delay, args.confirm_spacing)


if __name__ == "__main__":
    sys.exit(main())
