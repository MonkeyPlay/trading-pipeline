-- 0001_initial_schema.sql
-- The 7 core tables of the NQ Trading Pipeline. Uses IF NOT EXISTS so it is
-- safe to run against a database that predates the migration runner.

CREATE TABLE IF NOT EXISTS contracts (
    contract_id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    expiry TEXT,
    sec_type TEXT NOT NULL DEFAULT 'FUT',
    exchange TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    tick_size REAL,
    multiplier TEXT
);

CREATE TABLE IF NOT EXISTS bars (
    contract_id INTEGER NOT NULL,
    timestamp_utc TEXT NOT NULL,
    interval TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    price_type TEXT NOT NULL,
    session_scope TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'IBKR',
    is_completed INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (contract_id, interval, timestamp_utc, price_type, session_scope),
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bars_timestamp ON bars (timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_bars_contract_interval ON bars (contract_id, interval);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    requested_start_utc TEXT NOT NULL,
    requested_end_utc TEXT NOT NULL,
    download_status TEXT NOT NULL,
    errors TEXT,
    missing_intervals TEXT,
    last_successful_update TEXT NOT NULL,
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_snapshots (
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

CREATE INDEX IF NOT EXISTS idx_features_timestamp ON feature_snapshots (timestamp_utc);

CREATE TABLE IF NOT EXISTS predictions (
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

CREATE TABLE IF NOT EXISTS outcomes (
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

CREATE TABLE IF NOT EXISTS analogue_matches (
    match_id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL,
    match_date TEXT NOT NULL,
    similarity_score REAL NOT NULL,
    ranking INTEGER NOT NULL,
    UNIQUE (prediction_id, match_date),
    FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id) ON DELETE CASCADE
);
