# database/queries.py
"""
PostgreSQL / TimescaleDB query interface for the NQ Trading Pipeline.
Provides robust functions to insert, update, and retrieve records across
all 7 core database tables, with strict typing, parameterized inputs,
and JSON handling for document storage fields.
"""

import hashlib
import json
import re
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from database.connection import Database, Error, Row

logger = logging.getLogger(__name__)

# ==========================================
# 1. CONTRACTS TABLE HANDLERS
# ==========================================

def upsert_contract(
    conn: Database,
    contract_id: int,
    symbol: str,
    expiry: Optional[str],
    exchange: str,
    currency: str = "USD",
    tick_size: Optional[float] = None,
    multiplier: Optional[str] = None,
    sec_type: str = "FUT",
    contract_month: Optional[str] = None,
    local_symbol: Optional[str] = None,
    trading_class: Optional[str] = None,
    primary_exchange: Optional[str] = None,
    time_zone_id: Optional[str] = None,
    trading_hours: Optional[str] = None,
    liquid_hours: Optional[str] = None,
) -> None:
    """
    Inserts or updates a contract; each contract_id is one expiry of one symbol.

    The IB detail fields (contract month, hours, ...) are optional: a caller that
    does not know them leaves whatever an earlier IB resolution stored.
    """
    query = """
    INSERT INTO contracts (contract_id, symbol, expiry, sec_type, exchange, currency, tick_size,
                           multiplier, contract_month, local_symbol, trading_class,
                           primary_exchange, time_zone_id, trading_hours, liquid_hours, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
    ON CONFLICT(contract_id) DO UPDATE SET
        symbol=excluded.symbol,
        expiry=excluded.expiry,
        sec_type=excluded.sec_type,
        exchange=excluded.exchange,
        currency=excluded.currency,
        tick_size=excluded.tick_size,
        multiplier=excluded.multiplier,
        contract_month=COALESCE(excluded.contract_month, contracts.contract_month),
        local_symbol=COALESCE(excluded.local_symbol, contracts.local_symbol),
        trading_class=COALESCE(excluded.trading_class, contracts.trading_class),
        primary_exchange=COALESCE(excluded.primary_exchange, contracts.primary_exchange),
        time_zone_id=COALESCE(excluded.time_zone_id, contracts.time_zone_id),
        trading_hours=COALESCE(excluded.trading_hours, contracts.trading_hours),
        liquid_hours=COALESCE(excluded.liquid_hours, contracts.liquid_hours),
        updated_at=excluded.updated_at;
    """
    try:
        with conn:
            conn.execute(query, (
                contract_id, symbol, expiry, sec_type, exchange, currency, tick_size, multiplier,
                contract_month, local_symbol, trading_class, primary_exchange, time_zone_id,
                trading_hours, liquid_hours,
            ))
        logger.debug(f"Contract {contract_id} ({symbol}{expiry or ''}) upserted successfully.")
    except Error as e:
        logger.error(f"Failed to upsert contract {contract_id}: {e}")
        raise


def get_contract(conn: Database, contract_id: int) -> Optional[Row]:
    """Retrieves metadata of a specific contract by its Unique ID."""
    try:
        return conn.execute("SELECT * FROM contracts WHERE contract_id = %s;", (contract_id,)).fetchone()
    except Error as e:
        logger.error(f"Failed to fetch contract {contract_id}: {e}")
        raise


def get_contract_by_expiry(conn: Database, symbol: str, expiry: Optional[str]) -> Optional[Row]:
    """
    Retrieves a contract by trading symbol and expiry.

    Callers usually have the contract month ('202609'), while IB resolves and
    stores the full last-trade date ('20260918'), so an exact match is tried
    first and a prefix match second. With several matching expiries the nearest
    one wins, which is the front month a bare contract month refers to.

    ``expiry=None`` means an instrument that has no expiry at all — a cash index —
    and matches only the row stored with a NULL expiry.
    """
    try:
        if expiry is None:
            return conn.execute(
                "SELECT * FROM contracts WHERE symbol = %s AND expiry IS NULL;", (symbol,)
            ).fetchone()

        row = conn.execute(
            "SELECT * FROM contracts WHERE symbol = %s AND expiry = %s;", (symbol, expiry)
        ).fetchone()
        if row is not None:
            return row
        return conn.execute(
            "SELECT * FROM contracts WHERE symbol = %s AND expiry LIKE %s "
            "ORDER BY expiry ASC LIMIT 1;",
            (symbol, f"{expiry}%"),
        ).fetchone()
    except Error as e:
        logger.error(f"Failed to fetch contract by symbol={symbol}, expiry={expiry}: {e}")
        raise


def list_contracts(conn: Database, with_data_only: bool = False) -> List[Row]:
    """
    Lists contracts in the database. The collector records a future's whole
    contract chain to plan its rolls, so most of those rows hold no bars;
    ``with_data_only`` keeps just the contracts that have at least one stored bar.
    """
    where = (" WHERE EXISTS (SELECT 1 FROM session_days s "
             "WHERE s.contract_id = contracts.contract_id AND s.bar_count > 0)") if with_data_only else ""
    try:
        return conn.execute(f"SELECT * FROM contracts{where} ORDER BY symbol, expiry;").fetchall()
    except Error as e:
        logger.error(f"Failed to list contracts: {e}")
        raise


def list_future_chain(conn: Database, symbol: str) -> List[Row]:
    """Every stored contract of a futures symbol, nearest expiry first."""
    return conn.execute(
        "SELECT * FROM contracts WHERE symbol = %s AND sec_type = 'FUT' AND expiry IS NOT NULL "
        "ORDER BY expiry ASC;",
        (symbol,),
    ).fetchall()


# ------------------------------------------
# Active contract per symbol and trading day
# ------------------------------------------

def set_active_contracts(conn: Database, symbol: str, assignments: Dict[str, int], rule: str) -> int:
    """
    Records which contract stands for ``symbol`` on each trading day, as
    ``{'YYYY-MM-DD': contract_id}``. Re-assigning a day replaces its row, so a
    changed roll rule takes effect the next time the collector plans that day.
    """
    rows = [(symbol, _validate_day(day), int(cid), rule) for day, cid in sorted(assignments.items())]
    if not rows:
        return 0
    try:
        with conn:
            conn.executemany(
                "INSERT INTO active_contracts (symbol, trading_day, contract_id, rule, assigned_at) "
                "VALUES (%s, %s, %s, %s, now()) "
                "ON CONFLICT (symbol, trading_day) DO UPDATE SET "
                "    contract_id = excluded.contract_id, rule = excluded.rule, "
                "    assigned_at = excluded.assigned_at;",
                rows,
            )
    except Error as e:
        logger.error(f"Failed to record active contracts for {symbol}: {e}")
        raise
    return len(rows)


def get_active_contract(conn: Database, symbol: str, trading_day: str) -> Optional[Row]:
    """
    The contract that stood for ``symbol`` on ``trading_day`` (joined with its
    contract row), or None if the collector never assigned one.
    """
    return conn.execute(
        "SELECT a.trading_day, a.rule, c.* FROM active_contracts a "
        "JOIN contracts c ON c.contract_id = a.contract_id "
        "WHERE a.symbol = %s AND a.trading_day = %s;",
        (symbol, _validate_day(trading_day)),
    ).fetchone()


# ------------------------------------------
# Logical asset -> recorded instrument map
# ------------------------------------------

def register_asset_sources(conn: Database, sources: List[Dict[str, Any]]) -> None:
    """
    Stores the current source definition of each logical asset. A definition is
    identified by the hash of its fields: an unchanged one only has its
    ``last_registered_at`` refreshed, a changed one becomes a new row, so every
    definition a feature may have been built under stays on record.
    """
    fields = ("symbol", "sec_type", "exchange", "what_to_show", "value_kind", "value_unit",
              "bps_per_unit", "max_age_minutes", "roll_rule", "is_proxy", "optional",
              "collected", "description", "notes")
    rows = []
    for src in sources:
        payload = {f: src.get(f) for f in fields}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
        rows.append({"asset": src["asset"], "config_hash": digest, **payload})

    cols = ("asset", "config_hash") + fields
    try:
        with conn:
            conn.executemany(
                f"INSERT INTO asset_sources ({', '.join(cols)}) "
                f"VALUES ({', '.join(f'%({c})s' for c in cols)}) "
                "ON CONFLICT (asset, config_hash) DO UPDATE SET last_registered_at = now();",
                rows,
            )
    except Error as e:
        logger.error(f"Failed to register asset sources: {e}")
        raise


def get_asset_sources(conn: Database) -> Dict[str, Row]:
    """``{asset: row}`` for the definition each asset is currently collected under."""
    rows = conn.execute("SELECT * FROM current_asset_sources ORDER BY asset;").fetchall()
    return {r["asset"]: r for r in rows}


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
VALUES ({", ".join(f"%({c})s" for c in _BAR_COLUMNS)});
"""

_SESSION_DAY_KEY = "contract_id = %s AND interval = %s AND price_type = %s AND trading_day = %s"

# The same key for the bars hypertable, plus a timestamp window that holds every bar
# of the day. trading_day alone would make TimescaleDB visit every chunk; the window
# lets it exclude all but the one or two chunks that can contain the day.
_BARS_DAY_KEY = _SESSION_DAY_KEY + " AND timestamp_utc >= %s AND timestamp_utc < %s"


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
    window_start, window_end = _day_window(trading_day)
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

        ts_day = _utc_date(b["timestamp_utc"])
        if not (window_start <= ts_day < window_end):
            raise ValueError(
                f"Bar at {b['timestamp_utc']!r} cannot belong to trading day {trading_day}; "
                f"its timestamp is outside that session."
            )

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


def _day_window(first_day: str, last_day: Optional[str] = None):
    """
    UTC bounds that contain every bar of the NY sessions ``first_day``..``last_day``.

    A session opens 18:00 ET the prior evening (22:00/23:00 UTC) and ends by 17:00 ET
    (21:00/22:00 UTC), so [first_day - 1 day, last_day + 1 day) UTC always covers it.
    """
    start = date.fromisoformat(first_day) - timedelta(days=1)
    end = date.fromisoformat(last_day or first_day) + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _utc_date(timestamp) -> str:
    """'YYYY-MM-DD' UTC date of a timestamp; naive values are UTC, as everywhere else."""
    dt = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(str(timestamp))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date().isoformat()


def _bars_day_params(contract_id, interval, price_type, trading_day):
    return (contract_id, interval, price_type, trading_day) + _day_window(trading_day)


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
        f"FROM bars WHERE {_BARS_DAY_KEY};",
        _bars_day_params(contract_id, interval, price_type, trading_day),
    ).fetchone()
    return {
        "bar_count": int(row[0]),
        "rth_bar_count": int(row[1]),
        "open_bar_count": int(row[2]),
        "first_bar_utc": row[3],
        "last_bar_utc": row[4],
    }


def save_trading_day(
    conn: Database,
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
                "VALUES (%s, %s, %s, %s, 'EMPTY', %s, %s, now()) "
                "ON CONFLICT(contract_id, interval, price_type, trading_day) DO UPDATE SET "
                "    expected_bar_count = excluded.expected_bar_count, "
                "    source = excluded.source, "
                "    fetched_at = excluded.fetched_at;",
                key + (expected_bar_count, source),
            )
            conn.execute(f"DELETE FROM bars WHERE {_BARS_DAY_KEY};", _bars_day_params(*key))
            if rows:
                conn.executemany(_BARS_INSERT, rows)

            stats = _day_stats(conn, *key)
            resolved = status or derive_day_status(
                stats["bar_count"], expected_bar_count, stats["open_bar_count"]
            )
            if resolved not in DAY_STATUSES:
                raise ValueError(f"status must be one of {DAY_STATUSES}, got {resolved!r}")

            conn.execute(
                "UPDATE session_days SET status = %s, bar_count = %s, rth_bar_count = %s, "
                "    open_bar_count = %s, first_bar_utc = %s, last_bar_utc = %s "
                f"WHERE {_SESSION_DAY_KEY};",
                (resolved, stats["bar_count"], stats["rth_bar_count"], stats["open_bar_count"],
                 stats["first_bar_utc"], stats["last_bar_utc"]) + key,
            )
    except Error as e:
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
    conn: Database,
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
    conn: Database,
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
    except Error as e:
        logger.error(f"Failed to delete trading day {trading_day}: {e}")
        raise


def get_session_day(
    conn: Database,
    contract_id: int,
    trading_day: str,
    interval: str = "1m",
    price_type: str = "TRADES",
) -> Optional[Row]:
    """The ledger row for one stored day, or None if that day was never fetched."""
    return conn.execute(
        f"SELECT * FROM session_days WHERE {_SESSION_DAY_KEY};",
        (contract_id, interval, price_type, _validate_day(trading_day)),
    ).fetchone()


def get_stored_trading_days(
    conn: Database,
    contract_id: int,
    interval: str = "1m",
    price_type: str = "TRADES",
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, Row]:
    """
    The ledger for a contract as ``{trading_day: row}`` — the single query the
    collector runs before contacting IB to decide what it still needs.
    ``start`` / ``end`` are inclusive 'YYYY-MM-DD' bounds.
    """
    clauses = ["contract_id = %s", "interval = %s", "price_type = %s"]
    params: List[Any] = [contract_id, interval, price_type]
    if start is not None:
        clauses.append("trading_day >= %s")
        params.append(_validate_day(start))
    if end is not None:
        clauses.append("trading_day <= %s")
        params.append(_validate_day(end))

    try:
        rows = conn.execute(
            f"SELECT * FROM session_days WHERE {' AND '.join(clauses)} ORDER BY trading_day;",
            tuple(params),
        ).fetchall()
        return {r["trading_day"]: r for r in rows}
    except Error as e:
        logger.error(f"Failed to read the session_days ledger for contract {contract_id}: {e}")
        raise


def list_trading_days(
    conn: Database,
    contract_id: int,
    interval: str = "1m",
    price_type: str = "TRADES",
    statuses: Optional[List[str]] = None,
    limit: int = 100,
) -> List[str]:
    """Stored trading days, newest first. Defaults to days that actually hold bars."""
    clauses = ["contract_id = %s", "interval = %s", "price_type = %s", "bar_count > 0"]
    params: List[Any] = [contract_id, interval, price_type]
    if statuses:
        clauses.append(f"status IN ({', '.join(['%s'] * len(statuses))})")
        params.extend(statuses)
    params.append(limit)

    rows = conn.execute(
        f"SELECT trading_day FROM session_days WHERE {' AND '.join(clauses)} "
        f"ORDER BY trading_day DESC LIMIT %s;",
        tuple(params),
    ).fetchall()
    return [r["trading_day"] for r in rows]


def get_day_bars(
    conn: Database,
    contract_id: int,
    trading_day: str,
    interval: str = "1m",
    price_type: str = "TRADES",
    session_scope: Optional[str] = None,
) -> List[Row]:
    """Every bar of one trading day, in time order. Hits the bars primary key directly."""
    clauses = [_BARS_DAY_KEY]
    params: List[Any] = list(_bars_day_params(contract_id, interval, price_type, _validate_day(trading_day)))
    if session_scope is not None:
        clauses.append("session_scope = %s")
        params.append(session_scope)

    return conn.execute(
        f"SELECT * FROM bars WHERE {' AND '.join(clauses)} ORDER BY timestamp_utc ASC;",
        tuple(params),
    ).fetchall()


def get_bars(
    conn: Database,
    contract_id: int,
    interval: str,
    start_utc: Optional[str] = None,
    end_utc: Optional[str] = None,
    price_type: str = "TRADES",
    session_scope: Optional[str] = None,
    trading_day: Optional[str] = None,
    start_day: Optional[str] = None,
    end_day: Optional[str] = None,
) -> List[Row]:
    """
    Retrieves historical candles across days.

    Filters:
      - start_utc / end_utc   : inclusive UTC timestamp bounds
      - session_scope         : 'RTH' or 'ETH'
      - trading_day           : a single NY session date 'YYYY-MM-DD'
      - start_day / end_day   : inclusive NY session-date bounds

    Prefer :func:`get_day_bars` when you want exactly one session.
    """
    clauses = ["contract_id = %s", "interval = %s", "price_type = %s"]
    params: List[Any] = [contract_id, interval, price_type]

    if trading_day is not None:
        clauses.append("trading_day = %s")
        params.append(_validate_day(trading_day))
        start_day = end_day = trading_day
    if start_day is not None:
        clauses.append("trading_day >= %s")
        params.append(_validate_day(start_day))
        # Also bound the time column, so TimescaleDB can skip chunks before the window.
        clauses.append("timestamp_utc >= %s")
        params.append(_day_window(start_day)[0])
    if end_day is not None:
        clauses.append("trading_day <= %s")
        params.append(_validate_day(end_day))
        clauses.append("timestamp_utc < %s")
        params.append(_day_window(end_day)[1])
    if start_utc is not None:
        clauses.append("timestamp_utc >= %s")
        params.append(start_utc)
    if end_utc is not None:
        clauses.append("timestamp_utc <= %s")
        params.append(end_utc)
    if session_scope is not None:
        clauses.append("session_scope = %s")
        params.append(session_scope)

    query = f"SELECT * FROM bars WHERE {' AND '.join(clauses)} ORDER BY timestamp_utc ASC;"
    try:
        return conn.execute(query, tuple(params)).fetchall()
    except Error as e:
        logger.error(f"Failed to retrieve bars: {e}")
        raise


def get_daily_rth_closes(
    conn: Database,
    contract_id: int,
    interval: str = "1m",
    price_type: str = "TRADES",
    end_day: Optional[str] = None,
) -> Dict[str, float]:
    """
    ``{trading_day: close of that day's last RTH bar}``, oldest first, for every
    stored day up to ``end_day``.

    RTH is classified from the timestamp exactly as
    ``features.session_windows.enrich_candle_timezones`` does (Mon-Fri,
    09:30-16:00 New York), so this equals the per-day closes that
    ``calculate_pre_open_snapshot`` would derive from the full bar history —
    without loading that history. An RTH bar never rolls into the next session,
    so its trading day is its New York calendar date.
    """
    clauses = ["contract_id = %s", "interval = %s", "price_type = %s"]
    params: List[Any] = [contract_id, interval, price_type]
    if end_day is not None:
        clauses.append("trading_day <= %s")
        params.append(_validate_day(end_day))
        clauses.append("timestamp_utc < %s")
        params.append(_day_window(end_day)[1])

    rows = conn.execute(
        "SELECT DISTINCT ON (ny::date) ny::date AS day, close "
        "FROM (SELECT timestamp_utc, close, timestamp_utc AT TIME ZONE 'America/New_York' AS ny "
        f"      FROM bars WHERE {' AND '.join(clauses)}) b "
        "WHERE extract(isodow FROM ny) < 6 "
        "  AND ny::time >= '09:30' AND ny::time < '16:00' "
        "ORDER BY ny::date, timestamp_utc DESC;",
        tuple(params),
    ).fetchall()
    return {r["day"]: float(r["close"]) for r in rows}


_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


def get_last_bar_at_or_before(
    conn: Database,
    contract_id: int,
    as_of_utc: str,
    interval: str = "1m",
    price_type: str = "TRADES",
    lookback_days: int = 7,
) -> Optional[Row]:
    """
    The last bar of a contract that had fully *closed* by ``as_of_utc``, or None.

    Bars are stamped with their open time, so a 1-minute bar stamped 09:27 is the
    09:28 close and is eligible at an as-of of 09:28 but not 09:27. Only stored
    bars are considered - nothing is filled forward - so the returned row's
    ``timestamp_utc`` plus one interval is the true time of the observation, and
    the caller measures its age against a freshness rule from that.

    ``lookback_days`` bounds the scan (and lets TimescaleDB skip other chunks);
    it spans a long weekend.
    """
    minutes = _INTERVAL_MINUTES.get(interval)
    if minutes is None:
        raise ValueError(f"Unknown interval {interval!r}")
    return conn.execute(
        "SELECT *, timestamp_utc + make_interval(mins => %s::int) AS close_time_utc FROM bars "
        "WHERE contract_id = %s AND interval = %s AND price_type = %s "
        "  AND timestamp_utc <= %s::timestamptz - make_interval(mins => %s::int) "
        "  AND timestamp_utc >= %s::timestamptz - make_interval(days => %s::int) "
        "ORDER BY timestamp_utc DESC LIMIT 1;",
        (minutes, contract_id, interval, price_type, as_of_utc, minutes, as_of_utc, lookback_days),
    ).fetchone()


_LEDGER_UPSERT_FROM_BARS = """
INSERT INTO session_days (
    contract_id, interval, price_type, trading_day, status, bar_count, rth_bar_count,
    open_bar_count, expected_bar_count, first_bar_utc, last_bar_utc, source, fetched_at
)
SELECT
    b.contract_id, b.interval, b.price_type, b.trading_day,
    CASE
        WHEN SUM(CASE WHEN b.is_completed = 0 THEN 1 ELSE 0 END) > 0 THEN 'PARTIAL'
        WHEN COUNT(*) < MAX(COALESCE(s.expected_bar_count, %(expected)s::integer, 0))
                        * %(ratio)s::double precision THEN 'PARTIAL'
        ELSE 'COMPLETE'
    END,
    COUNT(*),
    SUM(CASE WHEN b.session_scope = 'RTH' THEN 1 ELSE 0 END),
    SUM(CASE WHEN b.is_completed = 0 THEN 1 ELSE 0 END),
    MAX(COALESCE(s.expected_bar_count, %(expected)s::integer)),
    MIN(b.timestamp_utc), MAX(b.timestamp_utc),
    COALESCE(MIN(b.source), 'IBKR'),
    COALESCE(MAX(s.fetched_at), now())
FROM bars b
LEFT JOIN session_days s
       ON s.contract_id = b.contract_id AND s.interval = b.interval
      AND s.price_type = b.price_type AND s.trading_day = b.trading_day
WHERE b.interval = %(interval)s AND b.price_type = %(price_type)s
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


def rebuild_session_days(conn: Database) -> int:
    """
    Recomputes the ``session_days`` ledger from the bars actually stored.

    Idempotent repair path, used by ``python -m database.migrate_from_sqlite``
    and ``python -m database.backfill``. Days that hold no bars are marked 'EMPTY'
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
                    "ratio": DAY_COMPLETE_RATIO,
                })
            conn.execute(_LEDGER_MARK_EMPTY)
            indexed = conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM bars "
                "GROUP BY contract_id, interval, price_type, trading_day) AS days;"
            ).fetchone()[0]
    except Error as e:
        logger.error(f"Failed to rebuild the session_days ledger: {e}")
        raise
    return int(indexed)


# ==========================================
# 3. COLLECTION RUNS TABLE HANDLERS
# ==========================================

def create_collection_run(
    conn: Database,
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
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
    RETURNING run_id;
    """
    missing_json = json.dumps(missing_intervals) if missing_intervals is not None else None
    try:
        with conn:
            cursor = conn.execute(query, (
                contract_id, trading_day, interval, requested_start_utc, requested_end_utc,
                download_status, errors, missing_json,
            ))
            return int(cursor.fetchone()[0])
    except Error as e:
        logger.error(f"Failed to log collection run start: {e}")
        raise


def update_collection_run(
    conn: Database,
    run_id: int,
    download_status: str,
    errors: Optional[str] = None,
    missing_intervals: Optional[List[str]] = None,
    bars_written: Optional[int] = None,
) -> None:
    """Updates status and logs gaps or error tracebacks upon completion/failure."""
    query = """
    UPDATE collection_runs
    SET download_status = %s, errors = %s, missing_intervals = %s, bars_written = %s,
        last_successful_update = now()
    WHERE run_id = %s;
    """
    missing_json = json.dumps(missing_intervals) if missing_intervals is not None else None
    try:
        with conn:
            conn.execute(query, (download_status, errors, missing_json, bars_written, run_id))
    except Error as e:
        logger.error(f"Failed to update collection run {run_id}: {e}")
        raise


def get_collection_runs_for_day(
    conn: Database, contract_id: int, trading_day: str
) -> List[Row]:
    """Every download attempt recorded for one trading day, newest first."""
    return conn.execute(
        "SELECT * FROM collection_runs WHERE contract_id = %s AND trading_day = %s "
        "ORDER BY run_id DESC;",
        (contract_id, _validate_day(trading_day)),
    ).fetchall()


def get_latest_collection_run(conn: Database, contract_id: int) -> Optional[Row]:
    """Retrieves the last recorded run for a contract."""
    try:
        return conn.execute(
            "SELECT * FROM collection_runs WHERE contract_id = %s ORDER BY run_id DESC LIMIT 1;",
            (contract_id,),
        ).fetchone()
    except Error as e:
        logger.error(f"Failed to fetch latest run for contract {contract_id}: {e}")
        raise


# ==========================================
# 4. FEATURE SNAPSHOTS TABLE HANDLERS
# ==========================================

def save_feature_snapshot(
    conn: Database,
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
    vix_pre_open: Optional[float] = None,
    vix_change: Optional[float] = None,
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
        historical_volatility, vwap, raw_features, feature_version, data_quality_status,
        vix_pre_open, vix_change
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        data_quality_status=excluded.data_quality_status,
        vix_pre_open=excluded.vix_pre_open,
        vix_change=excluded.vix_change
    RETURNING snapshot_id;
    """
    try:
        with conn:
            row = conn.execute(query, (
                contract_id, timestamp_utc, previous_rth_high, previous_rth_low, previous_rth_close,
                overnight_high, overnight_low, overnight_range, gap, pre_open_direction,
                historical_volatility, vwap, json.dumps(raw_features), feature_version, data_quality_status,
                vix_pre_open, vix_change,
            )).fetchone()
            return int(row["snapshot_id"])
    except Error as e:
        logger.error(f"Failed to save feature snapshot: {e}")
        raise


def get_feature_snapshot(conn: Database, snapshot_id: int) -> Optional[Row]:
    """Retrieves a feature snapshot by ID."""
    try:
        return conn.execute(
            "SELECT * FROM feature_snapshots WHERE snapshot_id = %s;", (snapshot_id,)
        ).fetchone()
    except Error as e:
        logger.error(f"Failed to get snapshot {snapshot_id}: {e}")
        raise


def get_latest_feature_snapshot(conn: Database, contract_id: int) -> Optional[Row]:
    """Retrieves the newest feature snapshot generated for a contract."""
    try:
        return conn.execute(
            "SELECT * FROM feature_snapshots WHERE contract_id = %s "
            "ORDER BY timestamp_utc DESC, snapshot_id DESC LIMIT 1;",
            (contract_id,),
        ).fetchone()
    except Error as e:
        logger.error(f"Failed to get latest snapshot for contract {contract_id}: {e}")
        raise


# ==========================================
# 5. PREDICTIONS TABLE HANDLERS
# ==========================================

def save_prediction(
    conn: Database,
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
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING prediction_id;
    """
    try:
        with conn:
            cursor = conn.execute(query, (
                contract_id, forecast_cutoff, snapshot_id, model_version, prompt_version,
                opening_bias, json.dumps(scenarios), json.dumps(probabilities), raw_response, created_at,
            ))
            return int(cursor.fetchone()[0])
    except Error as e:
        logger.error(f"Failed to save prediction: {e}")
        raise


def get_prediction(conn: Database, prediction_id: int) -> Optional[Row]:
    """Retrieves an LLM prediction by its ID."""
    try:
        return conn.execute(
            "SELECT * FROM predictions WHERE prediction_id = %s;", (prediction_id,)
        ).fetchone()
    except Error as e:
        logger.error(f"Failed to get prediction {prediction_id}: {e}")
        raise


def get_predictions_by_snapshot(conn: Database, snapshot_id: int) -> List[Row]:
    """Retrieves model predictions associated with a specific feature snapshot."""
    try:
        return conn.execute(
            "SELECT * FROM predictions WHERE snapshot_id = %s ORDER BY created_at DESC;",
            (snapshot_id,),
        ).fetchall()
    except Error as e:
        logger.error(f"Failed to get predictions for snapshot {snapshot_id}: {e}")
        raise


def get_evaluations(conn: Database) -> List[Row]:
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
       AND o.session_date = (p.forecast_cutoff AT TIME ZONE 'America/New_York')::date
    ORDER BY p.forecast_cutoff DESC;
    """
    try:
        return conn.execute(query).fetchall()
    except Error as e:
        logger.error(f"Failed to load evaluations: {e}")
        raise


# ==========================================
# 6. OUTCOMES TABLE HANDLERS
# ==========================================

def save_outcome(
    conn: Database,
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
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        raw_outcomes=excluded.raw_outcomes
    RETURNING outcome_id;
    """
    try:
        with conn:
            row = conn.execute(query, (
                contract_id, session_date, first_15_minute_high, first_15_minute_low, first_15_minute_close,
                first_30_minute_high, first_30_minute_low, first_30_minute_close,
                initial_balance_high, initial_balance_low, rth_high, rth_low, rth_close,
                json.dumps(raw_outcomes),
            )).fetchone()
            return int(row["outcome_id"])
    except Error as e:
        logger.error(f"Failed to save realized outcomes: {e}")
        raise


def get_outcome(conn: Database, contract_id: int, session_date: str) -> Optional[Row]:
    """Retrieves a session's realized outcomes by contract and YYYY-MM-DD date."""
    try:
        return conn.execute(
            "SELECT * FROM outcomes WHERE contract_id = %s AND session_date = %s;",
            (contract_id, session_date),
        ).fetchone()
    except Error as e:
        logger.error(f"Failed to get outcome for contract {contract_id} on {session_date}: {e}")
        raise


# ==========================================
# 7. ANALOGUE MATCHES TABLE HANDLERS
# ==========================================

def save_analogue_matches(conn: Database, prediction_id: int, matches: List[Dict[str, Any]]) -> None:
    """
    Saves analogue historical session matches to support forecasting transparency.
    Expects matches to contain 'match_date', 'similarity_score', and 'ranking'.
    Idempotent on (prediction_id, match_date).
    """
    query = """
    INSERT INTO analogue_matches (prediction_id, match_date, similarity_score, ranking)
    VALUES (%s, %s, %s, %s)
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
    except Error as e:
        logger.error(f"Failed to bulk-save analogue matches: {e}")
        raise


def get_analogue_matches(conn: Database, prediction_id: int) -> List[Row]:
    """Retrieves historical analogues sorted by closest similarity ranking."""
    try:
        return conn.execute(
            "SELECT * FROM analogue_matches WHERE prediction_id = %s ORDER BY ranking ASC;",
            (prediction_id,),
        ).fetchall()
    except Error as e:
        logger.error(f"Failed to retrieve matches for prediction {prediction_id}: {e}")
        raise
