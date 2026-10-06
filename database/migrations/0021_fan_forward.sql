-- 0021_fan_forward.sql
-- The forward record of the intermarket fan experiment (docs/fan_experiment.md, chunk 8):
-- the frozen fan_model and its baseline issued live from what the store held at each
-- issue time, every 15 minutes of the trading day and at the 09:29 cutoff
-- (forecaster/fan_forward.py), and scored once each outcome is final.
--
--   journal.fan_forward_runs     one row per attempt to issue: issued, or why not
--                                (stale data, late, a duplicate, a closed market, failed)
--   journal.fan_forward_shapes   the baseline's standardised shape per session and
--                                horizon (the 200 levels the CRPS reads), once
--   journal.fan_forward_issues   one row per issued forecast: the model, the origin and
--                                its price as read, every input the forecast read with its
--                                freshness, and every horizon's sigma and multiplier
--   journal.fan_forward_scores   one row per issue and horizon, once the session is final:
--                                the realised move from the origin price as read, both
--                                CRPS, both PITs, the origin bar's later revision
--
-- Times are the database's; append-only, like the rest of the journal.

CREATE TABLE journal.fan_forward_runs (
    run_id        UUID PRIMARY KEY,
    model         TEXT NOT NULL REFERENCES journal.definition_versions (version),
    mark_at       TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('issued', 'stale', 'late', 'duplicate', 'closed', 'failed')),
    issue_id      UUID,
    detail        JSONB,
    code_revision TEXT NOT NULL,
    at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX idx_fan_forward_runs_model ON journal.fan_forward_runs (model, mark_at);

CREATE TABLE journal.fan_forward_shapes (
    shape_id      TEXT PRIMARY KEY,
    baseline      TEXT NOT NULL REFERENCES journal.definition_versions (version),
    target        TEXT NOT NULL,
    session_date  DATE NOT NULL,
    horizons      INTEGER[] NOT NULL,
    quantiles     JSONB NOT NULL,              -- per horizon, the standardised quantiles at tau_k = (k - 1/2) / K
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE journal.fan_forward_issues (
    issue_id       UUID PRIMARY KEY,
    model          TEXT NOT NULL REFERENCES journal.definition_versions (version),
    baseline       TEXT NOT NULL REFERENCES journal.definition_versions (version),
    target         TEXT NOT NULL,
    session_date   DATE NOT NULL,
    origin_slot    INTEGER NOT NULL,
    origin_bar_at  TIMESTAMPTZ NOT NULL,       -- the start of the last closed bar the forecast is centred on
    mark_at        TIMESTAMPTZ NOT NULL,       -- the issue time it belongs to (the bar closes then)
    origin_price   DOUBLE PRECISION NOT NULL,  -- as read
    shape_id       TEXT NOT NULL REFERENCES journal.fan_forward_shapes (shape_id),
    inputs         JSONB NOT NULL,
    forecast       JSONB NOT NULL,             -- per horizon: the baseline's sigma and the model's multiplier
    code_revision  TEXT NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (model, target, session_date, origin_slot)
);

CREATE INDEX idx_fan_forward_issues_session ON journal.fan_forward_issues (session_date, origin_slot);

CREATE TABLE journal.fan_forward_scores (
    issue_id        UUID NOT NULL REFERENCES journal.fan_forward_issues (issue_id),
    horizon         INTEGER NOT NULL,
    outcome_bar_at  TIMESTAMPTZ NOT NULL,
    realised        DOUBLE PRECISION NOT NULL, -- log(final last price at t + h) - log(origin price as read)
    revision        DOUBLE PRECISION NOT NULL, -- log(the origin bar's final close / its close as read)
    crps_base_bps   DOUBLE PRECISION NOT NULL,
    crps_model_bps  DOUBLE PRECISION NOT NULL,
    pit_base        DOUBLE PRECISION NOT NULL,
    pit_model       DOUBLE PRECISION NOT NULL,
    code_revision   TEXT NOT NULL,
    scored_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (issue_id, horizon)
);

CREATE FUNCTION journal.stamp_fan_forward() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'fan_forward_runs' THEN
        NEW.at := clock_timestamp();
    ELSIF TG_TABLE_NAME = 'fan_forward_scores' THEN
        NEW.scored_at := clock_timestamp();
    ELSE
        NEW.recorded_at := clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER fan_forward_runs_stamp BEFORE INSERT ON journal.fan_forward_runs
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_fan_forward();
CREATE TRIGGER fan_forward_shapes_stamp BEFORE INSERT ON journal.fan_forward_shapes
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_fan_forward();
CREATE TRIGGER fan_forward_issues_stamp BEFORE INSERT ON journal.fan_forward_issues
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_fan_forward();
CREATE TRIGGER fan_forward_scores_stamp BEFORE INSERT ON journal.fan_forward_scores
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_fan_forward();

CREATE TRIGGER fan_forward_runs_append_only BEFORE UPDATE OR DELETE ON journal.fan_forward_runs
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER fan_forward_runs_no_truncate BEFORE TRUNCATE ON journal.fan_forward_runs
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER fan_forward_shapes_append_only BEFORE UPDATE OR DELETE ON journal.fan_forward_shapes
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER fan_forward_shapes_no_truncate BEFORE TRUNCATE ON journal.fan_forward_shapes
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER fan_forward_issues_append_only BEFORE UPDATE OR DELETE ON journal.fan_forward_issues
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER fan_forward_issues_no_truncate BEFORE TRUNCATE ON journal.fan_forward_issues
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER fan_forward_scores_append_only BEFORE UPDATE OR DELETE ON journal.fan_forward_scores
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER fan_forward_scores_no_truncate BEFORE TRUNCATE ON journal.fan_forward_scores
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
