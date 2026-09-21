# database/queries.py
"""
SQLite database query interface for the NQ Trading Pipeline.
Provides robust functions to insert, update, and retrieve records across
all 7 core database tables, with strict typing, parameterized inputs,
and JSON handling for document storage fields.
"""

import json
import re
import sqlite3
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# ==========================================
# 1. CONTRACTS TABLE HANDLERS
# ==========================================

def upsert_contract(
    conn: sqlite3.Connection,
    contract_id: int,
    symbol: str,
    expiry: Optional[str],
    exchange: str,
    currency: str = "USD",
    tick_size: Optional[float] = None,
    multiplier: Optional[str] = None,
    sec_type: str = "FUT",
) -> None:
    """
    Inserts or updates a contract. Enables unique contract tracking for futures
    by ensuring each contract_id represents a separate NQ expiry.
    """
    query = """
    INSERT INTO contracts (contract_id, symbol, expiry, sec_type, exchange, currency, tick_size, multiplier)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(contract_id) DO UPDATE SET
        symbol=excluded.symbol,
        expiry=excluded.expiry,
        sec_type=excluded.sec_type,
        exchange=excluded.exchange,
        currency=excluded.currency,
        tick_size=excluded.tick_size,
        multiplier=excluded.multiplier;
    """
    try:
        with conn:
            conn.execute(query, (contract_id, symbol, expiry, sec_type, exchange, currency, tick_size, multiplier))
        logger.debug(f"Contract {contract_id} ({symbol}{expiry or ''}) upserted successfully.")
    except sqlite3.Error as e:
        logger.error(f"Failed to upsert contract {contract_id}: {e}")
        raise


def get_contract(conn: sqlite3.Connection, contract_id: int) -> Optional[sqlite3.Row]:
    """Retrieves metadata of a specific contract by its Unique ID."""
    try:
        return conn.execute("SELECT * FROM contracts WHERE contract_id = ?;", (contract_id,)).fetchone()
    except sqlite3.Error as e:
        logger.error(f"Failed to fetch contract {contract_id}: {e}")
        raise


def get_contract_by_expiry(conn: sqlite3.Connection, symbol: str, expiry: str) -> Optional[sqlite3.Row]:
    """
    Retrieves a contract by trading symbol and expiry.

    Callers usually have the contract month ('202609'), while IB resolves and
    stores the full last-trade date ('20260918'), so an exact match is tried
    first and a prefix match second. With several matching expiries the nearest
    one wins, which is the front month a bare contract month refers to.
    """
    try:
        row = conn.execute(
            "SELECT * FROM contracts WHERE symbol = ? AND expiry = ?;", (symbol, expiry)
        ).fetchone()
        if row is not None:
            return row
        return conn.execute(
            "SELECT * FROM contracts WHERE symbol = ? AND expiry LIKE ? "
            "ORDER BY expiry ASC LIMIT 1;",
            (symbol, f"{expiry}%"),
        ).fetchone()
    except sqlite3.Error as e:
        logger.error(f"Failed to fetch contract by symbol={symbol}, expiry={expiry}: {e}")
        raise


def list_contracts(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Lists all monitored contracts in the database."""
    try:
        return conn.execute("SELECT * FROM contracts ORDER BY symbol, expiry;").fetchall()
    except sqlite3.Error as e:
        logger.error(f"Failed to list contracts: {e}")
        raise


# ==========================================
# 2. TRADING DAYS + BARS (day-partitioned store)
# ==========================================
#
# The trading day is the unit of storage. Every bar carries the NY session date it
# belongs to, and ``session_days`` holds one row per (contract, interval,
# price_type, trading_day) recording what we know about that day. Bars are a child
# of that ledger row (FK, ON DELETE CASCADE), so a bar cannot exist outside a
# registered day and a day is always written or replaced as one atomic unit.

# Rough number of bars a full NQ electronic session (18:00 -> 17:00 ET, less the
# one-hour halt) yields per interval. Only bars containing trades are returned by
# IB, so thin overnight minutes are missing — these are yardsticks for judging
# completeness, not exact counts.
EXPECTED_BARS_PER_SESSION = {"1m": 1290, "5m": 258, "15m": 86, "30m": 43, "1h": 23}
# The same for a 09:30-16:00 ET regular session (390 minutes).
EXPECTED_RTH_BARS_PER_SESSION = {"1m": 390, "5m": 78, "15m": 26, "30m": 13, "1h": 7}

# A day counts as COMPLETE once it holds at least this fraction of the expected bars.
DAY_COMPLETE_RATIO = 0.9

DAY_STATUSES = ("COMPLETE", "PARTIAL", "EMPTY")

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_BAR_COLUMNS = (
    "contract_id", "interval", "price_type", "trading_day", "timestamp_utc", "session_scope",
    "open", "high", "low", "close", "volume", "wap", "bar_count", "source", "is_completed",
)

_BARS_INSERT = f"""
INSERT INTO bars ({", ".join(_BAR_COLUMNS)})
VALUES ({", ".join(":" + c for c in _BAR_COLUMNS)});
"""

_SESSION_DAY_KEY = "contract_id = ? AND interval = ? AND price_type = ? AND trading_day = ?"


def expected_bars_for(interval: str, rth_only: bool = False) -> Optional[int]:
    """Expected bar count for one full session at this interval, or None if unknown."""
    table = EXPECTED_RTH_BARS_PER_SESSION if rth_only else EXPECTED_BARS_PER_SESSION
    return table.get(interval)


def derive_day_status(bar_count: int, expected_bar_count: Optional[int], open_bar_count: int = 0) -> str:
    """
    Classifies a stored day.

      EMPTY    - the source returned nothing for this day (recorded so we do not ask again)
      PARTIAL  - fewer bars than expected, or bars still flagged is_completed = 0
      COMPLETE - enough settled bars; the collector will skip it
    """
    if bar_count <= 0:
        return "EMPTY"
    if open_bar_count > 0:
        return "PARTIAL"
    if expected_bar_count and bar_count < expected_bar_count * DAY_COMPLETE_RATIO:
        return "PARTIAL"
    return "COMPLETE"


def _validate_day(trading_day: Any) -> str:
    day = str(trading_day)
    if not _DAY_RE.match(day):
        raise ValueError(f"trading_day must be 'YYYY-MM-DD', got {trading_day!r}")
    return day


def _prepare_bar_rows(bars, contract_id, interval, price_type, trading_day, source):
    """
    Normalises bar dicts into insert rows, enforcing that every bar belongs to
    ``trading_day``. A bar with a missing or mismatched trading_day is a caller
    bug: silently filing it under the wrong day is exactly what this store exists
    to prevent, so it raises instead.
    """
    rows = []
    mismatched = set()
    for b in bars:
        day = b.get("trading_day")
        if day is None:
            raise ValueError(
                f"Bar at {b.get('timestamp_utc')!r} has no trading_day; "
                f"compute it with features.session_windows.get_trading_day_date()."
            )
        if str(day) != trading_day:
            mismatched.add(str(day))
            continue

        scope = str(b.get("session_scope", "ETH"))
        rows.append({
            "contract_id": int(contract_id),
            "interval": str(interval),
            "price_type": str(price_type),
            "trading_day": trading_day,
            "timestamp_utc": str(b["timestamp_utc"]),
            "session_scope": scope if scope in ("RTH", "ETH") else "ETH",
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
            "volume": int(b["volume"]),
            "wap": _opt(b.get("wap"), float),
            "bar_count": _opt(b.get("bar_count"), int),
            "source": str(b.get("source", source or "IBKR")),
            "is_completed": 0 if int(b.get("is_completed", 1)) == 0 else 1,
        })

    if mismatched:
        raise ValueError(
            f"save_trading_day({trading_day}) received bars from other trading day(s): "
            f"{sorted(mismatched)}. Split the batch by day before saving."
        )

    # One timestamp maps to one bar; keep the last occurrence of any duplicate.
    deduped = {r["timestamp_utc"]: r for r in rows}
    return list(deduped.values())


def _opt(value, cast):
    if value is None or (isinstance(value, float) and value != value):  # NaN
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _day_stats(conn, contract_id, interval, price_type, trading_day):
    row = conn.execute(
        "SELECT COUNT(*), "
        "       COALESCE(SUM(CASE WHEN session_scope = 'RTH' THEN 1 ELSE 0 END), 0), "
        "       COALESCE(SUM(CASE WHEN is_completed = 0 THEN 1 ELSE 0 END), 0), "
        "       MIN(timestamp_utc), MAX(timestamp_utc) "
        f"FROM bars WHERE {_SESSION_DAY_KEY};",
        (contract_id, interval, price_type, trading_day),
    ).fetchone()
    return {
        "bar_count": int(row[0]),
        "rth_bar_count": int(row[1]),
        "open_bar_count": int(row[2]),
        "first_bar_utc": row[3],
        "last_bar_utc": row[4],
    }


def save_trading_day(
    conn: sqlite3.Connection,
    contract_id: int,
    trading_day: str,
    bars: List[Dict[str, Any]],
    interval: str = "1m",
    price_type: str = "TRADES",
    source: str = "IBKR",
    expected_bar_count: Optional[int] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Writes exactly one trading day, atomically.

    The day's existing bars are deleted and replaced by ``bars`` inside a single
    transaction, and the ``session_days`` ledger row is created or refreshed from
    the result. Re-running with the same day is therefore idempotent, and a day is
    never left half-written.

    An empty ``bars`` list is meaningful: it records that the source had nothing
    for this day (status 'EMPTY'), so the collector remembers not to ask again.

    Each bar dict needs ``timestamp_utc``, ``trading_day`` (must equal
    ``trading_day``), OHLC, ``volume``, and optionally ``session_scope``, ``wap``,
    ``bar_count``, ``source``, ``is_completed``.

    Returns the stored day summary.
    """
    day = _validate_day(trading_day)
    if expected_bar_count is None:
        expected_bar_count = expected_bars_for(interval)

    rows = _prepare_bar_rows(bars, contract_id, interval, price_type, day, source)
    key = (int(contract_id), str(interval), str(price_type), day)

    try:
        with conn:
            # The ledger row is the parent of the bars, so it must exist first.
            conn.execute(
                "INSERT INTO session_days (contract_id, interval, price_type, trading_day, "
                "                          status, expected_bar_count, source, fetched_at) "
                "VALUES (?, ?, ?, ?, 'EMPTY', ?, ?, datetime('now')) "
                "ON CONFLICT(contract_id, interval, price_type, trading_day) DO UPDATE SET "
                "    expected_bar_count = excluded.expected_bar_count, "
                "    source = excluded.source, "
                "    fetched_at = excluded.fetched_at;",
                key + (expected_bar_count, source),
            )
            conn.execute(f"DELETE FROM bars WHERE {_SESSION_DAY_KEY};", key)
            if rows:
                conn.executemany(_BARS_INSERT, rows)

            stats = _day_stats(conn, *key)
            resolved = status or derive_day_status(
                stats["bar_count"], expected_bar_count, stats["open_bar_count"]
            )
            if resolved not in DAY_STATUSES:
                raise ValueError(f"status must be one of {DAY_STATUSES}, got {resolved!r}")

            conn.execute(
                "UPDATE session_days SET status = ?, bar_count = ?, rth_bar_count = ?, "
                "    open_bar_count = ?, first_bar_utc = ?, last_bar_utc = ? "
                f"WHERE {_SESSION_DAY_KEY};",
                (resolved, stats["bar_count"], stats["rth_bar_count"], stats["open_bar_count"],
                 stats["first_bar_utc"], stats["last_bar_utc"]) + key,
            )
    except sqlite3.Error as e:
        logger.error(f"Failed to save trading day {day} for contract {contract_id}: {e}")
        raise

    summary = {"contract_id": contract_id, "interval": interval, "price_type": price_type,
               "trading_day": day, "status": resolved, "expected_bar_count": expected_bar_count,
               **stats}
    logger.info(
        f"Saved {day}: {stats['bar_count']} bar(s) [{resolved}]"
        + (f", {stats['open_bar_count']} still open" if stats["open_bar_count"] else "")
    )
    return summary


def save_bars_by_day(
    conn: sqlite3.Connection,
    bars: List[Dict[str, Any]],
    interval: str = "1m",
    price_type: str = "TRADES",
    source: str = "IBKR",
    expected_bar_count: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Bulk path: groups bars by ``contract_id`` + ``trading_day`` and writes each day
    through :func:`save_trading_day`. Every day touched is fully replaced, so a
    batch that covers only part of a day would truncate it — pass whole days.
    """
    grouped: Dict[Any, List[Dict[str, Any]]] = {}
    for b in bars:
        if b.get("trading_day") is None:
            raise ValueError(f"Bar at {b.get('timestamp_utc')!r} has no trading_day.")
        grouped.setdefault((int(b["contract_id"]), str(b["trading_day"])), []).append(b)

    return [
        save_trading_day(
            conn, contract_id=cid, trading_day=day, bars=day_bars, interval=interval,
            price_type=price_type, source=source, expected_bar_count=expected_bar_count,
        )
        for (cid, day), day_bars in sorted(grouped.items())
    ]


def delete_trading_day(
    conn: sqlite3.Connection,
    contract_id: int,
    trading_day: str,
    interval: str = "1m",
    price_type: str = "TRADES",
) -> int:
    """Removes a day from the ledger; its bars go with it (ON DELETE CASCADE)."""
    key = (contract_id, interval, price_type, _validate_day(trading_day))
    try:
        with conn:
            cursor = conn.execute(f"DELETE FROM session_days WHERE {_SESSION_DAY_KEY};", key)
        return cursor.rowcount
    except sqlite3.Error as e:
        logger.error(f"Failed to delete trading day {trading_day}: {e}")
        raise


def get_session_day(
    conn: sqlite3.Connection,
    contract_id: int,
    trading_day: str,
    interval: str = "1m",
    price_type: str = "TRADES",
) -> Optional[sqlite3.Row]:
    """The ledger row for one stored day, or None if that day was never fetched."""
    return conn.execute(
        f"SELECT * FROM session_days WHERE {_SESSION_DAY_KEY};",
        (contract_id, interval, price_type, _validate_day(trading_day)),
    ).fetchone()


def get_stored_trading_days(
    conn: sqlite3.Connection,
    contract_id: int,
    interval: str = "1m",
    price_type: str = "TRADES",
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, sqlite3.Row]:
    """
    The ledger for a contract as ``{trading_day: row}`` — the single query the
    collector runs before contacting IB to decide what it still needs.
    ``start`` / ``end`` are inclusive 'YYYY-MM-DD' bounds.
    """
    clauses = ["contract_id = ?", "interval = ?", "price_type = ?"]
    params: List[Any] = [contract_id, interval, price_type]
    if start is not None:
        clauses.append("trading_day >= ?")
        params.append(_validate_day(start))
    if end is not None:
        clauses.append("trading_day <= ?")
        params.append(_validate_day(end))

    try:
        rows = conn.execute(
            f"SELECT * FROM session_days WHERE {' AND '.join(clauses)} ORDER BY trading_day;",
            tuple(params),
        ).fetchall()
        return {r["trading_day"]: r for r in rows}
    except sqlite3.Error as e:
        logger.error(f"Failed to read the session_days ledger for contract {contract_id}: {e}")
        raise


def list_trading_days(
    conn: sqlite3.Connection,
    contract_id: int,
    interval: str = "1m",
    price_type: str = "TRADES",
    statuses: Optional[List[str]] = None,
    limit: int = 100,
) -> List[str]:
    """Stored trading days, newest first. Defaults to days that actually hold bars."""
    clauses = ["contract_id = ?", "interval = ?", "price_type = ?", "bar_count > 0"]
    params: List[Any] = [contract_id, interval, price_type]
    if statuses:
        clauses.append(f"status IN ({', '.join('?' * len(statuses))})")
        params.extend(statuses)
    params.append(limit)

    rows = conn.execute(
        f"SELECT trading_day FROM session_days WHERE {' AND '.join(clauses)} "
        f"ORDER BY trading_day DESC LIMIT ?;",
        tuple(params),
    ).fetchall()
    return [r["trading_day"] for r in rows]


def get_day_bars(
    conn: sqlite3.Connection,
    contract_id: int,
    trading_day: str,
    interval: str = "1m",
    price_type: str = "TRADES",
    session_scope: Optional[str] = None,
) -> List[sqlite3.Row]:
    """Every bar of one trading day, in time order. Hits the bars primary key directly."""
    clauses = [_SESSION_DAY_KEY]
    params: List[Any] = [contract_id, interval, price_type, _validate_day(trading_day)]
    if session_scope is not None:
        clauses.append("session_scope = ?")
        params.append(session_scope)

    return conn.execute(
        f"SELECT * FROM bars WHERE {' AND '.join(clauses)} ORDER BY timestamp_utc ASC;",
        tuple(params),
    ).fetchall()


def get_bars(
    conn: sqlite3.Connection,
    contract_id: int,
    interval: str,
    start_utc: Optional[str] = None,
    end_utc: Optional[str] = None,
    price_type: str = "TRADES",
    session_scope: Optional[str] = None,
    trading_day: Optional[str] = None,
    start_day: Optional[str] = None,
    end_day: Optional[str] = None,
) -> List[sqlite3.Row]:
    """
    Retrieves historical candles across days.

    Filters:
      - start_utc / end_utc   : inclusive UTC timestamp bounds
      - session_scope         : 'RTH' or 'ETH'
      - trading_day           : a single NY session date 'YYYY-MM-DD'
      - start_day / end_day   : inclusive NY session-date bounds

    Prefer :func:`get_day_bars` when you want exactly one session.
    """
    clauses = ["contract_id = ?", "interval = ?", "price_type = ?"]
    params: List[Any] = [contract_id, interval, price_type]

    if trading_day is not None:
        clauses.append("trading_day = ?")
        params.append(_validate_day(trading_day))
    if start_day is not None:
        clauses.append("trading_day >= ?")
        params.append(_validate_day(start_day))
    if end_day is not None:
        clauses.append("trading_day <= ?")
        params.append(_validate_day(end_day))
    if start_utc is not None:
        clauses.append("timestamp_utc >= ?")
        params.append(start_utc)
    if end_utc is not None:
        clauses.append("timestamp_utc <= ?")
        params.append(end_utc)
    if session_scope is not None:
        clauses.append("session_scope = ?")
        params.append(session_scope)

    query = f"SELECT * FROM bars WHERE {' AND '.join(clauses)} ORDER BY timestamp_utc ASC;"
    try:
        return conn.execute(query, tuple(params)).fetchall()
    except sqlite3.Error as e:
        logger.error(f"Failed to retrieve bars: {e}")
        raise


_LEDGER_UPSERT_FROM_BARS = """
INSERT INTO session_days (
    contract_id, interval, price_type, trading_day, status, bar_count, rth_bar_count,
    open_bar_count, expected_bar_count, first_bar_utc, last_bar_utc, source, fetched_at
)
SELECT
    b.contract_id, b.interval, b.price_type, b.trading_day,
    CASE
        WHEN SUM(CASE WHEN b.is_completed = 0 THEN 1 ELSE 0 END) > 0 THEN 'PARTIAL'
        WHEN COUNT(*) < :threshold THEN 'PARTIAL'
        ELSE 'COMPLETE'
    END,
    COUNT(*),
    SUM(CASE WHEN b.session_scope = 'RTH' THEN 1 ELSE 0 END),
    SUM(CASE WHEN b.is_completed = 0 THEN 1 ELSE 0 END),
    :expected,
    MIN(b.timestamp_utc), MAX(b.timestamp_utc),
    COALESCE(MIN(b.source), 'IBKR'),
    COALESCE(
        (SELECT s.fetched_at FROM session_days s
          WHERE s.contract_id = b.contract_id AND s.interval = b.interval
            AND s.price_type = b.price_type AND s.trading_day = b.trading_day),
        datetime('now')
    )
FROM bars b
WHERE b.interval = :interval AND b.price_type = :price_type
GROUP BY b.contract_id, b.interval, b.price_type, b.trading_day
ON CONFLICT(contract_id, interval, price_type, trading_day) DO UPDATE SET
    status = excluded.status,
    bar_count = excluded.bar_count,
    rth_bar_count = excluded.rth_bar_count,
    open_bar_count = excluded.open_bar_count,
    expected_bar_count = excluded.expected_bar_count,
    first_bar_utc = excluded.first_bar_utc,
    last_bar_utc = excluded.last_bar_utc,
    source = excluded.source;
"""

# Ledger rows whose bars are gone are kept, not deleted: an 'EMPTY' day records
# that the source was already asked and had nothing.
_LEDGER_MARK_EMPTY = """
UPDATE session_days
   SET status = 'EMPTY', bar_count = 0, rth_bar_count = 0, open_bar_count = 0,
       first_bar_utc = NULL, last_bar_utc = NULL
 WHERE NOT EXISTS (
     SELECT 1 FROM bars b
      WHERE b.contract_id = session_days.contract_id
        AND b.interval = session_days.interval
        AND b.price_type = session_days.price_type
        AND b.trading_day = session_days.trading_day
 );
"""


def rebuild_session_days(conn: sqlite3.Connection) -> int:
    """
    Recomputes the ``session_days`` ledger from the bars actually stored.

    Idempotent repair path, used by migration 0003 and by
    ``python -m database.backfill``. Days that hold no bars are marked 'EMPTY'
    rather than dropped, so a recorded "source had nothing" is not lost.
    Returns the number of ledger rows the bars table accounts for.
    """
    combos = conn.execute(
        "SELECT DISTINCT interval, price_type FROM bars;"
    ).fetchall()

    try:
        with conn:
            for combo in combos:
                interval, price_type = combo[0], combo[1]
                expected = expected_bars_for(interval)
                conn.execute(_LEDGER_UPSERT_FROM_BARS, {
                    "interval": interval,
                    "price_type": price_type,
                    "expected": expected,
                    "threshold": (expected * DAY_COMPLETE_RATIO) if expected else 0,
                })
            conn.execute(_LEDGER_MARK_EMPTY)
            indexed = conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM bars "
                "GROUP BY contract_id, interval, price_type, trading_day);"
            ).fetchone()[0]
    except sqlite3.Error as e:
        logger.error(f"Failed to rebuild the session_days ledger: {e}")
        raise
    return int(indexed)


# ==========================================
# 3. COLLECTION RUNS TABLE HANDLERS
# ==========================================

def create_collection_run(
    conn: sqlite3.Connection,
    contract_id: int,
    requested_start_utc: str,
    requested_end_utc: str,
    trading_day: Optional[str] = None,
    interval: Optional[str] = None,
    download_status: str = "PENDING",
    errors: Optional[str] = None,
    missing_intervals: Optional[List[str]] = None,
) -> int:
    """
    Opens an audit-log entry for one download. The collector fetches a day at a
    time, so ``trading_day`` records which NY session the IB window was aimed at.
    """
    query = """
    INSERT INTO collection_runs (
        contract_id, trading_day, interval, requested_start_utc, requested_end_utc,
        download_status, errors, missing_intervals, last_successful_update
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'));
    """
    missing_json = json.dumps(missing_intervals) if missing_intervals is not None else None
    try:
        with conn:
            cursor = conn.execute(query, (
                contract_id, trading_day, interval, requested_start_utc, requested_end_utc,
                download_status, errors, missing_json,
            ))
            return cursor.lastrowid
    except sqlite3.Error as e:
        logger.error(f"Failed to log collection run start: {e}")
        raise


def update_collection_run(
    conn: sqlite3.Connection,
    run_id: int,
    download_status: str,
    errors: Optional[str] = None,
    missing_intervals: Optional[List[str]] = None,
    bars_written: Optional[int] = None,
) -> None:
    """Updates status and logs gaps or error tracebacks upon completion/failure."""
    query = """
    UPDATE collection_runs
    SET download_status = ?, errors = ?, missing_intervals = ?, bars_written = ?,
        last_successful_update = datetime('now')
    WHERE run_id = ?;
    """
    missing_json = json.dumps(missing_intervals) if missing_intervals is not None else None
    try:
        with conn:
            conn.execute(query, (download_status, errors, missing_json, bars_written, run_id))
    except sqlite3.Error as e:
        logger.error(f"Failed to update collection run {run_id}: {e}")
        raise


def get_collection_runs_for_day(
    conn: sqlite3.Connection, contract_id: int, trading_day: str
) -> List[sqlite3.Row]:
    """Every download attempt recorded for one trading day, newest first."""
    return conn.execute(
        "SELECT * FROM collection_runs WHERE contract_id = ? AND trading_day = ? "
        "ORDER BY run_id DESC;",
        (contract_id, _validate_day(trading_day)),
    ).fetchall()


def get_latest_collection_run(conn: sqlite3.Connection, contract_id: int) -> Optional[sqlite3.Row]:
    """Retrieves the last recorded run for a contract."""
    try:
        return conn.execute(
            "SELECT * FROM collection_runs WHERE contract_id = ? ORDER BY run_id DESC LIMIT 1;",
            (contract_id,),
        ).fetchone()
    except sqlite3.Error as e:
        logger.error(f"Failed to fetch latest run for contract {contract_id}: {e}")
        raise


# ==========================================
# 4. FEATURE SNAPSHOTS TABLE HANDLERS
# ==========================================

def save_feature_snapshot(
    conn: sqlite3.Connection,
    contract_id: int,
    timestamp_utc: str,
    previous_rth_high: Optional[float],
    previous_rth_low: Optional[float],
    previous_rth_close: Optional[float],
    overnight_high: Optional[float],
    overnight_low: Optional[float],
    overnight_range: Optional[float],
    gap: Optional[float],
    pre_open_direction: str,
    historical_volatility: Optional[float],
    vwap: Optional[float],
    raw_features: Dict[str, Any],
    feature_version: str,
    data_quality_status: str = "VALID",
) -> int:
    """
    Saves computed pre-open metrics frozen at the cutoff time.
    Idempotent on (contract_id, timestamp_utc, feature_version): re-running
    updates the existing row instead of creating duplicates. Returns snapshot_id.
    """
    query = """
    INSERT INTO feature_snapshots (
        contract_id, timestamp_utc, previous_rth_high, previous_rth_low, previous_rth_close,
        overnight_high, overnight_low, overnight_range, gap, pre_open_direction,
        historical_volatility, vwap, raw_features, feature_version, data_quality_status
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(contract_id, timestamp_utc, feature_version) DO UPDATE SET
        previous_rth_high=excluded.previous_rth_high,
        previous_rth_low=excluded.previous_rth_low,
        previous_rth_close=excluded.previous_rth_close,
        overnight_high=excluded.overnight_high,
        overnight_low=excluded.overnight_low,
        overnight_range=excluded.overnight_range,
        gap=excluded.gap,
        pre_open_direction=excluded.pre_open_direction,
        historical_volatility=excluded.historical_volatility,
        vwap=excluded.vwap,
        raw_features=excluded.raw_features,
        data_quality_status=excluded.data_quality_status;
    """
    try:
        with conn:
            conn.execute(query, (
                contract_id, timestamp_utc, previous_rth_high, previous_rth_low, previous_rth_close,
                overnight_high, overnight_low, overnight_range, gap, pre_open_direction,
                historical_volatility, vwap, json.dumps(raw_features), feature_version, data_quality_status,
            ))
            row = conn.execute(
                "SELECT snapshot_id FROM feature_snapshots "
                "WHERE contract_id = ? AND timestamp_utc = ? AND feature_version = ?;",
                (contract_id, timestamp_utc, feature_version),
            ).fetchone()
            return int(row["snapshot_id"])
    except sqlite3.Error as e:
        logger.error(f"Failed to save feature snapshot: {e}")
        raise


def get_feature_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> Optional[sqlite3.Row]:
    """Retrieves a feature snapshot by ID."""
    try:
        return conn.execute(
            "SELECT * FROM feature_snapshots WHERE snapshot_id = ?;", (snapshot_id,)
        ).fetchone()
    except sqlite3.Error as e:
        logger.error(f"Failed to get snapshot {snapshot_id}: {e}")
        raise


def get_latest_feature_snapshot(conn: sqlite3.Connection, contract_id: int) -> Optional[sqlite3.Row]:
    """Retrieves the newest feature snapshot generated for a contract."""
    try:
        return conn.execute(
            "SELECT * FROM feature_snapshots WHERE contract_id = ? "
            "ORDER BY timestamp_utc DESC, snapshot_id DESC LIMIT 1;",
            (contract_id,),
        ).fetchone()
    except sqlite3.Error as e:
        logger.error(f"Failed to get latest snapshot for contract {contract_id}: {e}")
        raise


# ==========================================
# 5. PREDICTIONS TABLE HANDLERS
# ==========================================

def save_prediction(
    conn: sqlite3.Connection,
    contract_id: int,
    forecast_cutoff: str,
    snapshot_id: int,
    model_version: str,
    prompt_version: str,
    opening_bias: Optional[str],
    scenarios: Dict[str, Any],
    probabilities: Dict[str, float],
    raw_response: str,
    created_at: str,
) -> int:
    """Saves the LLM prediction run. Scenarios and probabilities are stored as JSON."""
    query = """
    INSERT INTO predictions (
        contract_id, forecast_cutoff, snapshot_id, model_version, prompt_version,
        opening_bias, scenarios, probabilities, raw_response, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """
    try:
        with conn:
            cursor = conn.execute(query, (
                contract_id, forecast_cutoff, snapshot_id, model_version, prompt_version,
                opening_bias, json.dumps(scenarios), json.dumps(probabilities), raw_response, created_at,
            ))
            return cursor.lastrowid
    except sqlite3.Error as e:
        logger.error(f"Failed to save prediction: {e}")
        raise


def get_prediction(conn: sqlite3.Connection, prediction_id: int) -> Optional[sqlite3.Row]:
    """Retrieves an LLM prediction by its ID."""
    try:
        return conn.execute(
            "SELECT * FROM predictions WHERE prediction_id = ?;", (prediction_id,)
        ).fetchone()
    except sqlite3.Error as e:
        logger.error(f"Failed to get prediction {prediction_id}: {e}")
        raise


def get_predictions_by_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> List[sqlite3.Row]:
    """Retrieves model predictions associated with a specific feature snapshot."""
    try:
        return conn.execute(
            "SELECT * FROM predictions WHERE snapshot_id = ? ORDER BY created_at DESC;",
            (snapshot_id,),
        ).fetchall()
    except sqlite3.Error as e:
        logger.error(f"Failed to get predictions for snapshot {snapshot_id}: {e}")
        raise


def get_evaluations(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """
    Joins predictions to their realized outcomes for the same contract and
    session date. Used by the evaluation dashboard.
    """
    query = """
    SELECT
        p.prediction_id,
        p.contract_id,
        p.forecast_cutoff AS forecast_cutoff,
        p.opening_bias    AS predicted_bias,
        p.probabilities   AS probabilities,
        o.session_date    AS session_date,
        o.rth_high, o.rth_low, o.rth_close,
        o.first_15_minute_high, o.first_15_minute_low,
        o.raw_outcomes    AS raw_outcomes
    FROM predictions p
    INNER JOIN outcomes o
        ON o.contract_id = p.contract_id
       AND date(o.session_date) = date(p.forecast_cutoff)
    ORDER BY p.forecast_cutoff DESC;
    """
    try:
        return conn.execute(query).fetchall()
    except sqlite3.Error as e:
        logger.error(f"Failed to load evaluations: {e}")
        raise


# ==========================================
# 6. OUTCOMES TABLE HANDLERS
# ==========================================

def save_outcome(
    conn: sqlite3.Connection,
    contract_id: int,
    session_date: str,
    first_15_minute_high: Optional[float],
    first_15_minute_low: Optional[float],
    first_15_minute_close: Optional[float],
    first_30_minute_high: Optional[float],
    first_30_minute_low: Optional[float],
    first_30_minute_close: Optional[float],
    initial_balance_high: Optional[float],
    initial_balance_low: Optional[float],
    rth_high: Optional[float],
    rth_low: Optional[float],
    rth_close: Optional[float],
    raw_outcomes: Dict[str, Any],
) -> int:
    """
    Saves the realized market outcome recorded post-session.
    Idempotent on (contract_id, session_date): re-running updates in place.
    """
    query = """
    INSERT INTO outcomes (
        contract_id, session_date, first_15_minute_high, first_15_minute_low, first_15_minute_close,
        first_30_minute_high, first_30_minute_low, first_30_minute_close,
        initial_balance_high, initial_balance_low, rth_high, rth_low, rth_close, raw_outcomes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(contract_id, session_date) DO UPDATE SET
        first_15_minute_high=excluded.first_15_minute_high,
        first_15_minute_low=excluded.first_15_minute_low,
        first_15_minute_close=excluded.first_15_minute_close,
        first_30_minute_high=excluded.first_30_minute_high,
        first_30_minute_low=excluded.first_30_minute_low,
        first_30_minute_close=excluded.first_30_minute_close,
        initial_balance_high=excluded.initial_balance_high,
        initial_balance_low=excluded.initial_balance_low,
        rth_high=excluded.rth_high,
        rth_low=excluded.rth_low,
        rth_close=excluded.rth_close,
        raw_outcomes=excluded.raw_outcomes;
    """
    try:
        with conn:
            conn.execute(query, (
                contract_id, session_date, first_15_minute_high, first_15_minute_low, first_15_minute_close,
                first_30_minute_high, first_30_minute_low, first_30_minute_close,
                initial_balance_high, initial_balance_low, rth_high, rth_low, rth_close,
                json.dumps(raw_outcomes),
            ))
            row = conn.execute(
                "SELECT outcome_id FROM outcomes WHERE contract_id = ? AND session_date = ?;",
                (contract_id, session_date),
            ).fetchone()
            return int(row["outcome_id"])
    except sqlite3.Error as e:
        logger.error(f"Failed to save realized outcomes: {e}")
        raise


def get_outcome(conn: sqlite3.Connection, contract_id: int, session_date: str) -> Optional[sqlite3.Row]:
    """Retrieves a session's realized outcomes by contract and YYYY-MM-DD date."""
    try:
        return conn.execute(
            "SELECT * FROM outcomes WHERE contract_id = ? AND session_date = ?;",
            (contract_id, session_date),
        ).fetchone()
    except sqlite3.Error as e:
        logger.error(f"Failed to get outcome for contract {contract_id} on {session_date}: {e}")
        raise


# ==========================================
# 7. ANALOGUE MATCHES TABLE HANDLERS
# ==========================================

def save_analogue_matches(conn: sqlite3.Connection, prediction_id: int, matches: List[Dict[str, Any]]) -> None:
    """
    Saves analogue historical session matches to support forecasting transparency.
    Expects matches to contain 'match_date', 'similarity_score', and 'ranking'.
    Idempotent on (prediction_id, match_date).
    """
    query = """
    INSERT INTO analogue_matches (prediction_id, match_date, similarity_score, ranking)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(prediction_id, match_date) DO UPDATE SET
        similarity_score=excluded.similarity_score,
        ranking=excluded.ranking;
    """
    formatted_matches = [
        (prediction_id, str(m["match_date"]), float(m["similarity_score"]), int(m["ranking"]))
        for m in matches
    ]
    try:
        with conn:
            conn.executemany(query, formatted_matches)
    except sqlite3.Error as e:
        logger.error(f"Failed to bulk-save analogue matches: {e}")
        raise


def get_analogue_matches(conn: sqlite3.Connection, prediction_id: int) -> List[sqlite3.Row]:
    """Retrieves historical analogues sorted by closest similarity ranking."""
    try:
        return conn.execute(
            "SELECT * FROM analogue_matches WHERE prediction_id = ? ORDER BY ranking ASC;",
            (prediction_id,),
        ).fetchall()
    except sqlite3.Error as e:
        logger.error(f"Failed to retrieve matches for prediction {prediction_id}: {e}")
        raise
