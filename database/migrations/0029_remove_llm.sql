-- 0029_remove_llm.sql
-- Removes the LLM forecasting system's records (2026-10-09: replaced by the scikit-learn
-- forecaster, contracts/nq_ml.py). Shared records stay exactly as they are: snapshots,
-- bars and receipts, the rule-based annotations and their analogue sets, the A and B
-- forecast runs, outcomes, review sets, experiments without an LLM arm, the fan and the
-- RTH evaluations.
--
-- What goes, identified by its registered definitions, not by guesswork:
--   definitions   annotation protocols whose definition names annotator 'llm', the
--                 forecast algorithms of arms C (nq_restricted_p1_*) and D
--                 (nq_synthesis_p1_*), D's forecast schema (nq_forecast_schema_v2),
--                 the live issue policy that named arm D (nq_issue_live_v3) when no run or
--                 capture uses it, and experiments with an LLM arm
--   rows          those protocols' annotations; the analogue sets (and members) built on
--                 them; the forecast runs of those algorithms or on those annotations or
--                 sets, with their evidence, predictions and events; the cases and results
--                 of the LLM experiments; every annotation attempt, inference request,
--                 batch and batch membership (tables only the LLM used)
--   schema        the LLM-only tables, forecast_runs.request_id, the 'judgement'
--                 estimation status, the 'llm' annotator, live_captures.with_d and the
--                 synthesis capture steps
--
-- Dependency order, explicit row sets, no ON DELETE CASCADE. Before deleting, every
-- reference into the doomed rows from a record that stays is counted; any one stops the
-- migration (nothing is applied). The append-only guards are paused for these deletions
-- only, inside this transaction, and restored before it ends. Applied migrations are not
-- edited: 0011-0028 still describe what was built.

DO $$
DECLARE
    n BIGINT;
    t TEXT;
BEGIN
    -- the doomed definitions and rows ---------------------------------------------------
    CREATE TEMP TABLE llm_defs ON COMMIT DROP AS
        SELECT version FROM journal.definition_versions
         WHERE (kind = 'annotation' AND definition ->> 'annotator' = 'llm')
            OR (kind = 'forecast_algorithm' AND (version LIKE 'nq_restricted_p1_%' OR version LIKE 'nq_synthesis_p1_%'))
            OR (kind = 'forecast_schema' AND version = 'nq_forecast_schema_v2');
    INSERT INTO llm_defs
        SELECT version FROM journal.definition_versions d
         WHERE kind = 'experiment' AND EXISTS (
               SELECT 1 FROM jsonb_each(d.definition -> 'arms') a
                WHERE a.value ->> 'algorithm' IN (SELECT version FROM llm_defs)
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(a.value -> 'delivered', '[]'::jsonb)) x
                               WHERE x IN (SELECT version FROM llm_defs)));
    INSERT INTO llm_defs
        SELECT version FROM journal.definition_versions
         WHERE kind = 'issue_policy' AND version = 'nq_issue_live_v3'
           AND NOT EXISTS (SELECT 1 FROM journal.forecast_runs WHERE issue_policy = 'nq_issue_live_v3')
           AND NOT EXISTS (SELECT 1 FROM journal.live_captures WHERE issue_policy = 'nq_issue_live_v3');

    CREATE TEMP TABLE llm_annotations ON COMMIT DROP AS
        SELECT annotation_id FROM journal.structure_annotations
         WHERE protocol_version IN (SELECT version FROM llm_defs) OR annotator = 'llm';
    CREATE TEMP TABLE llm_sets ON COMMIT DROP AS
        SELECT set_id FROM journal.analogue_sets WHERE target_annotation_id IN (SELECT annotation_id FROM llm_annotations);
    CREATE TEMP TABLE llm_runs ON COMMIT DROP AS
        SELECT run_id FROM journal.forecast_runs
         WHERE algorithm_version IN (SELECT version FROM llm_defs) OR schema_version IN (SELECT version FROM llm_defs)
            OR annotation_id IN (SELECT annotation_id FROM llm_annotations)
            OR analogue_set_id IN (SELECT set_id FROM llm_sets) OR request_id IS NOT NULL;

    -- nothing that stays may depend on them -----------------------------------------------
    SELECT count(*) INTO n FROM journal.forecast_runs f
     WHERE f.run_id NOT IN (SELECT run_id FROM llm_runs) AND f.supersedes_run_id IN (SELECT run_id FROM llm_runs);
    IF n > 0 THEN RAISE EXCEPTION '0029: % kept run(s) supersede an LLM run', n; END IF;
    SELECT count(*) INTO n FROM journal.analogue_members m
     WHERE m.set_id NOT IN (SELECT set_id FROM llm_sets) AND m.annotation_id IN (SELECT annotation_id FROM llm_annotations);
    IF n > 0 THEN RAISE EXCEPTION '0029: % kept analogue member(s) use an LLM annotation', n; END IF;
    SELECT count(*) INTO n FROM journal.annotation_review_members
     WHERE annotation_id IN (SELECT annotation_id FROM llm_annotations);
    IF n > 0 THEN RAISE EXCEPTION '0029: % annotation review member(s) use an LLM annotation', n; END IF;
    SELECT count(*) INTO n FROM journal.annotation_review_verdicts
     WHERE annotation_id IN (SELECT annotation_id FROM llm_annotations);
    IF n > 0 THEN RAISE EXCEPTION '0029: % annotation review verdict(s) use an LLM annotation', n; END IF;
    SELECT count(*) INTO n FROM journal.experiment_cases
     WHERE run_id IN (SELECT run_id FROM llm_runs) AND experiment NOT IN (SELECT version FROM llm_defs);
    IF n > 0 THEN RAISE EXCEPTION '0029: % case(s) of an experiment without an LLM arm use an LLM run', n; END IF;
    SELECT count(*) INTO n FROM journal.annotation_review_sets WHERE protocol_version IN (SELECT version FROM llm_defs);
    IF n > 0 THEN RAISE EXCEPTION '0029: % annotation review set(s) are of an LLM protocol', n; END IF;

    -- delete, the append-only guards paused for this transaction only ---------------------
    FOREACH t IN ARRAY ARRAY['forecast_run_events', 'forecast_predictions', 'forecast_evidence', 'forecast_runs',
                             'experiment_cases', 'experiment_results', 'analogue_members', 'analogue_sets',
                             'annotation_attempts', 'inference_batch_requests', 'inference_batches',
                             'inference_requests', 'structure_annotations', 'definition_versions'] LOOP
        EXECUTE format('ALTER TABLE journal.%I DISABLE TRIGGER %I', t, t || '_append_only');
    END LOOP;

    DELETE FROM journal.forecast_run_events WHERE run_id IN (SELECT run_id FROM llm_runs);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % forecast run event(s)', n;
    DELETE FROM journal.forecast_predictions WHERE run_id IN (SELECT run_id FROM llm_runs);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % forecast prediction(s)', n;
    DELETE FROM journal.forecast_evidence WHERE run_id IN (SELECT run_id FROM llm_runs);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % forecast evidence row(s)', n;
    DELETE FROM journal.experiment_cases WHERE experiment IN (SELECT version FROM llm_defs);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % experiment case(s) of LLM experiments', n;
    DELETE FROM journal.experiment_results WHERE experiment IN (SELECT version FROM llm_defs);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % experiment result(s) of LLM experiments', n;
    DELETE FROM journal.forecast_runs WHERE run_id IN (SELECT run_id FROM llm_runs);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % forecast run(s) (arms C and D)', n;
    DELETE FROM journal.analogue_members WHERE set_id IN (SELECT set_id FROM llm_sets);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % analogue member(s) of LLM-annotation sets', n;
    DELETE FROM journal.analogue_sets WHERE set_id IN (SELECT set_id FROM llm_sets);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % analogue set(s) built on LLM annotations', n;
    DELETE FROM journal.annotation_attempts;
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % LLM annotation attempt(s)', n;
    DELETE FROM journal.inference_batch_requests;
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % batch request link(s)', n;
    DELETE FROM journal.inference_batches;
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % inference batch(es)', n;
    ALTER TABLE journal.forecast_runs DROP COLUMN request_id;
    DELETE FROM journal.inference_requests;
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % inference request(s) (requests, responses and usage)', n;
    DELETE FROM journal.structure_annotations WHERE annotation_id IN (SELECT annotation_id FROM llm_annotations);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % LLM structure annotation(s)', n;
    DELETE FROM journal.definition_versions WHERE version IN (SELECT version FROM llm_defs);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '0029: deleted % LLM definition(s)', n;

    FOREACH t IN ARRAY ARRAY['forecast_run_events', 'forecast_predictions', 'forecast_evidence', 'forecast_runs',
                             'experiment_cases', 'experiment_results', 'analogue_members', 'analogue_sets',
                             'structure_annotations', 'definition_versions'] LOOP
        EXECUTE format('ALTER TABLE journal.%I ENABLE TRIGGER %I', t, t || '_append_only');
    END LOOP;
END;
$$;

-- the LLM-only tables and columns -------------------------------------------------------
DROP TABLE journal.inference_batch_requests;
DROP TABLE journal.inference_batches;
DROP TABLE journal.annotation_attempts;
DROP TABLE journal.inference_requests;

ALTER TABLE journal.forecast_predictions DROP CONSTRAINT forecast_predictions_estimation_status_check;
ALTER TABLE journal.forecast_predictions ADD CONSTRAINT forecast_predictions_estimation_status_check
    CHECK (estimation_status IN ('analogues', 'prior_only', 'none'));

ALTER TABLE journal.structure_annotations DROP CONSTRAINT structure_annotations_annotator_check;
ALTER TABLE journal.structure_annotations ADD CONSTRAINT structure_annotations_annotator_check
    CHECK (annotator = 'rules');

ALTER TABLE journal.live_captures DROP COLUMN with_d;
ALTER TABLE journal.live_capture_events DROP CONSTRAINT live_capture_events_event_check;
ALTER TABLE journal.live_capture_events ADD CONSTRAINT live_capture_events_event_check
    CHECK (event IN ('bars_requested', 'bars_received', 'stale', 'snapshot_frozen', 'snapshot_reused', 'annotated',
                     'matched', 'forecast', 'failed', 'delivered'));
