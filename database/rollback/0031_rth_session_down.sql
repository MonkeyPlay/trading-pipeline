-- database/rollback/0031_rth_session_down.sql
-- EMERGENCY ONLY - not a migration (the runner reads database/migrations/ only; migrations are forward-only).
-- Takes the schema from v31 back to v30 so that revision 900f3cb starts again, and ONLY while nothing uses 0031:
-- no full-session (nq_match_rth_v3) set, no recorded miss, no rth_session_v1 forecast - i.e. after the deployment
-- and before the first RTH issue (Monday 2026-10-12, 09:31 ET). Once v3 rows exist the journal's append-only
-- triggers keep them, and the way back is the pre-deployment backup (docs/reports/deployment_plan_2026-10-10.md).
--
--   docker exec -i trading_pipeline-timescaledb-1 psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--       < database/rollback/0031_rth_session_down.sql
--
-- One transaction: it refuses (and changes nothing) when 0031 is in use or the database is not at v31.

BEGIN;

DO $$
BEGIN
    IF (SELECT max(version) FROM schema_migrations) <> 31 THEN
        RAISE EXCEPTION 'the database is not at schema v31: nothing to undo';
    END IF;
    IF EXISTS (SELECT 1 FROM journal.rth_analogue_sets
                WHERE matcher_version NOT IN ('nq_match_rth_v1', 'nq_match_rth_v2') OR elapsed_minutes > 60
                   OR issue_class IS NOT NULL OR session_minutes IS NOT NULL)
       OR EXISTS (SELECT 1 FROM journal.rth_issue_misses)
       OR EXISTS (SELECT 1 FROM journal.rth_eval_forecasts
                   WHERE evaluation_version NOT IN ('rth_continuation_v1', 'rth_continuation_v2', 'rth_operational_v1')
                      OR elapsed_minutes > 60) THEN
        RAISE EXCEPTION '0031 is in use (full-session sets, misses or rth_session_v1 forecasts exist): restore the '
                        'pre-deployment backup instead';
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
