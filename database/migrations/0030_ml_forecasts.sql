-- 0030_ml_forecasts.sql
-- The scikit-learn forecaster of NQ's direction_15m (contracts/nq_ml.py,
-- forecaster/ml_service.py).
--
--   definition_versions.kind       'ml_features': the feature definitions an ML forecast reads
--                                  (contracts/nq_ml.features_record); its algorithms are
--                                  forecast_algorithm definitions naming their artifact's sha256
--   forecast_predictions.estimation_status
--                                  'model': a distribution from a trained model, not counted
--                                  from analogues
--   journal.forecast_deliveries    the forecast in force for a session, profile and mode: the
--                                  first usable run in the delivery order, or none, with the
--                                  reason any earlier one was passed over - recorded when it is
--                                  decided, stamped by the database clock (forecaster/delivery.py).
--                                  Append-only, like the rest of the journal.

ALTER TABLE journal.definition_versions DROP CONSTRAINT definition_versions_kind_check;
ALTER TABLE journal.definition_versions ADD CONSTRAINT definition_versions_kind_check
    CHECK (kind IN ('labels', 'convention', 'snapshot', 'annotation', 'matcher', 'forecast_schema',
                    'forecast_algorithm', 'issue_policy', 'experiment', 'fan_experiment', 'fan_model', 'fan_forward',
                    'rth_matcher', 'rth_evaluation', 'ml_features'));

ALTER TABLE journal.forecast_predictions DROP CONSTRAINT forecast_predictions_estimation_status_check;
ALTER TABLE journal.forecast_predictions ADD CONSTRAINT forecast_predictions_estimation_status_check
    CHECK (estimation_status IN ('analogues', 'prior_only', 'none', 'model'));

CREATE TABLE journal.forecast_deliveries (
    delivery_id     BIGSERIAL PRIMARY KEY,
    session_date    DATE NOT NULL,
    profile         TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('historical_replay', 'live')),
    run_id          UUID REFERENCES journal.forecast_runs (run_id),
    algorithm       TEXT REFERENCES journal.definition_versions (version),
    delivery_order  JSONB NOT NULL,
    reason          TEXT NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK ((run_id IS NULL) = (algorithm IS NULL))
);

CREATE INDEX idx_forecast_deliveries_session ON journal.forecast_deliveries (session_date, profile, mode, decided_at);

CREATE FUNCTION journal.stamp_delivery() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.decided_at := clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE TRIGGER forecast_deliveries_stamp BEFORE INSERT ON journal.forecast_deliveries
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_delivery();
CREATE TRIGGER forecast_deliveries_append_only BEFORE UPDATE OR DELETE ON journal.forecast_deliveries
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER forecast_deliveries_no_truncate BEFORE TRUNCATE ON journal.forecast_deliveries
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
