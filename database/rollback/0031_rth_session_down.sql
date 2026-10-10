-- database/rollback/0031_rth_session_down.sql
-- EMERGENCY ONLY - not a migration (the runner reads database/migrations/ only; migrations are forward-only).
-- Takes the schema from v31 back to exactly v30, so that revision 900f3cb (schema v30, the same ML artifacts) starts
-- again - and only while no row needs v31. Which rows those are is decided here from the data, never from the
-- calendar: it refuses when any of these exist, whoever wrote them (Auto, a manual issue, a backfill or a test):
--
--   journal.rth_analogue_sets     a set of a version other than nq_match_rth_v1/v2, one past 60 minutes, or any set
--                                 stored since 0031 - its trigger stamps issue_class on every new row, first-hour
--                                 sets included - or carrying one of 0031's timing columns
--   journal.rth_issue_misses      any row (the table goes)
--   journal.rth_eval_forecasts    a forecast of an evaluation other than rth_continuation_v1/v2 and
--                                 rth_operational_v1, one past 60 minutes, or a second horizon of one key - the v30
--                                 unique constraint would reject it
--   journal.rth_eval_results      a result of such an evaluation
--
-- Members of a refused set and forecasts built on it are covered by the set. Definitions registered by the v31 code
-- (nq_match_rth_v3, rth_session_v1) are append-only history: they stay, reported, and refuse nothing - the v30 code
-- never reads them.
--
-- Stop every writer first (scripts/release_check.py writers). One transaction: every table it reads or alters is
-- locked before the check (a writer cannot add a row between the check and the change); any failure leaves the
-- database exactly as it was.
--
--   docker exec -i trading_pipeline-timescaledb-1 psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--       < database/rollback/0031_rth_session_down.sql

BEGIN;
SET LOCAL lock_timeout = '10s';
LOCK TABLE journal.rth_analogue_sets, journal.rth_analogue_members, journal.rth_eval_forecasts,
           journal.rth_eval_results, journal.rth_issue_misses, schema_migrations IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE
    v INTEGER;
    sets BIGINT;
    misses BIGINT;
    forecasts BIGINT;
    results BIGINT;
    keys BIGINT;
    defs TEXT;
BEGIN
    SELECT max(version) INTO v FROM schema_migrations;
    IF v IS DISTINCT FROM 31 THEN
        RAISE EXCEPTION 'the database is at schema v%, not v31: nothing to undo', v;
    END IF;
    SELECT count(*) INTO sets FROM journal.rth_analogue_sets
     WHERE matcher_version NOT IN ('nq_match_rth_v1', 'nq_match_rth_v2') OR elapsed_minutes > 60
        OR issue_class IS NOT NULL OR session_minutes IS NOT NULL OR computation_started_at IS NOT NULL
        OR confirmed_by_start IS NOT NULL OR confirmed_received_at IS NOT NULL;
    SELECT count(*) INTO misses FROM journal.rth_issue_misses;
    SELECT count(*) INTO forecasts FROM journal.rth_eval_forecasts
     WHERE evaluation_version NOT IN ('rth_continuation_v1', 'rth_continuation_v2', 'rth_operational_v1')
        OR elapsed_minutes > 60;
    SELECT count(*) INTO results FROM journal.rth_eval_results
     WHERE evaluation_version NOT IN ('rth_continuation_v1', 'rth_continuation_v2', 'rth_operational_v1');
    SELECT count(*) INTO keys FROM (SELECT 1 FROM journal.rth_eval_forecasts
                                     GROUP BY evaluation_version, symbol, session_date, elapsed_minutes
                                    HAVING count(*) > 1) k;
    IF sets + misses + forecasts + results + keys > 0 THEN
        RAISE EXCEPTION '0031 is in use: % set(s) only v31 can hold, % miss(es), % forecast(s) and % result(s) of a '
                        'new evaluation, % key(s) the v30 constraint would reject - nothing changed; restore the '
                        'pre-deployment backup instead', sets, misses, forecasts, results, keys;
    END IF;
    SELECT string_agg(version, ', ' ORDER BY version) INTO defs FROM journal.definition_versions
     WHERE version IN ('nq_match_rth_v3', 'rth_session_v1');
    IF defs IS NOT NULL THEN
        RAISE NOTICE 'kept (append-only history, unused by the v30 code): definitions %', defs;
    END IF;
END
$$;

DROP TABLE journal.rth_issue_misses;
DROP FUNCTION journal.stamp_rth_issue_miss();

-- the stamp trigger as 0025 left it
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

ALTER TABLE journal.rth_analogue_sets
    DROP COLUMN issue_class,
    DROP COLUMN confirmed_received_at,
    DROP COLUMN confirmed_by_start,
    DROP COLUMN computation_started_at,
    DROP COLUMN session_minutes;
ALTER TABLE journal.rth_analogue_sets DROP CONSTRAINT rth_analogue_sets_elapsed_minutes_check;
ALTER TABLE journal.rth_analogue_sets ADD CONSTRAINT rth_analogue_sets_elapsed_minutes_check
    CHECK (elapsed_minutes >= 1 AND elapsed_minutes <= 60);

ALTER TABLE journal.rth_eval_forecasts DROP CONSTRAINT rth_eval_forecasts_elapsed_minutes_check;
ALTER TABLE journal.rth_eval_forecasts ADD CONSTRAINT rth_eval_forecasts_elapsed_minutes_check
    CHECK (elapsed_minutes >= 1 AND elapsed_minutes <= 60);
DROP INDEX journal.rth_eval_forecasts_identity;
ALTER TABLE journal.rth_eval_forecasts ADD CONSTRAINT rth_eval_forecasts_evaluation_version_symbol_session_date_e_key
    UNIQUE (evaluation_version, symbol, session_date, elapsed_minutes);

DELETE FROM schema_migrations WHERE version = 31;

COMMIT;
