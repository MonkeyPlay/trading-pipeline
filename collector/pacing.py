# collector/pacing.py
"""
Pacing and Throttling Rules for Interactive Brokers Historical Data API.
Enforces limits to prevent triggering HMDS (Historical Market Data Service)
rate-limit errors (Error 162: query rate limit exceeded).
"""

import time
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

class IBKRPacer:
    """
    Tracks and paces historical data requests to comply with IBKR API rate limits.
    
    Standard IBKR HMDS Rate Limits:
    1. Making identical requests (same contract, expiry, duration, bar size, whatToShow) 
       within 15 seconds is strictly prohibited.
    2. Making more than 60 historical data requests within any rolling 10-minute (600 seconds) 
       window will trigger HMDS throttling.
    3. Dynamic pacing based on bar size/duration to prevent overloading the gateway.
    """
    
    def __init__(self, max_requests_per_window=60, window_seconds=600, default_delay=2.0):
        self.max_requests = max_requests_per_window
        self.window_seconds = window_seconds
        self.default_delay = default_delay
        
        # Tracks timestamps of recent requests to maintain a sliding window
        self.request_history = deque()
        
        # Tracks unique identifiers of recent requests to avoid making duplicate requests within 15 seconds
        # Stores tuples of (contract_id, duration, bar_size, end_time) mapped to timestamp
        self.recent_requests = {}
        self.duplicate_lockout_seconds = 15.0

    def register_request(self, contract_id: int, duration: str, bar_size: str, end_time_str: str):
        """
        Registers a historical data request in the sliding pacing windows.
        """
        now = time.time()
        
        # 1. Add to sliding volume window
        self.request_history.append(now)
        
        # 2. Add to duplicate request cache
        req_key = (contract_id, duration, bar_size, end_time_str)
        self.recent_requests[req_key] = now
        
        logger.debug(f"Registered historical request for Contract {contract_id} ({bar_size}) ending at {end_time_str}")

    def wait_if_necessary(self, contract_id: int, duration: str, bar_size: str, end_time_str: str,
                          min_spacing: float = None):
        """
        Analyzes recent request logs and blocks execution (sleeps) if necessary to comply
        with volume windows and identical request locks.

        ``min_spacing`` overrides the default gap between consecutive requests, for a
        burst of small requests to *different* contracts (IB's burst limit is per
        contract); the volume window and the duplicate lock still apply.
        """
        now = time.time()
        
        # --- Rule 1: Prevent Duplicate Requests in 15 Seconds ---
        req_key = (contract_id, duration, bar_size, end_time_str)
        if req_key in self.recent_requests:
            elapsed = now - self.recent_requests[req_key]
            if elapsed < self.duplicate_lockout_seconds:
                wait_time = self.duplicate_lockout_seconds - elapsed
                logger.warning(
                    f"[Pacing] Identical request detected within {self.duplicate_lockout_seconds}s limit. "
                    f"Locking thread for {wait_time:.2f} seconds."
                )
                time.sleep(wait_time)
                now = time.time()
        
        # --- Rule 2: Enforce Sliding Window Request Volume Limit ---
        # Evict timestamps older than our window
        cutoff = now - self.window_seconds
        while self.request_history and self.request_history[0] < cutoff:
            self.request_history.popleft()
            
        # Check volume
        if len(self.request_history) >= self.max_requests:
            # The earliest request in the window dictates how long we wait until we can make another
            earliest_allowed_time = self.request_history[0] + self.window_seconds
            wait_time = earliest_allowed_time - now
            if wait_time > 0:
                logger.warning(
                    f"[Pacing] Volume limit warning ({len(self.request_history)}/{self.max_requests} requests). "
                    f"Throttling collection pipeline. Sleeping for {wait_time:.2f} seconds..."
                )
                time.sleep(wait_time)
                now = time.time()
                
        # --- Rule 3: Enforce Default Cooldown Delay ---
        # Ensures basic serial spacing between consecutive API requests
        if self.request_history:
            time_since_last = now - self.request_history[-1]
            spacing = self.default_delay if min_spacing is None else min_spacing
            if time_since_last < spacing:
                wait_time = spacing - time_since_last
                time.sleep(wait_time)

    def handle_rate_limit_error(self, backoff_seconds=30.0):
        """
        If the IB Gateway returns a rate-limit error (e.g. Error Code 162),
        this function blocks execution and clears some historical state to allow
        the server-side cooldown window to expire.
        """
        logger.error(
            f"[Rate Limit Exceeded] IB Gateway reported rate limits. "
            f"Backing off and pausing collection loop for {backoff_seconds} seconds..."
        )
        time.sleep(backoff_seconds)
        # Wipe sliding history on hard rate limits to re-align windows
        self.request_history.clear()
        self.recent_requests.clear()


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
