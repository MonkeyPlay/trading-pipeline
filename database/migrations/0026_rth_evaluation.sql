-- 0026_rth_evaluation.sql
-- The RTH analogues' usefulness evaluation (contracts/rth_eval.py, docs/rth_analogues.md):
-- its forecasts stored when they are issued, and its one scoring.
--
--   journal.definition_versions   gains the kind 'rth_evaluation'
--   journal.rth_eval_forecasts    per session and cutoff window, the four forecasts made by the
--                                 run that stored the live RTH set (set_id): every member with
--                                 session, snapshot, contract, similarity, weight and move, the
--                                 versions and the pre-open set used. The first per session and
--                                 cutoff only. The database stamps created_at and decides
--                                 ``eligible``: stored within the registered definition's
--                                 max_issue_delay_s of the cutoff, and before the window ends -
--                                 apart from the set's provenance (issued_by, receipt times)
--   journal.rth_eval_results      the scoring, once
--
-- Append-only, like the rest of the journal.

ALTER TABLE journal.definition_versions DROP CONSTRAINT definition_versions_kind_check;
ALTER TABLE journal.definition_versions ADD CONSTRAINT definition_versions_kind_check
    CHECK (kind IN ('labels', 'convention', 'snapshot', 'annotation', 'matcher', 'forecast_schema',
                    'forecast_algorithm', 'issue_policy', 'experiment', 'fan_experiment', 'fan_model',
                    'fan_forward', 'rth_matcher', 'rth_evaluation'));

CREATE TABLE journal.rth_eval_forecasts (
    forecast_id        UUID PRIMARY KEY,
    evaluation_version TEXT NOT NULL REFERENCES journal.definition_versions (version),
    set_id             UUID NOT NULL REFERENCES journal.rth_analogue_sets (set_id),
    symbol             TEXT NOT NULL,
    session_date       DATE NOT NULL,
    elapsed_minutes    SMALLINT NOT NULL CHECK (elapsed_minutes BETWEEN 1 AND 60),
    cutoff_at          TIMESTAMPTZ NOT NULL,
    horizon_minutes    SMALLINT NOT NULL CHECK (horizon_minutes > 0),
    target_atr         NUMERIC NOT NULL CHECK (target_atr > 0),
    forecasts          JSONB NOT NULL,
    sources            JSONB NOT NULL,
    digest             TEXT NOT NULL,
    code_revision      TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    issue_delay_s      NUMERIC NOT NULL DEFAULT 0,
    eligible           BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (evaluation_version, symbol, session_date, elapsed_minutes)
);

CREATE TABLE journal.rth_eval_results (
    evaluation_version TEXT PRIMARY KEY REFERENCES journal.definition_versions (version),
    results            JSONB NOT NULL,
    code_revision      TEXT NOT NULL,
    scored_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- The database clock, not the caller, says when a forecast was stored and whether that was in time to count.
CREATE FUNCTION journal.stamp_rth_eval_forecast() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    max_delay NUMERIC;
BEGIN
    SELECT (definition ->> 'max_issue_delay_s')::numeric INTO max_delay
      FROM journal.definition_versions WHERE version = NEW.evaluation_version;
    IF max_delay IS NULL THEN
        RAISE EXCEPTION 'evaluation % has no registered max_issue_delay_s', NEW.evaluation_version;
    END IF;
    NEW.created_at := clock_timestamp();
    NEW.issue_delay_s := extract(epoch FROM NEW.created_at - NEW.cutoff_at);
    NEW.eligible := NEW.issue_delay_s <= max_delay
                    AND NEW.created_at < NEW.cutoff_at + make_interval(mins => NEW.horizon_minutes);
    RETURN NEW;
END;
$$;

CREATE FUNCTION journal.stamp_rth_eval_result() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.scored_at := clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE TRIGGER rth_eval_forecasts_stamp BEFORE INSERT ON journal.rth_eval_forecasts
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_rth_eval_forecast();
CREATE TRIGGER rth_eval_results_stamp BEFORE INSERT ON journal.rth_eval_results
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_rth_eval_result();

CREATE TRIGGER rth_eval_forecasts_append_only BEFORE UPDATE OR DELETE ON journal.rth_eval_forecasts
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER rth_eval_forecasts_no_truncate BEFORE TRUNCATE ON journal.rth_eval_forecasts
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER rth_eval_results_append_only BEFORE UPDATE OR DELETE ON journal.rth_eval_results
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER rth_eval_results_no_truncate BEFORE TRUNCATE ON journal.rth_eval_results
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
