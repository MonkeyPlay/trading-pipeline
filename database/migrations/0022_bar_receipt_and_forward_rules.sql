-- 0022_bar_receipt_and_forward_rules.sql
-- Timing for the intermarket fan's forward record (docs/fan_experiment.md, chunk 8):
--
--   bars.first_stored_at        when a bar first reached the store. The collector rewrites a
--                               day's bars on every run; it carries this time over for every bar
--                               it already held (database/queries.py), so it stays the moment the
--                               bar became available here - an input's receipt time. Bars stored
--                               before this migration have none.
--   kind 'fan_forward'          a registered forward-record definition: the frozen model it issues,
--                               its schedule, the rule an issue is on time by and the evaluation it
--                               feeds - fixed before the evaluation sample it governs
--   fan_forward_issues.record   the forward-record definition an issue was made under

ALTER TABLE bars ADD COLUMN first_stored_at TIMESTAMPTZ;
ALTER TABLE bars ALTER COLUMN first_stored_at SET DEFAULT clock_timestamp();

ALTER TABLE journal.definition_versions DROP CONSTRAINT definition_versions_kind_check;
ALTER TABLE journal.definition_versions ADD CONSTRAINT definition_versions_kind_check
    CHECK (kind IN ('labels', 'convention', 'snapshot', 'annotation', 'matcher', 'forecast_schema',
                    'forecast_algorithm', 'issue_policy', 'experiment', 'fan_experiment', 'fan_model',
                    'fan_forward'));

ALTER TABLE journal.fan_forward_issues ADD COLUMN record TEXT REFERENCES journal.definition_versions (version);
