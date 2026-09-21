-- 0003_day_partitioned_store.sql
-- Make the trading day the organising unit of the market-data store.
--
--   session_days : one row per (contract, interval, price_type, NY trading day).
--                  This is the authoritative ledger of *which days we hold*. The
--                  collector queries it before contacting IB so that a day that is
--                  already stored is never downloaded again. A day with no data at
--                  the source is recorded too (status 'EMPTY'), so "we already
--                  asked and there was nothing" is remembered rather than retried
--                  forever.
--
--   bars         : rebuilt so that trading_day is NOT NULL, is the leading part of
--                  the primary key after the contract/interval, and is a foreign
--                  key into session_days. A bar therefore cannot exist outside a
--                  registered day, and deleting a day removes its bars atomically.
--                  session_scope leaves the primary key (it is derived from the
--                  timestamp, so it was never an identity column) which makes one
--                  timestamp map to exactly one bar.
--
-- database/migrations.py runs two Python hooks around this file: one before, to
-- fill any NULL trading_day left over from pre-0002 rows, and one after, to derive
-- each ledger row's status from real expected-bar counts.

CREATE TABLE session_days (
    contract_id        INTEGER NOT NULL,
    interval           TEXT NOT NULL,
    price_type         TEXT NOT NULL,
    trading_day        TEXT NOT NULL,              -- 'YYYY-MM-DD' NY session date
    status             TEXT NOT NULL,              -- COMPLETE | PARTIAL | EMPTY
    bar_count          INTEGER NOT NULL DEFAULT 0,
    rth_bar_count      INTEGER NOT NULL DEFAULT 0,
    open_bar_count     INTEGER NOT NULL DEFAULT 0, -- bars still flagged is_completed = 0
    expected_bar_count INTEGER,                    -- rough yardstick used to judge completeness
    first_bar_utc      TEXT,
    last_bar_utc       TEXT,
    source             TEXT NOT NULL DEFAULT 'IBKR',
    fetched_at         TEXT NOT NULL,              -- when this day was last written
    PRIMARY KEY (contract_id, interval, price_type, trading_day),
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE,
    CHECK (status IN ('COMPLETE', 'PARTIAL', 'EMPTY')),
    CHECK (trading_day = date(trading_day))
);

CREATE INDEX idx_session_days_day ON session_days (trading_day);
CREATE INDEX idx_session_days_status ON session_days (contract_id, interval, status);

-- Parent rows must exist before the child bars are copied (FKs are immediate).
-- status is provisional here; the post-migration hook re-derives it.
INSERT INTO session_days (
    contract_id, interval, price_type, trading_day, status,
    bar_count, rth_bar_count, open_bar_count,
    first_bar_utc, last_bar_utc, source, fetched_at
)
SELECT
    contract_id, interval, price_type, trading_day, 'PARTIAL',
    COUNT(*),
    SUM(CASE WHEN session_scope = 'RTH' THEN 1 ELSE 0 END),
    SUM(CASE WHEN is_completed = 0 THEN 1 ELSE 0 END),
    MIN(timestamp_utc), MAX(timestamp_utc),
    COALESCE(MIN(source), 'IBKR'), datetime('now')
FROM bars
WHERE trading_day IS NOT NULL
GROUP BY contract_id, interval, price_type, trading_day;

CREATE TABLE bars_new (
    contract_id   INTEGER NOT NULL,
    interval      TEXT NOT NULL,
    price_type    TEXT NOT NULL,
    trading_day   TEXT NOT NULL,                   -- partition key: NY session date
    timestamp_utc TEXT NOT NULL,
    session_scope TEXT NOT NULL,                   -- derived from timestamp: RTH | ETH
    open          REAL NOT NULL,
    high          REAL NOT NULL,
    low           REAL NOT NULL,
    close         REAL NOT NULL,
    volume        INTEGER NOT NULL,
    wap           REAL,
    bar_count     INTEGER,
    source        TEXT NOT NULL DEFAULT 'IBKR',
    is_completed  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (contract_id, interval, price_type, trading_day, timestamp_utc),
    FOREIGN KEY (contract_id, interval, price_type, trading_day)
        REFERENCES session_days (contract_id, interval, price_type, trading_day)
        ON DELETE CASCADE ON UPDATE CASCADE,
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE,
    CHECK (trading_day = date(trading_day)),
    CHECK (session_scope IN ('RTH', 'ETH')),
    CHECK (is_completed IN (0, 1))
);

-- OR IGNORE: pre-0003 rows could hold the same timestamp twice under two
-- session_scope values; the first classification wins.
INSERT OR IGNORE INTO bars_new (
    contract_id, interval, price_type, trading_day, timestamp_utc, session_scope,
    open, high, low, close, volume, wap, bar_count, source, is_completed
)
SELECT
    contract_id, interval, price_type, trading_day, timestamp_utc,
    CASE WHEN session_scope = 'RTH' THEN 'RTH' ELSE 'ETH' END,
    open, high, low, close, volume, wap, bar_count, source,
    CASE WHEN is_completed = 0 THEN 0 ELSE 1 END
FROM bars
WHERE trading_day IS NOT NULL;

DROP TABLE bars;
ALTER TABLE bars_new RENAME TO bars;

-- The primary key already serves every (contract, interval, price_type, day) scan;
-- this index covers cross-day queries that filter on the raw UTC timestamp.
CREATE INDEX idx_bars_timestamp ON bars (timestamp_utc);

-- collection_runs becomes a per-day audit log rather than a per-window one.
ALTER TABLE collection_runs ADD COLUMN trading_day TEXT;
ALTER TABLE collection_runs ADD COLUMN interval TEXT;
ALTER TABLE collection_runs ADD COLUMN bars_written INTEGER;

CREATE INDEX idx_collection_runs_day ON collection_runs (contract_id, trading_day);
