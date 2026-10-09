-- 0025_rth_issue_provenance.sql
-- How an RTH analogue set was produced and from inputs stored when (matcher nq_match_rth_v2,
-- contracts/nq_rth.py, docs/rth_analogues.md). Migration 0024 called a set live by its age
-- alone, so a reconstruction run within 30 minutes of a cutoff would have counted.
--
--   issued_by           auto (Auto mode, after a successful collection), manual (a person) or
--                       backfill (a reconstruction). The sets stored before this migration came
--                       from rth-backfill and are marked so
--   inputs_received_at  when every input of the target the set read had reached the store: its
--                       window's bars, the bar that confirmed the last of them, its overnight bars,
--                       its snapshot (its distance from cutoff_at is the feed's delay)
--   pool_received_at    the same for every earlier session read
--   pit_status          verified: every earlier input was in the store by the cutoff and the
--                       target's within 30 minutes of it, as a live issue would have had them; else
--                       unverified (NULL: stored before this migration)
--   data_mode           live only when issued by auto or manual within 30 minutes of the cutoff, by
--                       the database clock; a backfill is always a historical reconstruction
--
-- The columns are added with their values for the existing rows (no row is updated: the
-- tables are append-only).

ALTER TABLE journal.rth_analogue_sets
    ADD COLUMN issued_by TEXT NOT NULL DEFAULT 'backfill' CHECK (issued_by IN ('auto', 'manual', 'backfill'));
ALTER TABLE journal.rth_analogue_sets ALTER COLUMN issued_by DROP DEFAULT;
ALTER TABLE journal.rth_analogue_sets
    ADD COLUMN inputs_received_at TIMESTAMPTZ,
    ADD COLUMN pool_received_at TIMESTAMPTZ,
    ADD COLUMN pit_status TEXT CHECK (pit_status IN ('verified', 'unverified'));

CREATE INDEX idx_rth_analogue_sets_issued
    ON journal.rth_analogue_sets (symbol, session_date, matcher_version, created_at) WHERE data_mode = 'live';

CREATE OR REPLACE FUNCTION journal.stamp_rth_analogue_set() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.created_at := clock_timestamp();
    NEW.data_mode := CASE WHEN NEW.issued_by IN ('auto', 'manual')
                               AND NEW.created_at <= NEW.cutoff_at + interval '30 minutes'
                          THEN 'live' ELSE 'historical_reconstruction' END;
    RETURN NEW;
END;
$$;
