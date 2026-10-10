-- 0031_rth_session.sql
-- The full-session RTH matcher (nq_match_rth_v3, contracts/nq_rth.py) and its evaluation (rth_session_v1,
-- contracts/rth_session.py), beside the first-hour records of nq_match_rth_v1/v2, rth_continuation_v2 and
-- rth_operational_v1, which keep every guarantee they had:
--
--   * windows to the longest regular session (390 minutes) for the new versions only: the first-hour versions stay
--     limited to 60 minutes, now by name
--   * a set's timing record: when its computation started, the bar that confirmed its last bar (its start and when
--     it reached the store), the session's scheduled RTH length, and issue_class - decided by the database:
--       reconstruction  a backfill, or stored more than 30 minutes after its cutoff (data_mode
--                       historical_reconstruction)
--       timely          issued by auto or manual within 2 minutes of its last input reaching the store - as current
--                       as the feed allows (contracts/nq_rth.TIMELY_WITHIN)
--       late            issued live, but later than that (a catch-up)
--     NULL on the rows stored before this migration: their class was never recorded
--   * one evaluation forecast per evaluation, session, cutoff and horizon (rth_session_v1 forecasts several horizons
--     at one cutoff; the earlier evaluations have one horizon, so their rows stay unique)
--   * journal.rth_issue_misses: the windows a live issue did not store and why - a window is never stored later
--     under a made-up issue time

ALTER TABLE journal.rth_analogue_sets DROP CONSTRAINT rth_analogue_sets_elapsed_minutes_check;
ALTER TABLE journal.rth_analogue_sets ADD CONSTRAINT rth_analogue_sets_elapsed_minutes_check
    CHECK (elapsed_minutes >= 1 AND elapsed_minutes <=
           CASE WHEN matcher_version IN ('nq_match_rth_v1', 'nq_match_rth_v2') THEN 60 ELSE 390 END);

ALTER TABLE journal.rth_analogue_sets
    ADD COLUMN session_minutes        SMALLINT CHECK (session_minutes BETWEEN 1 AND 390),
    ADD COLUMN computation_started_at TIMESTAMPTZ,
    ADD COLUMN confirmed_by_start     TIMESTAMPTZ,
    ADD COLUMN confirmed_received_at  TIMESTAMPTZ,
    ADD COLUMN issue_class            TEXT CHECK (issue_class IN ('timely', 'late', 'reconstruction'));

CREATE OR REPLACE FUNCTION journal.stamp_rth_analogue_set() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.created_at := clock_timestamp();
    NEW.data_mode := CASE WHEN NEW.issued_by IN ('auto', 'manual')
                               AND NEW.created_at <= NEW.cutoff_at + interval '30 minutes'
                          THEN 'live' ELSE 'historical_reconstruction' END;
    NEW.issue_class := CASE WHEN NEW.data_mode = 'historical_reconstruction' THEN 'reconstruction'
                            WHEN NEW.inputs_received_at IS NOT NULL
                                 AND NEW.created_at <= NEW.inputs_received_at + interval '2 minutes' THEN 'timely'
                            ELSE 'late' END;
    RETURN NEW;
END;
$$;

ALTER TABLE journal.rth_eval_forecasts DROP CONSTRAINT rth_eval_forecasts_elapsed_minutes_check;
ALTER TABLE journal.rth_eval_forecasts ADD CONSTRAINT rth_eval_forecasts_elapsed_minutes_check
    CHECK (elapsed_minutes >= 1 AND elapsed_minutes <=
           CASE WHEN evaluation_version IN ('rth_continuation_v1', 'rth_continuation_v2', 'rth_operational_v1')
                THEN 60 ELSE 390 END);
ALTER TABLE journal.rth_eval_forecasts DROP CONSTRAINT rth_eval_forecasts_evaluation_version_symbol_session_date_e_key;
CREATE UNIQUE INDEX rth_eval_forecasts_identity
    ON journal.rth_eval_forecasts (evaluation_version, symbol, session_date, elapsed_minutes, horizon_minutes);

CREATE TABLE journal.rth_issue_misses (
    miss_id         BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    session_date    DATE NOT NULL,
    matcher_version TEXT NOT NULL REFERENCES journal.definition_versions (version),
    first_minutes   SMALLINT NOT NULL CHECK (first_minutes BETWEEN 1 AND 390),
    last_minutes    SMALLINT NOT NULL CHECK (last_minutes BETWEEN 1 AND 390),
    reason          TEXT NOT NULL CHECK (reason IN ('coalesced', 'not_running', 'expired')),
    detail          TEXT NOT NULL,
    issued_by       TEXT NOT NULL CHECK (issued_by IN ('auto', 'manual')),
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (last_minutes >= first_minutes)
);
CREATE INDEX idx_rth_issue_misses_session ON journal.rth_issue_misses (symbol, session_date, matcher_version);

CREATE FUNCTION journal.stamp_rth_issue_miss() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.recorded_at := clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER rth_issue_misses_stamp BEFORE INSERT ON journal.rth_issue_misses
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_rth_issue_miss();
CREATE TRIGGER rth_issue_misses_append_only BEFORE UPDATE OR DELETE ON journal.rth_issue_misses
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER rth_issue_misses_no_truncate BEFORE TRUNCATE ON journal.rth_issue_misses
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
