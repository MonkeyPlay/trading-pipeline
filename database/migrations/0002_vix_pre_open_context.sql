-- 0002_vix_pre_open_context.sql
-- Records the cash VIX level as it stood at the 09:30 ET feature cutoff, as
-- pre-open context for the equity-index forecasts.
--
-- VIX is collected like any other instrument (its own contracts row, sec_type
-- 'IND', NULL expiry) but is never a forecast target, so these columns live on
-- the futures' snapshots rather than getting their own table.
--
-- Both are nullable: snapshots computed before VIX was collected, or for a day
-- with no VIX bars, simply leave them NULL rather than reporting a wrong level.

ALTER TABLE feature_snapshots
    ADD COLUMN IF NOT EXISTS vix_pre_open DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS vix_change   DOUBLE PRECISION;

COMMENT ON COLUMN feature_snapshots.vix_pre_open IS
    'Last cash VIX print strictly before 09:30 ET on the target trading day.';
COMMENT ON COLUMN feature_snapshots.vix_change IS
    'vix_pre_open minus the previous session''s closing VIX, in index points.';
