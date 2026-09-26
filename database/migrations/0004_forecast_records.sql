-- 0004_forecast_records.sql
-- Versioned forecast records for the v2 feature contract (nq_features_v2).
--
-- The market-data store (contracts, session_days, bars, active_contracts,
-- asset_sources) is unchanged. The new records live in their own schema,
-- "forecast", so they can carry the contract's names without colliding with the
-- v1 tables of the same name in "public" (feature_snapshots, predictions,
-- outcomes), which keep serving the LLM/analogue pipeline and the dashboard.
--
--   forecast.feature_snapshots  one immutable snapshot per instrument, session,
--                               cutoff, feature version and source revision
--   forecast.forecast_runs      one row per model execution on a snapshot
--   forecast.predictions        one row per (forecast_run_id, target_id)
--   forecast.realised_outcomes  one row per (snapshot_id, label_version,
--                               target_id, outcome_revision)
--   forecast.outcome_metrics    one row per (snapshot_id, metric_version,
--                               outcome_revision)
--
-- plus the registries that make them interpretable (feature, label and model
-- versions; source revisions). Every one of these tables is append-only: a
-- trigger rejects UPDATE, DELETE and TRUNCATE. A correction is a new row - a new
-- snapshot (new source revision, optionally pointing at the one it supersedes),
-- a new forecast run, or the next outcome_revision.
--
-- Economic-event calendars are source data and go in "public" beside the bars.

CREATE SCHEMA IF NOT EXISTS forecast;

-- ---------------------------------------------------------------------------
-- Market data: bar interval bounds, and the bar-labelling convention
-- ---------------------------------------------------------------------------

COMMENT ON COLUMN bars.timestamp_utc IS
    'bar_start_at: the start of the bar''s interval [start, start + interval), in UTC. '
    'IB labels bars by their start, so nothing is converted; a provider that labels by '
    'end time must be shifted at ingestion. The v2 contract names a bar by its start: '
    '"the 09:28 close" is the bar starting 09:28 ET, which ends at 09:29.';

-- bars with explicit interval bounds. bar_start_at is timestamp_utc itself, so a
-- filter on it still lets TimescaleDB exclude chunks.
CREATE VIEW bar_intervals AS
SELECT b.contract_id, b.interval, b.price_type, b.trading_day,
       b.timestamp_utc AS bar_start_at,
       b.timestamp_utc + CASE b.interval
           WHEN '1m' THEN INTERVAL '1 minute'   WHEN '5m'  THEN INTERVAL '5 minutes'
           WHEN '15m' THEN INTERVAL '15 minutes' WHEN '30m' THEN INTERVAL '30 minutes'
           WHEN '1h' THEN INTERVAL '1 hour' END AS bar_end_at,
       b.open, b.high, b.low, b.close, b.volume, b.wap, b.bar_count, b.source, b.is_completed
  FROM bars b;

-- ---------------------------------------------------------------------------
-- Economic-event calendar (source data; optional)
-- ---------------------------------------------------------------------------
-- The event features are computed only for a session a coverage row vouches
-- for: without one, "no event" and "no calendar" are indistinguishable, so the
-- features stay null. recorded_at is when the pipeline learned of the row, which
-- is the point-in-time evidence for a live capture.

CREATE TABLE economic_event_coverage (
    source       TEXT NOT NULL,
    covered_from DATE NOT NULL,
    covered_to   DATE NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes        TEXT,
    PRIMARY KEY (source, covered_from, covered_to),
    CHECK (covered_from <= covered_to)
);

CREATE TABLE economic_events (
    source       TEXT NOT NULL,
    event_key    TEXT NOT NULL,                  -- the source's own identifier
    scheduled_at TIMESTAMPTZ NOT NULL,
    name         TEXT NOT NULL,
    country      TEXT,
    tier         TEXT NOT NULL,                  -- low | moderate | high
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, event_key, scheduled_at),
    CHECK (tier IN ('low', 'moderate', 'high'))
);

CREATE INDEX idx_economic_events_time ON economic_events (scheduled_at);

-- ---------------------------------------------------------------------------
-- Append-only guard
-- ---------------------------------------------------------------------------

CREATE FUNCTION forecast.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'forecast.% is append-only: % rejected. Record a correction as a new row '
                    '(new snapshot / run / outcome_revision) instead.', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

-- 09:30 ET on a session date, as UTC: the live-forecast deadline.
CREATE FUNCTION forecast.rth_open_at(d DATE) RETURNS TIMESTAMPTZ
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT (d + TIME '09:30') AT TIME ZONE 'America/New_York'
$$;

-- ---------------------------------------------------------------------------
-- Registries
-- ---------------------------------------------------------------------------

CREATE TABLE forecast.feature_versions (
    feature_version TEXT PRIMARY KEY,
    definition_hash TEXT NOT NULL,     -- hash of parameters + every definition row
    parameters      JSONB NOT NULL,    -- cutoff, calendar, roll policy, warm-up, thresholds
    description     TEXT NOT NULL,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(parameters) = 'object')
);

CREATE TABLE forecast.feature_definitions (
    feature_version  TEXT NOT NULL REFERENCES forecast.feature_versions (feature_version),
    feature_name     TEXT NOT NULL,
    family           TEXT NOT NULL,    -- nq | intermarket | intermarket_optional | calendar | event
    data_type        TEXT NOT NULL,    -- float | integer | boolean | categorical
    unit             TEXT,
    allowed_values   TEXT[],
    lower_bound      DOUBLE PRECISION,
    upper_bound      DOUBLE PRECISION,
    default_required BOOLEAN NOT NULL,
    definition       TEXT NOT NULL,
    PRIMARY KEY (feature_version, feature_name),
    CHECK (data_type IN ('float', 'integer', 'boolean', 'categorical')),
    CHECK (data_type <> 'categorical' OR allowed_values IS NOT NULL)
);

CREATE TABLE forecast.label_versions (
    label_version   TEXT PRIMARY KEY,
    definition_hash TEXT NOT NULL,
    description     TEXT NOT NULL,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The label vocabulary of a target. Predictions and realised outcomes are both
-- validated against this one row, so their vocabularies are identical by
-- construction.
CREATE TABLE forecast.label_definitions (
    label_version TEXT NOT NULL REFERENCES forecast.label_versions (label_version),
    target_id     TEXT NOT NULL,
    labels        TEXT[] NOT NULL,
    definition    TEXT NOT NULL,
    parameters    JSONB NOT NULL,
    PRIMARY KEY (label_version, target_id),
    CHECK (cardinality(labels) >= 2)
);

CREATE TABLE forecast.model_versions (
    model_version     TEXT PRIMARY KEY,
    feature_version   TEXT NOT NULL REFERENCES forecast.feature_versions (feature_version),
    label_version     TEXT NOT NULL REFERENCES forecast.label_versions (label_version),
    target_ids        TEXT[] NOT NULL,
    required_features TEXT[] NOT NULL,   -- governs a run's input_quality_status
    description       TEXT NOT NULL,
    parameters        JSONB NOT NULL,
    definition_hash   TEXT NOT NULL,
    registered_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Content-addressed: the id is the hash of the manifest (per-source contract and
-- digest of every bar window read, calendar version, roll policy, source map).
CREATE TABLE forecast.source_revisions (
    source_revision_id TEXT PRIMARY KEY,
    manifest           JSONB NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(manifest) = 'object')
);

-- ---------------------------------------------------------------------------
-- feature_snapshots
-- ---------------------------------------------------------------------------

CREATE TABLE forecast.feature_snapshots (
    snapshot_id             UUID PRIMARY KEY,
    session_date            DATE NOT NULL,
    instrument_id           BIGINT NOT NULL REFERENCES contracts (contract_id),
    cutoff_at               TIMESTAMPTZ NOT NULL,
    features_frozen_at      TIMESTAMPTZ NOT NULL,
    data_mode               TEXT NOT NULL,
    pit_availability_status TEXT NOT NULL,
    feature_version         TEXT NOT NULL REFERENCES forecast.feature_versions (feature_version),
    source_revision_id      TEXT NOT NULL REFERENCES forecast.source_revisions (source_revision_id),
    session_schedule        TEXT NOT NULL,
    scheduled_close_at      TIMESTAMPTZ,
    source_status           JSONB NOT NULL,
    feature_status          JSONB NOT NULL,
    data_quality_status     TEXT NOT NULL,   -- against the feature version's default required set
    reference_values        JSONB NOT NULL,  -- P, Cprev, PDH, PDL, A, ONH, ONL...: metadata, not features
    features                JSONB NOT NULL,  -- {feature_name: value | null}
    supersedes_snapshot_id  UUID REFERENCES forecast.feature_snapshots (snapshot_id),
    correction_reason       TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (instrument_id, session_date, cutoff_at, feature_version, source_revision_id),
    CHECK (data_mode IN ('live_capture', 'historical_reconstruction')),
    CHECK (pit_availability_status IN ('verified', 'unverified_historical')),
    CHECK (session_schedule IN ('full', 'early_close', 'closed')),
    CHECK (data_quality_status IN ('valid', 'partial', 'invalid')),
    CHECK ((session_schedule = 'closed') = (scheduled_close_at IS NULL)),
    CHECK (features_frozen_at >= cutoff_at),
    -- A live capture is frozen before the open it forecasts.
    CHECK (data_mode <> 'live_capture' OR features_frozen_at < forecast.rth_open_at(session_date)),
    CHECK (jsonb_typeof(features) = 'object'),
    CHECK (jsonb_typeof(feature_status) = 'object'),
    CHECK (jsonb_typeof(source_status) = 'object'),
    CHECK (jsonb_typeof(reference_values) = 'object'),
    CHECK ((supersedes_snapshot_id IS NULL) = (correction_reason IS NULL))
);

CREATE INDEX idx_fc_snapshots_session ON forecast.feature_snapshots (session_date, feature_version);

-- ---------------------------------------------------------------------------
-- forecast_runs + predictions
-- ---------------------------------------------------------------------------

CREATE TABLE forecast.forecast_runs (
    forecast_run_id      UUID PRIMARY KEY,
    snapshot_id          UUID NOT NULL REFERENCES forecast.feature_snapshots (snapshot_id),
    model_version        TEXT NOT NULL REFERENCES forecast.model_versions (model_version),
    label_version        TEXT NOT NULL REFERENCES forecast.label_versions (label_version),
    generated_at         TIMESTAMPTZ NOT NULL,
    calibration          JSONB NOT NULL,     -- calibration provenance
    input_quality_status TEXT NOT NULL,      -- against the model's required_features
    code_revision        TEXT,
    supersedes_run_id    UUID REFERENCES forecast.forecast_runs (forecast_run_id),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (input_quality_status IN ('valid', 'partial', 'invalid')),
    CHECK (jsonb_typeof(calibration) = 'object')
);

CREATE INDEX idx_fc_runs_snapshot ON forecast.forecast_runs (snapshot_id);

CREATE FUNCTION forecast.check_forecast_run() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    snap forecast.feature_snapshots%ROWTYPE;
    model forecast.model_versions%ROWTYPE;
BEGIN
    SELECT * INTO snap FROM forecast.feature_snapshots WHERE snapshot_id = NEW.snapshot_id;
    SELECT * INTO model FROM forecast.model_versions WHERE model_version = NEW.model_version;
    IF model.label_version <> NEW.label_version THEN
        RAISE EXCEPTION 'run label_version % does not match model % (%)',
            NEW.label_version, NEW.model_version, model.label_version;
    END IF;
    IF model.feature_version <> snap.feature_version THEN
        RAISE EXCEPTION 'model % expects feature_version %, snapshot has %',
            NEW.model_version, model.feature_version, snap.feature_version;
    END IF;
    IF NEW.generated_at < snap.features_frozen_at THEN
        RAISE EXCEPTION 'generated_at % precedes features_frozen_at %',
            NEW.generated_at, snap.features_frozen_at;
    END IF;
    IF snap.data_mode = 'live_capture'
       AND NEW.generated_at >= forecast.rth_open_at(snap.session_date) THEN
        RAISE EXCEPTION 'a live forecast must be generated before 09:30 ET (generated_at %)',
            NEW.generated_at;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER forecast_runs_check BEFORE INSERT ON forecast.forecast_runs
    FOR EACH ROW EXECUTE FUNCTION forecast.check_forecast_run();

CREATE TABLE forecast.predictions (
    forecast_run_id   UUID NOT NULL REFERENCES forecast.forecast_runs (forecast_run_id),
    target_id         TEXT NOT NULL,
    predicted_label   TEXT,
    probabilities     JSONB,            -- {label: probability} over the full vocabulary
    abstained         BOOLEAN NOT NULL,
    abstention_reason TEXT,
    PRIMARY KEY (forecast_run_id, target_id),
    CHECK (abstained = (predicted_label IS NULL)),
    CHECK (abstained = (abstention_reason IS NOT NULL)),
    CHECK (abstained OR probabilities IS NOT NULL)
);

-- The label and every probability key must come from the target's vocabulary,
-- the distribution must cover all of it and sum to 1.
CREATE FUNCTION forecast.check_prediction() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    run forecast.forecast_runs%ROWTYPE;
    vocab TEXT[];
    total DOUBLE PRECISION := 0;
    k TEXT;
    v JSONB;
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
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER predictions_check BEFORE INSERT ON forecast.predictions
    FOR EACH ROW EXECUTE FUNCTION forecast.check_prediction();

-- ---------------------------------------------------------------------------
-- outcome_metrics + realised_outcomes
-- ---------------------------------------------------------------------------

CREATE TABLE forecast.outcome_metrics (
    snapshot_id           UUID NOT NULL REFERENCES forecast.feature_snapshots (snapshot_id),
    metric_version        TEXT NOT NULL,
    outcome_revision      INTEGER NOT NULL,
    metrics               JSONB NOT NULL,   -- continuous returns, ranges, excursions, path
    metric_status         JSONB NOT NULL,
    available_at          TIMESTAMPTZ NOT NULL,  -- market time the last input bar ended
    computed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome_source_digest TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, metric_version, outcome_revision),
    CHECK (outcome_revision >= 1),
    CHECK (jsonb_typeof(metrics) = 'object'),
    CHECK (jsonb_typeof(metric_status) = 'object')
);

CREATE TABLE forecast.realised_outcomes (
    snapshot_id             UUID NOT NULL REFERENCES forecast.feature_snapshots (snapshot_id),
    label_version           TEXT NOT NULL,
    target_id               TEXT NOT NULL,
    outcome_revision        INTEGER NOT NULL,
    actual_label            TEXT,
    eligible                BOOLEAN NOT NULL,
    ineligibility_reason    TEXT,
    available_at            TIMESTAMPTZ NOT NULL,  -- market time the label became knowable
    computed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome_source_digest   TEXT NOT NULL,
    metric_version          TEXT,
    metric_outcome_revision INTEGER,
    PRIMARY KEY (snapshot_id, label_version, target_id, outcome_revision),
    FOREIGN KEY (label_version, target_id)
        REFERENCES forecast.label_definitions (label_version, target_id),
    FOREIGN KEY (snapshot_id, metric_version, metric_outcome_revision)
        REFERENCES forecast.outcome_metrics (snapshot_id, metric_version, outcome_revision),
    CHECK (outcome_revision >= 1),
    CHECK (eligible = (actual_label IS NOT NULL)),
    CHECK (eligible = (ineligibility_reason IS NULL))
);

CREATE FUNCTION forecast.check_realised_outcome() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    vocab TEXT[];
BEGIN
    SELECT labels INTO vocab FROM forecast.label_definitions
     WHERE label_version = NEW.label_version AND target_id = NEW.target_id;
    IF NEW.actual_label IS NOT NULL AND NOT NEW.actual_label = ANY (vocab) THEN
        RAISE EXCEPTION 'label % is not in the vocabulary % of %', NEW.actual_label, vocab, NEW.target_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER realised_outcomes_check BEFORE INSERT ON forecast.realised_outcomes
    FOR EACH ROW EXECUTE FUNCTION forecast.check_realised_outcome();

-- ---------------------------------------------------------------------------
-- Append-only enforcement on every forecast table
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'feature_versions', 'feature_definitions', 'label_versions', 'label_definitions',
        'model_versions', 'source_revisions', 'feature_snapshots', 'forecast_runs',
        'predictions', 'outcome_metrics', 'realised_outcomes'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON forecast.%I '
            'FOR EACH ROW EXECUTE FUNCTION forecast.reject_mutation();', t || '_append_only', t);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE TRUNCATE ON forecast.%I '
            'FOR EACH STATEMENT EXECUTE FUNCTION forecast.reject_mutation();', t || '_no_truncate', t);
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------

-- Every prediction joined to its realised outcome through snapshot_id, target_id
-- and the run's label_version. One row per available outcome_revision: callers
-- must select an explicit revision (database/forecast_store.py requires one).
CREATE VIEW forecast.prediction_outcomes AS
SELECT r.forecast_run_id, r.model_version, r.label_version, r.generated_at,
       r.input_quality_status,
       s.snapshot_id, s.session_date, s.instrument_id, s.data_mode, s.pit_availability_status,
       s.feature_version,
       p.target_id, p.predicted_label, p.probabilities, p.abstained, p.abstention_reason,
       o.outcome_revision, o.actual_label, o.eligible, o.ineligibility_reason,
       o.available_at AS outcome_available_at, o.computed_at AS outcome_computed_at
  FROM forecast.predictions p
  JOIN forecast.forecast_runs r ON r.forecast_run_id = p.forecast_run_id
  JOIN forecast.feature_snapshots s ON s.snapshot_id = r.snapshot_id
  JOIN forecast.realised_outcomes o
    ON o.snapshot_id = r.snapshot_id
   AND o.target_id = p.target_id
   AND o.label_version = r.label_version;

-- The nq_features_v2 predictive matrix with typed columns. Metadata columns are
-- the leading ones; the model-facing features are everything from
-- daily_atr_fraction onward. tests/test_forecast_store.py keeps this column list
-- equal to the catalogue in features/catalogue.py.
CREATE VIEW forecast.feature_matrix_nq_v2 AS
SELECT s.snapshot_id, s.session_date, s.instrument_id, s.cutoff_at, s.features_frozen_at,
       s.data_mode, s.pit_availability_status, s.source_revision_id, s.data_quality_status,
       f.*
  FROM forecast.feature_snapshots s,
       jsonb_to_record(s.features) AS f(
           daily_atr_fraction DOUBLE PRECISION,
           daily_volatility_ratio DOUBLE PRECISION,
           atr_1m_14_fraction DOUBLE PRECISION,
           atr_1m_14_relative_30d DOUBLE PRECISION,
           gap_signed_atr DOUBLE PRECISION,
           prior_range_position DOUBLE PRECISION,
           distance_pdh_atr DOUBLE PRECISION,
           distance_pdl_atr DOUBLE PRECISION,
           distance_onh_atr DOUBLE PRECISION,
           distance_onl_atr DOUBLE PRECISION,
           overnight_range_atr DOUBLE PRECISION,
           overnight_range_position DOUBLE PRECISION,
           distance_on_vwap_hlc3_atr DOUBLE PRECISION,
           return_15m_atr DOUBLE PRECISION,
           return_60m_atr DOUBLE PRECISION,
           range_60m_atr DOUBLE PRECISION,
           efficiency_60m DOUBLE PRECISION,
           ema200_distance_5m_atr DOUBLE PRECISION,
           ema9_21_spread_5m_atr DOUBLE PRECISION,
           ema20_slope_15m_atr DOUBLE PRECISION,
           rvol_overnight_30d DOUBLE PRECISION,
           rvol_60m_30d DOUBLE PRECISION,
           prior_rth_return_atr DOUBLE PRECISION,
           prior_rth_range_atr DOUBLE PRECISION,
           prior_rth_close_location DOUBLE PRECISION,
           nq_preopen_return DOUBLE PRECISION,
           es_preopen_return DOUBLE PRECISION,
           rty_preopen_return DOUBLE PRECISION,
           nq_es_relative_return DOUBLE PRECISION,
           nq_es_standardized_divergence DOUBLE PRECISION,
           nq_es_relative_return_60m DOUBLE PRECISION,
           vix_level DOUBLE PRECISION,
           vix_change_points DOUBLE PRECISION,
           vxn_level DOUBLE PRECISION,
           vxn_change_points DOUBLE PRECISION,
           us10y_change_bps DOUBLE PRECISION,
           us2y_change_bps DOUBLE PRECISION,
           yield_curve_10y_2y_change_bps DOUBLE PRECISION,
           dxy_preopen_return DOUBLE PRECISION,
           smh_preopen_return DOUBLE PRECISION,
           dx_fut_preopen_return DOUBLE PRECISION,
           us10y_yield_fut_change_bps DOUBLE PRECISION,
           us2y_yield_fut_change_bps DOUBLE PRECISION,
           yield_fut_curve_10y_2y_change_bps DOUBLE PRECISION,
           gc_preopen_return DOUBLE PRECISION,
           cl_preopen_return DOUBLE PRECISION,
           weekday TEXT,
           monthly_opex_week BOOLEAN,
           days_to_nq_expiry INTEGER,
           roll_transition BOOLEAN,
           is_early_close BOOLEAN,
           remaining_event_risk TEXT,
           has_future_high_event BOOLEAN,
           minutes_to_high_event DOUBLE PRECISION
       )
 WHERE s.feature_version = 'nq_features_v2';

-- ---------------------------------------------------------------------------
-- The v1 records stay, labelled as such
-- ---------------------------------------------------------------------------

COMMENT ON TABLE feature_snapshots IS
    'v1 (legacy) pre-open snapshots of the LLM/analogue pipeline: frozen at 09:30 ET using '
    'the 09:30 opening bar, and overwritten on re-run. The v2 contract lives in '
    'forecast.feature_snapshots.';
COMMENT ON TABLE predictions IS
    'v1 (legacy) LLM/analogue forecasts. v2 runs and predictions live in forecast.forecast_runs '
    'and forecast.predictions.';
COMMENT ON TABLE outcomes IS
    'v1 (legacy) realised session levels, overwritten on re-run. v2: forecast.outcome_metrics '
    'and forecast.realised_outcomes, revisioned.';
