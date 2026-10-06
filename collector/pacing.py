# collector/pacing.py
"""
Pacing and Throttling Rules for Interactive Brokers Historical Data API.
Enforces limits to prevent triggering HMDS (Historical Market Data Service)
rate-limit errors (Error 162: query rate limit exceeded).

The pacer is shared by every thread of a collection (one IB connection, one pacer):
each request reserves its slot under the lock, then waits for it outside the lock,
so requests for different contracts go out together while one contract's stay spaced.
"""

import time
import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

class IBKRPacer:
    """
    Tracks and paces historical data requests to comply with IBKR API rate limits.

    Standard IBKR HMDS Rate Limits:
    1. Making identical requests (same contract, expiry, duration, bar size, whatToShow)
       within 15 seconds is strictly prohibited.
    2. Making more than 60 historical data requests within any rolling 10-minute (600 seconds)
       window will trigger HMDS throttling.
    3. Six or more requests for the same contract within 2 seconds is a violation: requests
       for one contract are ``default_delay`` apart; requests for different contracts only
       ``global_delay`` - IB's burst limit is per contract.
    """

    def __init__(self, max_requests_per_window=60, window_seconds=600, default_delay=2.0, global_delay=0.25):
        self.max_requests = max_requests_per_window
        self.window_seconds = window_seconds
        self.default_delay = default_delay          # between two requests for the same contract
        self.global_delay = global_delay            # between any two requests

        # The reserved slots, oldest first (strictly increasing: each is at least global_delay after the last)
        self.request_history = deque()

        # Tracks unique identifiers of recent requests to avoid making duplicate requests within 15 seconds
        # Stores tuples of (contract_id, duration, bar_size, end_time) mapped to their slot
        self.recent_requests = {}
        self.duplicate_lockout_seconds = 15.0
        self.last_by_contract = {}                  # contract_id -> its latest slot
        self.last_any: Optional[float] = None       # the latest slot of any request
        self._lock = threading.Lock()

    def reserve(self, contract_id: int, duration: str, bar_size: str, end_time_str: str,
                min_spacing: float = None, now: Optional[float] = None) -> float:
        """
        Reserves the earliest slot (epoch seconds) a request may go out at under every rule - identical
        requests 15 s apart, at most ``max_requests`` in any ``window_seconds``, ``default_delay`` (or
        ``min_spacing``) after the same contract's last request and ``global_delay`` after any - and records
        it. Thread-safe; it does not wait (``pace`` does).
        """
        with self._lock:
            now = time.time() if now is None else now
            at = now
            key = (contract_id, duration, bar_size, end_time_str)
            last = self.recent_requests.get(key)
            if last is not None and at < last + self.duplicate_lockout_seconds:
                at = last + self.duplicate_lockout_seconds
                logger.warning(f"[Pacing] Identical request detected within {self.duplicate_lockout_seconds}s "
                               f"limit. Waiting {at - now:.2f} seconds.")
            if len(self.request_history) >= self.max_requests:
                bound = self.request_history[-self.max_requests] + self.window_seconds
                if bound > at:
                    logger.warning(f"[Pacing] Volume limit ({self.max_requests} requests in "
                                   f"{self.window_seconds:.0f} s). Waiting {bound - now:.2f} seconds.")
                    at = bound
            spacing = self.default_delay if min_spacing is None else min_spacing
            mine = self.last_by_contract.get(contract_id)
            if mine is not None:
                at = max(at, mine + spacing)
            if self.last_any is not None:
                at = max(at, self.last_any + self.global_delay)
            self.recent_requests[key] = at
            self.last_by_contract[contract_id] = at
            self.last_any = at
            self.request_history.append(at)
            while len(self.request_history) > self.max_requests:   # only the last max_requests bound a slot
                self.request_history.popleft()
            return at

    def pace(self, contract_id: int, duration: str, bar_size: str, end_time_str: str,
             min_spacing: float = None) -> float:
        """Reserves the request's slot and sleeps until it (outside the lock). Returns the seconds slept."""
        at = self.reserve(contract_id, duration, bar_size, end_time_str, min_spacing)
        wait = at - time.time()
        if wait > 0:
            time.sleep(wait)
        return max(0.0, wait)

    def handle_rate_limit_error(self, backoff_seconds=30.0):
        """
        If the IB Gateway returns a rate-limit error (e.g. Error Code 162), no request
        goes out for ``backoff_seconds``: the next slot is pushed back. It returns at
        once - it is called from IB's reader thread, which must keep delivering the
        answers of the requests in flight.
        """
        logger.error(
            f"[Rate Limit Exceeded] IB Gateway reported rate limits. "
            f"Holding back every request for {backoff_seconds} seconds..."
        )
        with self._lock:
            self.last_any = max(self.last_any or 0.0, time.time() + backoff_seconds - self.global_delay)


def chunk_date_range(start_utc: datetime, end_utc: datetime, chunk_days: int = 1):
    """
    Slices a long historical collection timeframe into smaller datetime chunks.
    IBKR recommends fetching high-resolution bars (e.g. 1-minute bars) in daily chunks
    to avoid heavy processing latency or query failures.

    Returns a list of tuples containing (chunk_start, chunk_end).
    """
    if start_utc >= end_utc:
        raise ValueError("Start date must be earlier than end date.")

    chunks = []
    current_start = start_utc
    delta = timedelta(days=chunk_days)

    while current_start < end_utc:
        current_end = min(current_start + delta, end_utc)
        chunks.append((current_start, current_end))
        current_start = current_end

    return chunks


def format_ibkr_datetime(dt: datetime) -> str:
    """
    Converts a standard UTC Python datetime object into the precise
    string format required by the IBKR historical API.

    Format: 'YYYYMMDD HH:mm:ss UTC'
    """
    if dt.tzinfo is None:
        # Assume UTC if timezone naive
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.strftime("%Y%m%d %H:%M:%S UTC")
