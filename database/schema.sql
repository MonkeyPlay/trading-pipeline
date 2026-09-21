-- database/schema.sql
-- GENERATED FILE — do not edit. Regenerate with:
--     python -m database.migrations --snapshot
-- The authoritative schema is the ordered set of files in database/migrations/.
-- Snapshot of schema version 0003.

CREATE TABLE analogue_matches (
    match_id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL,
    match_date TEXT NOT NULL,
    similarity_score REAL NOT NULL,
    ranking INTEGER NOT NULL,
    UNIQUE (prediction_id, match_date),
    FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id) ON DELETE CASCADE
);

CREATE TABLE "bars" (
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

CREATE TABLE collection_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    requested_start_utc TEXT NOT NULL,
    requested_end_utc TEXT NOT NULL,
    download_status TEXT NOT NULL,
    errors TEXT,
    missing_intervals TEXT,
    last_successful_update TEXT NOT NULL, trading_day TEXT, interval TEXT, bars_written INTEGER,
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
);

CREATE TABLE contracts (
    contract_id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    expiry TEXT,
    sec_type TEXT NOT NULL DEFAULT 'FUT',
    exchange TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    tick_size REAL,
    multiplier TEXT
);

CREATE TABLE feature_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    timestamp_utc TEXT NOT NULL,
    previous_rth_high REAL,
    previous_rth_low REAL,
    previous_rth_close REAL,
    overnight_high REAL,
    overnight_low REAL,
    overnight_range REAL,
    gap REAL,
    pre_open_direction TEXT,
    historical_volatility REAL,
    vwap REAL,
    raw_features JSON,
    feature_version TEXT NOT NULL,
    data_quality_status TEXT NOT NULL,
    UNIQUE (contract_id, timestamp_utc, feature_version),
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
);

CREATE TABLE outcomes (
    outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    session_date TEXT NOT NULL,
    first_15_minute_high REAL,
    first_15_minute_low REAL,
    first_15_minute_close REAL,
    first_30_minute_high REAL,
    first_30_minute_low REAL,
    first_30_minute_close REAL,
    initial_balance_high REAL,
    initial_balance_low REAL,
    rth_high REAL,
    rth_low REAL,
    rth_close REAL,
    raw_outcomes JSON,
    UNIQUE (contract_id, session_date),
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
);

CREATE TABLE predictions (
    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    forecast_cutoff TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    opening_bias TEXT,
    scenarios JSON,
    probabilities JSON,
    raw_response TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE,
    FOREIGN KEY (snapshot_id) REFERENCES feature_snapshots(snapshot_id)
);

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

CREATE INDEX idx_bars_timestamp ON bars (timestamp_utc);

CREATE INDEX idx_collection_runs_day ON collection_runs (contract_id, trading_day);

CREATE INDEX idx_features_timestamp ON feature_snapshots (timestamp_utc);

CREATE INDEX idx_session_days_day ON session_days (trading_day);

CREATE INDEX idx_session_days_status ON session_days (contract_id, interval, status);

