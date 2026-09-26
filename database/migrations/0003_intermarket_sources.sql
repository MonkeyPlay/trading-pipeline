-- 0003_intermarket_sources.sql
-- Prepares the store for the pre-open intermarket features (NQ/ES/RTY returns,
-- VIX/VXN levels, yields, the dollar, semiconductors).
--
-- Those features pair two observations of the *same* instrument: its latest
-- eligible pre-open value and its value at/before the previous NQ RTH close. That
-- needs three things the store did not record:
--
--   contracts        : what IB says about each contract - its contract month (the
--                      roll needs it; for CL it differs from the expiry month), and
--                      the trading/liquid hours and time zone that bound when a
--                      genuine observation can exist at all.
--
--   active_contracts : which contract stood for a symbol on each trading day. The
--                      collector fills it from the instrument's roll rule; a
--                      feature reads its contract here instead of guessing, so a
--                      return never mixes two expiries. The collector also stores
--                      the trading day *before* a contract becomes active, so the
--                      first day after a roll still has a same-contract reference.
--
--   asset_sources    : every version of the logical-asset -> instrument map
--                      (config.ASSET_SOURCES), with units, proxy flags and the
--                      freshness rule, so a feature row can always be traced to
--                      the exact source definition it was built under.

ALTER TABLE contracts
    ADD COLUMN IF NOT EXISTS contract_month   TEXT,        -- 'YYYYMM', futures only
    ADD COLUMN IF NOT EXISTS local_symbol     TEXT,
    ADD COLUMN IF NOT EXISTS trading_class    TEXT,
    ADD COLUMN IF NOT EXISTS primary_exchange TEXT,
    ADD COLUMN IF NOT EXISTS time_zone_id     TEXT,
    ADD COLUMN IF NOT EXISTS trading_hours    TEXT,        -- IB tradingHours, as sent
    ADD COLUMN IF NOT EXISTS liquid_hours     TEXT,        -- IB liquidHours, as sent
    ADD COLUMN IF NOT EXISTS updated_at       TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_contracts_symbol ON contracts (symbol, expiry);

CREATE TABLE active_contracts (
    symbol      TEXT NOT NULL,
    trading_day DATE NOT NULL,                   -- NY session date
    contract_id BIGINT NOT NULL REFERENCES contracts (contract_id) ON DELETE CASCADE,
    rule        TEXT NOT NULL,                   -- how it was chosen, e.g. the roll rule
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, trading_day)
);

CREATE INDEX idx_active_contracts_contract ON active_contracts (contract_id, trading_day);

CREATE TABLE asset_sources (
    asset               TEXT NOT NULL,           -- logical asset: nq, es, vix, us10y, ...
    config_hash         TEXT NOT NULL,           -- hash of every field below
    symbol              TEXT,                    -- NULL: no recorded source, features are null
    sec_type            TEXT,
    exchange            TEXT,
    what_to_show        TEXT,                    -- IB whatToShow = bars.price_type
    value_kind          TEXT,                    -- price | index_level | yield
    value_unit          TEXT,                    -- price | index_points | percent | percent_x10 | decimal
    bps_per_unit        DOUBLE PRECISION,        -- yields: bps = (now - ref) * bps_per_unit
    max_age_minutes     INTEGER,                 -- freshness rule for both endpoints
    roll_rule           TEXT,
    is_proxy            BOOLEAN NOT NULL DEFAULT false,
    optional            BOOLEAN NOT NULL DEFAULT false,
    collected           BOOLEAN NOT NULL,        -- was the symbol in the collector's set
    description         TEXT,
    notes               TEXT,
    first_registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_registered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset, config_hash),
    CHECK (value_kind IS NULL OR value_kind IN ('price', 'index_level', 'yield')),
    CHECK (value_unit IS NULL OR value_unit IN ('price', 'index_points', 'percent', 'percent_x10', 'decimal'))
);

-- The definition each asset is currently collected under.
CREATE VIEW current_asset_sources AS
SELECT DISTINCT ON (asset) *
  FROM asset_sources
 ORDER BY asset, last_registered_at DESC;

COMMENT ON COLUMN bars.timestamp_utc IS
    'Bar OPEN time in UTC (IB convention). A 1-minute bar stamped 09:27 ET closes at 09:28 ET, '
    'so the "09:28 close" is the close of the bar stamped 09:27.';
COMMENT ON COLUMN bars.volume IS
    'Traded volume; 0 for a cash index, which IB reports without volume.';
COMMENT ON TABLE bars IS
    'Only bars the source actually sent. Minutes without a print are absent, never '
    'forward-filled, so the age of the latest value is always recoverable.';
