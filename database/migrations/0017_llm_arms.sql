-- 0017_llm_arms.sql
-- Guideline revision 2, stage 4B arms C and D (forecaster/llm_arms.py).
--
--   arm C   the restricted Claude annotation (nq_structure_restricted_v1: Claude owns
--           Overnight Structure and Premarket Pattern, the rules the rest) matched and
--           smoothed like arm B - its annotations, attempts and requests use the
--           existing tables, nothing new is needed
--   arm D   the forecast synthesis (Appendix A, A2): Claude's own distributions from
--           arm B's frozen evidence, stored as a forecast run
--
--   journal.forecast_predictions.estimation_status   'judgement': a distribution the
--                                    synthesis judged from the evidence, not estimated
--                                    from analogue counts
--   journal.forecast_runs.request_id the inference request a synthesis run answers (one
--                                    run per request); a request with such a run is
--                                    resolved, like one with an annotation attempt
--
-- Append-only, like the rest of the journal.

ALTER TABLE journal.forecast_predictions DROP CONSTRAINT forecast_predictions_estimation_status_check;
ALTER TABLE journal.forecast_predictions ADD CONSTRAINT forecast_predictions_estimation_status_check
    CHECK (estimation_status IN ('analogues', 'prior_only', 'none', 'judgement'));

ALTER TABLE journal.forecast_runs ADD COLUMN request_id UUID REFERENCES journal.inference_requests (request_id);
CREATE UNIQUE INDEX forecast_runs_one_per_request ON journal.forecast_runs (request_id) WHERE request_id IS NOT NULL;

COMMENT ON COLUMN journal.forecast_runs.request_id IS
    'the inference request a synthesis (arm D) run answers; null for a deterministic run';
