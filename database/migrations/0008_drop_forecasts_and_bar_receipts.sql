-- 0008_drop_forecasts_and_bar_receipts.sql
-- Drops everything the removed forecasting and real-time streaming code kept.
--
--   v1 forecast tables (0001, 0002) : feature_snapshots, predictions, outcomes,
--                                     analogue_matches - the analogue forecast's
--                                     records.
--   schema forecast (0004, 0006, 0007): the v2 snapshots, forecast runs,
--                                     predictions, realised outcomes, outcome
--                                     metrics, their registries, views and
--                                     functions. Nothing outside the schema
--                                     depends on it.
--   bar_receipts (0005)             : the real-time streamer's per-bar receive
--                                     times.
--
-- Kept: the market data (contracts, session_days, bars, collection_runs,
-- active_contracts, asset_sources, bar_intervals) and the economic calendar
-- (economic_events, economic_event_coverage).

DROP TABLE analogue_matches;
DROP TABLE predictions;
DROP TABLE outcomes;
DROP TABLE feature_snapshots;

DROP SCHEMA forecast CASCADE;

DROP TABLE bar_receipts;
DROP FUNCTION reject_bar_receipt_mutation();

COMMENT ON COLUMN bars.timestamp_utc IS
    'bar_start_at: the start of the bar''s interval [start, start + interval), in UTC. '
    'IB labels bars by their start, so nothing is converted; a provider that labels by '
    'end time must be shifted at ingestion. A bar is named by its start: '
    '"the 09:28 close" is the bar starting 09:28 ET, which ends at 09:29.';

COMMENT ON COLUMN bars.source IS 'IBKR for a downloaded day; MOCK for populate_mock_data.py.';
