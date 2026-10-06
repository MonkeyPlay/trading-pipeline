-- 0019_fan_experiments.sql
-- The intermarket fan experiment (step 3 of the fan; forecaster/fan_experiment.py,
-- docs/fan_experiment.md) is fixed in advance like the P1 experiments, as registered
-- definitions with their own kinds - the Evaluation page reads kind 'experiment' as
-- a P1 manifest, so the fan's cannot share it:
--
--   fan_experiment   the manifest: question, targets, horizons and their roles, the
--                    score and its uncertainty, the baselines and gates, the session
--                    split (development, the three checks, the sealed holdout),
--                    instruments and groups, exclusions, attribution
--   fan_model        a model frozen against one fan_experiment (its name and hash and
--                    the development sessions it was trained on): only such a record
--                    opens that experiment's holdout
--
-- Immutable, like every registered definition: a changed manifest is a new version.

ALTER TABLE journal.definition_versions DROP CONSTRAINT definition_versions_kind_check;
ALTER TABLE journal.definition_versions ADD CONSTRAINT definition_versions_kind_check
    CHECK (kind IN ('labels', 'convention', 'snapshot', 'annotation', 'matcher', 'forecast_schema',
                    'forecast_algorithm', 'issue_policy', 'experiment', 'fan_experiment', 'fan_model'));
