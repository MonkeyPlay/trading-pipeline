-- 0020_fan_experiment_results.sql
-- journal.experiment_results also keeps the scorings of the intermarket fan experiment
-- (kind fan_experiment, migration 0019) - first its baseline gate, fan_rw_v2 against
-- fan_rw_v1 on the development checks (forecaster/fan_harness.py) - append-only like
-- every result. The P1 Evaluation page still lists kind 'experiment' only.

CREATE OR REPLACE FUNCTION journal.check_experiment_row() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF COALESCE((SELECT kind FROM journal.definition_versions WHERE version = NEW.experiment), '')
            NOT IN ('experiment', 'fan_experiment') THEN
        RAISE EXCEPTION '% is not an experiment', NEW.experiment USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
