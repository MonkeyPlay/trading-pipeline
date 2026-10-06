-- 0023_bar_versions.sql
-- Two receipt times per bar, for the intermarket fan's forward record (docs/fan_experiment.md):
--
--   first_stored_at    when the bar first reached the store; NULL when unknown - every bar held
--                      from before this migration. The collector rewrites a day's bars on every
--                      run and carries the time over, NULL included: a rewrite never stands in
--                      for an unknown first arrival
--   version_stored_at  when the bar's current values reached the store: carried over while the
--                      values stay the same, the database's clock when IB revises them - the
--                      availability of the values a forecast actually read
--
-- Migration 0022's carry-over gave a bar held from before it the time of its first rewrite.
-- Those times are not arrivals; they are reset to unknown.

UPDATE bars SET first_stored_at = NULL WHERE first_stored_at IS NOT NULL;
ALTER TABLE bars ADD COLUMN version_stored_at TIMESTAMPTZ;
ALTER TABLE bars ALTER COLUMN version_stored_at SET DEFAULT clock_timestamp();
