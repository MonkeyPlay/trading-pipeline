# database/connection.py
"""
PostgreSQL / TimescaleDB connection management for the NQ Trading Pipeline.

Schema is managed exclusively through forward-only migrations
(``database/migrations/``); see ``database/migrations.py``. An existing database
is upgraded in place and never regenerated - and only by an explicit deployment
step (``scripts/deploy.sh``, or ``python -m database.migrations --db URL`` from the
revision being deployed). Routine processes - the dashboard, the collector, the
CLIs - only check that the database's schema is the one their code expects
(``init_database``) and stop when it is not: creating a migration file in a
checkout can never change a database by itself.

The rest of the codebase was written against ``sqlite3`` and keeps that shape:
``conn.execute(...)`` returns a cursor, ``with conn:`` is one transaction, and
rows answer to both ``row[0]`` and ``row["column"]``. ``Database`` provides
exactly that on top of psycopg 3, so query code only differs in its SQL.

Dates and timestamps come back as the same strings the SQLite store held
('YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS' in UTC), and JSON columns as their text,
so callers that compare or parse those values are unchanged.
"""

import functools
import logging
import os
import threading
from typing import Any, Optional, Sequence

import psycopg
from psycopg.adapt import Loader
from psycopg.types.string import TextLoader

from database.migrations import apply_migrations, get_user_version, latest_version

logger = logging.getLogger(__name__)

_DEFAULT_DSN = "postgresql://trading:trading@localhost:5432/trading_pipeline"

Error = psycopg.Error


def default_dsn() -> str:
    """``DATABASE_URL`` if set, else the local development default."""
    return os.getenv("DATABASE_URL", _DEFAULT_DSN)


class Row(tuple):
    """A result row readable by position or by column name, like ``sqlite3.Row``."""

    __slots__ = ()
    _names: Sequence[str] = ()

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._names.index(key))
            except ValueError:
                raise IndexError(f"No column named {key!r}") from None
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


@functools.lru_cache(maxsize=256)
def _row_class(names):
    return type("Row", (Row,), {"__slots__": (), "_names": names})


def _row_factory(cursor):
    if cursor.description is None:
        return tuple
    cls = _row_class(tuple(c.name for c in cursor.description))
    return lambda values: tuple.__new__(cls, values)


class _UtcTimestampLoader(Loader):
    """TIMESTAMPTZ -> 'YYYY-MM-DD HH:MM:SS' (the session time zone is pinned to UTC)."""

    def load(self, data):
        text = bytes(data).decode()
        return text[:-3] if text.endswith("+00") else text


def _configure_adapters(conn: psycopg.Connection) -> None:
    conn.adapters.register_loader("date", TextLoader)
    conn.adapters.register_loader("timestamptz", _UtcTimestampLoader)
    conn.adapters.register_loader("timestamp", TextLoader)
    conn.adapters.register_loader("json", TextLoader)
    conn.adapters.register_loader("jsonb", TextLoader)


class Database:
    """
    A psycopg connection with the ``sqlite3.Connection`` surface this project uses.

    Statements outside ``with conn:`` autocommit. ``with conn:`` opens a
    transaction that commits on success and rolls back on an exception; nested
    blocks become savepoints.

    The dashboard shares one connection across NiceGUI's worker threads, so a
    re-entrant lock is held for every statement and for the whole of a
    transaction block — one thread's transaction never interleaves another's.
    """

    def __init__(self, raw: psycopg.Connection):
        self.raw = raw
        self._lock = threading.RLock()
        self._tx = threading.local()

    # -- statements ---------------------------------------------------------

    def execute(self, query: str, params: Optional[Any] = None) -> psycopg.Cursor:
        with self._lock:
            return self.raw.execute(query, params)

    def executemany(self, query: str, params_seq) -> psycopg.Cursor:
        with self._lock:
            cur = self.raw.cursor()
            cur.executemany(query, list(params_seq))
            return cur

    # -- transactions -------------------------------------------------------

    def __enter__(self):
        self._lock.acquire()
        try:
            stack = getattr(self._tx, "stack", None)
            if stack is None:
                stack = self._tx.stack = []
            tx = self.raw.transaction()
            tx.__enter__()
            stack.append(tx)
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._tx.stack.pop().__exit__(exc_type, exc, tb)
        finally:
            self._lock.release()

    def close(self) -> None:
        self.raw.close()

    @property
    def closed(self) -> bool:
        return self.raw.closed


def get_db_connection(dsn: Optional[str] = None) -> Database:
    """
    Opens an autocommit connection whose session time zone is UTC, so naive
    timestamp strings are read as UTC — the same convention the SQLite store used.
    """
    raw = psycopg.connect(dsn or default_dsn(), autocommit=True, row_factory=_row_factory,
                          options="-c timezone=UTC")
    _configure_adapters(raw)
    return Database(raw)


class SchemaMismatch(RuntimeError):
    """The database's schema version is not the one this code's migrations end at: deploy first."""


def check_schema(conn, dsn: Optional[str] = None) -> int:
    """The database's schema version when it is the one this checkout's migrations end at; raises SchemaMismatch
    otherwise - behind (a migration not deployed) or ahead (this code is older than the database)."""
    db, code = get_user_version(conn), latest_version()
    if db != code:
        where = describe_dsn(dsn)
        raise SchemaMismatch(
            f"The database at {where} is at schema v{db:04d}; this code expects v{code:04d}. "
            + ("Deploy the migrations explicitly: scripts/deploy.sh <revision> (or python -m database.migrations "
               "--db <url> from the revision to run)." if db < code else
               "This checkout is older than the database: run the deployed revision (scripts/deploy.sh).")
            + " Routine processes never migrate a database.")
    return db


def init_database(dsn: Optional[str] = None, schema_path=None, apply: bool = False) -> int:
    """
    Every process start: checks that the database's schema is the one this code expects (check_schema) and
    returns its version - it never migrates. ``apply=True`` applies pending migrations first: the deployment step,
    the test and development databases only. ``schema_path`` is accepted for backwards compatibility and ignored.
    """
    conn = get_db_connection(dsn)
    try:
        if apply:
            apply_migrations(conn)
        return check_schema(conn, dsn)
    finally:
        conn.close()


def reset_database(dsn: Optional[str] = None, upto: Optional[int] = None):
    """
    DEV ONLY. Drops every table in the current schema (including the migration
    ledger) and re-migrates from scratch - to migration ``upto`` when given (the test
    of one migration). Never call this against a database holding real collected data.
    """
    conn = get_db_connection(dsn)
    try:
        with conn:
            names = [
                r[0] for r in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema();"
                )
            ]
            for name in names:
                conn.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE;')
            # The journal records (0009) live in their own schema, a database before
            # migration 0008 still has the old forecast schema (0004), and functions
            # outlive the tables that use them.
            conn.execute("DROP SCHEMA IF EXISTS forecast CASCADE;")
            conn.execute("DROP SCHEMA IF EXISTS journal CASCADE;")
            conn.execute("DROP FUNCTION IF EXISTS reject_bar_receipt_mutation() CASCADE;")
        logger.info(f"Dropped {len(names)} table(s); schema version reset to 0.")
        if upto is not None:
            apply_migrations(conn, upto)
    finally:
        conn.close()
    if upto is None:
        init_database(dsn, apply=True)


def describe_dsn(dsn: Optional[str] = None) -> str:
    """'host:port/dbname' for display, without credentials."""
    info = psycopg.conninfo.conninfo_to_dict(dsn or default_dsn())
    host = info.get("host", "localhost")
    port = info.get("port", "5432")
    return f"{host}:{port}/{info.get('dbname', '')}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    init_database(apply=True)
    c = get_db_connection()
    try:
        print("schema version:", get_user_version(c))
        print(f"Success! Database at {describe_dsn()} is migrated.")
    finally:
        c.close()
