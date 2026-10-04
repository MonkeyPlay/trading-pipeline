-- 0015_experiments.sql
-- Guideline revision 2, stage 4: registered forecast experiments.
--
--   journal.definition_versions     gains the kind 'experiment': an experiment's
--                                   manifest (session range, profile, versions, arms,
--                                   official-run rule, targets, metrics, zero policy,
--                                   uncertainty method) is registered like any other
--                                   definition - before any result is seen, and never
--                                   changed under its name (forecaster/experiments.py)
--   journal.experiment_cases        per session and arm, frozen once: the official run
--                                   the manifest's rule chose and the outcome revision it
--                                   is scored against, or why there is none
--   journal.experiment_results      every scoring of an experiment's frozen cases, with
--                                   the code revision that computed it
--
-- Append-only, like the rest of the journal.

ALTER TABLE journal.definition_versions DROP CONSTRAINT definition_versions_kind_check;
ALTER TABLE journal.definition_versions ADD CONSTRAINT definition_versions_kind_check
    CHECK (kind IN ('labels', 'convention', 'snapshot', 'annotation', 'matcher', 'forecast_schema',
                    'forecast_algorithm', 'issue_policy', 'experiment'));

CREATE TABLE journal.experiment_cases (
    experiment       TEXT NOT NULL REFERENCES journal.definition_versions (version),
    session_date     DATE NOT NULL,
    arm              TEXT NOT NULL,
    run_id           UUID REFERENCES journal.forecast_runs (run_id),
    snapshot_id      UUID REFERENCES journal.snapshots (snapshot_id),
    outcome_revision INTEGER CHECK (outcome_revision >= 1),
    status           TEXT NOT NULL CHECK (status IN ('case', 'no_run', 'no_outcome')),
    detail           TEXT,
    frozen_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (experiment, session_date, arm),
    CHECK ((status = 'case') = (run_id IS NOT NULL AND outcome_revision IS NOT NULL))
);

CREATE TABLE journal.experiment_results (
    result_id     UUID PRIMARY KEY,
    experiment    TEXT NOT NULL REFERENCES journal.definition_versions (version),
    results       JSONB NOT NULL,
    results_hash  TEXT NOT NULL,
    code_revision TEXT NOT NULL,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX idx_experiment_results ON journal.experiment_results (experiment, computed_at);

CREATE FUNCTION journal.check_experiment_row() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT kind FROM journal.definition_versions WHERE version = NEW.experiment) <> 'experiment' THEN
        RAISE EXCEPTION '% is not an experiment', NEW.experiment USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER experiment_cases_check BEFORE INSERT ON journal.experiment_cases
    FOR EACH ROW EXECUTE FUNCTION journal.check_experiment_row();
CREATE TRIGGER experiment_results_check BEFORE INSERT ON journal.experiment_results
    FOR EACH ROW EXECUTE FUNCTION journal.check_experiment_row();

CREATE TRIGGER experiment_cases_append_only BEFORE UPDATE OR DELETE ON journal.experiment_cases
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER experiment_cases_no_truncate BEFORE TRUNCATE ON journal.experiment_cases
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER experiment_results_append_only BEFORE UPDATE OR DELETE ON journal.experiment_results
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER experiment_results_no_truncate BEFORE TRUNCATE ON journal.experiment_results
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
