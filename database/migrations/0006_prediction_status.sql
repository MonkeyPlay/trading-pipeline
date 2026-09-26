-- 0006_prediction_status.sql
-- nq_schema_v2 section 7 and 9 attributes on the v2 forecast records.
--
--   forecast.forecast_runs     : calibration_version - the calibration model applied,
--                                if any (NULL when the probabilities are raw).
--   forecast.predictions       : prediction_status (issued | abstained | unavailable),
--                                decision_reason and calibration_status. An abstained
--                                prediction may keep its probabilities; an unavailable
--                                one has none. abstained stays TRUE for both. An issued label must be the largest
--                                probability, ties broken by the vocabulary's order.
--   forecast.realised_outcomes : the outcome window [window_start_at, window_end_at).
--                                The label status lives in ineligibility_reason
--                                (NULL = valid), as before.
--
-- Rows written before this migration keep NULL in the new columns; the
-- prediction_outcomes view derives their status from `abstained`. All tables
-- stay append-only: adding a column rewrites no row through the triggers.

ALTER TABLE forecast.forecast_runs ADD COLUMN calibration_version TEXT;

ALTER TABLE forecast.predictions
    ADD COLUMN prediction_status  TEXT,
    ADD COLUMN decision_reason    TEXT,
    ADD COLUMN calibration_status TEXT,
    ADD CONSTRAINT predictions_status_all_or_none CHECK (
        (prediction_status IS NULL) = (decision_reason IS NULL)
        AND (prediction_status IS NULL) = (calibration_status IS NULL)),
    ADD CONSTRAINT predictions_status_vocabulary CHECK (
        prediction_status IN ('issued', 'abstained', 'unavailable')),
    ADD CONSTRAINT predictions_decision_reason_vocabulary CHECK (
        decision_reason IN ('none', 'data_quality', 'uncertainty', 'out_of_distribution',
                            'event_policy', 'late_generation', 'shortened_session')),
    ADD CONSTRAINT predictions_calibration_status_vocabulary CHECK (
        calibration_status IN ('unvalidated', 'validated_raw', 'calibrated')),
    ADD CONSTRAINT predictions_issued_iff_not_abstained CHECK (
        prediction_status IS NULL OR (prediction_status = 'issued') = (NOT abstained)),
    ADD CONSTRAINT predictions_issued_iff_no_reason CHECK (
        prediction_status IS NULL OR (prediction_status = 'issued') = (decision_reason = 'none')),
    ADD CONSTRAINT predictions_unavailable_has_no_probabilities CHECK (
        prediction_status IS DISTINCT FROM 'unavailable' OR probabilities IS NULL);

ALTER TABLE forecast.realised_outcomes
    ADD COLUMN window_start_at TIMESTAMPTZ,
    ADD COLUMN window_end_at   TIMESTAMPTZ,
    ADD CONSTRAINT realised_outcomes_window CHECK (window_start_at < window_end_at);

-- The prediction check of 0004, plus: an issued label is the arg-max of its
-- distribution, ties going to the label earliest in the vocabulary.
CREATE OR REPLACE FUNCTION forecast.check_prediction() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    run forecast.forecast_runs%ROWTYPE;
    vocab TEXT[];
    total DOUBLE PRECISION := 0;
    k TEXT;
    v JSONB;
    best TEXT;
    best_p DOUBLE PRECISION := -1;
    p DOUBLE PRECISION;
BEGIN
    SELECT * INTO run FROM forecast.forecast_runs WHERE forecast_run_id = NEW.forecast_run_id;
    IF NOT EXISTS (SELECT 1 FROM forecast.model_versions
                    WHERE model_version = run.model_version AND NEW.target_id = ANY (target_ids)) THEN
        RAISE EXCEPTION 'target % is not a target of model %', NEW.target_id, run.model_version;
    END IF;
    SELECT labels INTO vocab FROM forecast.label_definitions
     WHERE label_version = run.label_version AND target_id = NEW.target_id;
    IF vocab IS NULL THEN
        RAISE EXCEPTION 'no label definition for (%, %)', run.label_version, NEW.target_id;
    END IF;
    IF NEW.predicted_label IS NOT NULL AND NOT NEW.predicted_label = ANY (vocab) THEN
        RAISE EXCEPTION 'label % is not in the vocabulary % of %', NEW.predicted_label, vocab, NEW.target_id;
    END IF;
    IF NEW.probabilities IS NOT NULL THEN
        IF jsonb_typeof(NEW.probabilities) <> 'object' THEN
            RAISE EXCEPTION 'probabilities must be a JSON object';
        END IF;
        IF (SELECT array_agg(x ORDER BY x) FROM jsonb_object_keys(NEW.probabilities) x)
           IS DISTINCT FROM (SELECT array_agg(x ORDER BY x) FROM unnest(vocab) x) THEN
            RAISE EXCEPTION 'probabilities must cover exactly the vocabulary %', vocab;
        END IF;
        FOR k, v IN SELECT * FROM jsonb_each(NEW.probabilities) LOOP
            IF jsonb_typeof(v) <> 'number' OR v::text::double precision < 0
               OR v::text::double precision > 1 THEN
                RAISE EXCEPTION 'probability of % must be a number in [0, 1]', k;
            END IF;
            total := total + v::text::double precision;
        END LOOP;
        IF abs(total - 1.0) > 1e-6 THEN
            RAISE EXCEPTION 'probabilities sum to %, not 1', total;
        END IF;
        IF NEW.prediction_status = 'issued' THEN
            FOREACH k IN ARRAY vocab LOOP
                p := (NEW.probabilities ->> k)::double precision;
                IF p > best_p THEN
                    best_p := p;
                    best := k;
                END IF;
            END LOOP;
            IF NEW.predicted_label IS DISTINCT FROM best THEN
                RAISE EXCEPTION 'issued label % is not the most probable label % (ties: vocabulary order)',
                    NEW.predicted_label, best;
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

-- 0004's view with the new attributes appended (CREATE OR REPLACE VIEW can only
-- add columns at the end). prediction_status is derived for pre-0006 rows.
CREATE OR REPLACE VIEW forecast.prediction_outcomes AS
SELECT r.forecast_run_id, r.model_version, r.label_version, r.generated_at,
       r.input_quality_status,
       s.snapshot_id, s.session_date, s.instrument_id, s.data_mode, s.pit_availability_status,
       s.feature_version,
       p.target_id, p.predicted_label, p.probabilities, p.abstained, p.abstention_reason,
       o.outcome_revision, o.actual_label, o.eligible, o.ineligibility_reason,
       o.available_at AS outcome_available_at, o.computed_at AS outcome_computed_at,
       COALESCE(p.prediction_status, CASE WHEN p.abstained THEN 'abstained' ELSE 'issued' END)
           AS prediction_status,
       p.decision_reason, p.calibration_status, r.calibration_version,
       COALESCE(o.ineligibility_reason, 'valid') AS label_status,
       o.window_start_at AS outcome_window_start_at, o.window_end_at AS outcome_window_end_at
  FROM forecast.predictions p
  JOIN forecast.forecast_runs r ON r.forecast_run_id = p.forecast_run_id
  JOIN forecast.feature_snapshots s ON s.snapshot_id = r.snapshot_id
  JOIN forecast.realised_outcomes o
    ON o.snapshot_id = r.snapshot_id
   AND o.target_id = p.target_id
   AND o.label_version = r.label_version;
