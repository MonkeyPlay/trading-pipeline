-- 0014_forecast_ledger.sql
-- Guideline revision 2, stage 3: deterministic forecasts on the journal (no
-- recreated forecast schema). Append-only, like the rest of the journal.
--
--   journal.definition_versions     gains the kinds forecast_schema, forecast_algorithm
--                                   and issue_policy (contracts/nq_forecast.py)
--   journal.forecast_runs           one row per forecast attempt - issued, unavailable,
--                                   late, failed or invalid - with its explicit evidence
--                                   ids, versions, code revision and times. issued_at is
--                                   the database clock, set only for an issued run; a
--                                   live run's deadline comes from its registered issue
--                                   policy and session date, never from the client, and
--                                   a live run inserted after it is late. A
--                                   unique idempotency key makes a repeated invocation
--                                   return the stored run; a revision is a new run
--                                   naming the one it supersedes
--   journal.forecast_evidence       the frozen evidence of a run: analogue outcome
--                                   revisions, prior manifest digest and counts,
--                                   thresholds, candidate levels, versions, payload hash
--   journal.forecast_predictions    per target: status, predicted label, the exact
--                                   distribution and its denominators, checked against
--                                   the label version's vocabulary
--   journal.forecast_run_events     acknowledgement after commit, at server time

ALTER TABLE journal.definition_versions DROP CONSTRAINT definition_versions_kind_check;
ALTER TABLE journal.definition_versions ADD CONSTRAINT definition_versions_kind_check
    CHECK (kind IN ('labels', 'convention', 'snapshot', 'annotation', 'matcher', 'forecast_schema',
                    'forecast_algorithm', 'issue_policy'));

CREATE TABLE journal.forecast_runs (
    run_id                  UUID PRIMARY KEY,
    idempotency_key         TEXT NOT NULL UNIQUE,
    symbol                  TEXT NOT NULL,
    session_date            DATE NOT NULL,
    contract_id             BIGINT NOT NULL REFERENCES contracts (contract_id),
    profile                 TEXT NOT NULL,
    snapshot_id             UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    annotation_id           UUID REFERENCES journal.structure_annotations (annotation_id),
    analogue_set_id         UUID REFERENCES journal.analogue_sets (set_id),
    label_version           TEXT NOT NULL REFERENCES journal.definition_versions (version),
    algorithm_version       TEXT NOT NULL REFERENCES journal.definition_versions (version),
    schema_version          TEXT NOT NULL REFERENCES journal.definition_versions (version),
    issue_policy            TEXT NOT NULL REFERENCES journal.definition_versions (version),
    code_revision           TEXT NOT NULL,
    mode                    TEXT NOT NULL CHECK (mode IN ('historical_replay', 'live')),
    input_cutoff_at         TIMESTAMPTZ NOT NULL,
    deadline_at             TIMESTAMPTZ,
    generation_started_at   TIMESTAMPTZ NOT NULL,
    generation_completed_at TIMESTAMPTZ NOT NULL,
    lifecycle_status        TEXT NOT NULL CHECK (lifecycle_status IN ('issued', 'unavailable', 'late', 'failed',
                                                                      'invalid')),
    issued_at               TIMESTAMPTZ,
    supersedes_run_id       UUID REFERENCES journal.forecast_runs (run_id),
    failure_reason          TEXT,
    evidence_digest         TEXT NOT NULL,
    outputs                 JSONB NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK ((lifecycle_status = 'issued') = (issued_at IS NOT NULL)),
    CHECK ((mode = 'live') = (deadline_at IS NOT NULL)),
    CHECK (generation_started_at <= generation_completed_at),
    CHECK (lifecycle_status = 'issued' OR failure_reason IS NOT NULL)
);

CREATE INDEX idx_forecast_runs_session ON journal.forecast_runs (session_date, profile, created_at);

CREATE TABLE journal.forecast_evidence (
    run_id          UUID PRIMARY KEY REFERENCES journal.forecast_runs (run_id),
    evidence        JSONB NOT NULL,
    evidence_digest TEXT NOT NULL
);

CREATE TABLE journal.forecast_predictions (
    run_id              UUID NOT NULL REFERENCES journal.forecast_runs (run_id),
    target              TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('predicted', 'ambiguous_prediction', 'unavailable')),
    predicted_label     TEXT,
    estimation_status   TEXT NOT NULL CHECK (estimation_status IN ('analogues', 'prior_only', 'none')),
    distribution        JSONB,                -- class -> "numerator/denominator", exact
    eligible            INTEGER NOT NULL CHECK (eligible >= 0),
    without_label       INTEGER NOT NULL CHECK (without_label >= 0),
    prior_sessions      INTEGER NOT NULL CHECK (prior_sessions >= 0),
    prior_without_label INTEGER NOT NULL CHECK (prior_without_label >= 0),
    reason              TEXT,
    PRIMARY KEY (run_id, target),
    CHECK ((status = 'predicted') = (predicted_label IS NOT NULL)),
    CHECK (status = 'predicted' OR reason IS NOT NULL)
);

CREATE TABLE journal.forecast_run_events (
    event_id BIGSERIAL PRIMARY KEY,
    run_id   UUID NOT NULL REFERENCES journal.forecast_runs (run_id),
    event    TEXT NOT NULL CHECK (event IN ('acknowledged')),
    at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    detail   TEXT
);

-- A run: its versions are of the right kinds, its evidence ids belong together, a
-- live run is a live capture, and issuance follows the database clock.
CREATE FUNCTION journal.check_forecast_run() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    snap     RECORD;
    ann      RECORD;
    aset     RECORD;
    deadline TEXT;
BEGIN
    IF (SELECT kind FROM journal.definition_versions WHERE version = NEW.label_version) <> 'labels'
       OR (SELECT kind FROM journal.definition_versions WHERE version = NEW.algorithm_version) <> 'forecast_algorithm'
       OR (SELECT kind FROM journal.definition_versions WHERE version = NEW.schema_version) <> 'forecast_schema'
       OR (SELECT kind FROM journal.definition_versions WHERE version = NEW.issue_policy) <> 'issue_policy' THEN
        RAISE EXCEPTION 'forecast run versions are not of their kinds' USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO snap FROM journal.snapshots WHERE snapshot_id = NEW.snapshot_id;
    IF snap.session_date <> NEW.session_date OR snap.symbol <> NEW.symbol OR snap.contract_id <> NEW.contract_id
       OR snap.cutoff_at <> NEW.input_cutoff_at THEN
        RAISE EXCEPTION 'the run does not describe its snapshot' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.annotation_id IS NOT NULL THEN
        SELECT * INTO ann FROM journal.structure_annotations WHERE annotation_id = NEW.annotation_id;
        IF ann.snapshot_id <> NEW.snapshot_id THEN
            RAISE EXCEPTION 'annotation % is not of snapshot %', NEW.annotation_id, NEW.snapshot_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.analogue_set_id IS NOT NULL THEN
        SELECT * INTO aset FROM journal.analogue_sets WHERE set_id = NEW.analogue_set_id;
        IF aset.target_snapshot_id <> NEW.snapshot_id OR aset.target_annotation_id IS DISTINCT FROM NEW.annotation_id
           OR aset.label_version <> NEW.label_version THEN
            RAISE EXCEPTION 'analogue set % does not target this snapshot, annotation and label version',
                NEW.analogue_set_id USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.mode = 'live' AND snap.data_mode <> 'live_capture' THEN
        RAISE EXCEPTION 'a live forecast needs a live_capture snapshot, not %', snap.data_mode
            USING ERRCODE = 'check_violation';
    END IF;
    -- the deadline is the policy's ET time on the session date, whatever the client sent
    IF NEW.mode = 'live' THEN
        SELECT definition ->> 'deadline_et' INTO deadline FROM journal.definition_versions
         WHERE version = NEW.issue_policy;
        IF deadline IS NULL THEN
            RAISE EXCEPTION 'issue policy % has no deadline for a live run', NEW.issue_policy
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.deadline_at := (NEW.session_date + deadline::TIME) AT TIME ZONE 'America/New_York';
    ELSE
        NEW.deadline_at := NULL;
    END IF;
    -- issuance is the database's: a client cannot set issued_at, and a live run after its deadline is late
    IF NEW.lifecycle_status = 'issued' THEN
        IF NEW.mode = 'live' AND clock_timestamp() > NEW.deadline_at THEN
            NEW.lifecycle_status := 'late';
            NEW.issued_at := NULL;
            NEW.failure_reason := format('inserted at %s, after the deadline %s', clock_timestamp(), NEW.deadline_at);
        ELSE
            NEW.issued_at := clock_timestamp();
        END IF;
    ELSE
        NEW.issued_at := NULL;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER forecast_runs_check BEFORE INSERT ON journal.forecast_runs
    FOR EACH ROW EXECUTE FUNCTION journal.check_forecast_run();

-- A prediction: its target and labels are the run's label version's, its distribution
-- covers exactly the target's classes with probabilities in [0, 1] summing to 1.
CREATE FUNCTION journal.check_forecast_prediction() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    def    JSONB;
    labels JSONB;
    total  NUMERIC := 0;
    p      NUMERIC;
    item   RECORD;
BEGIN
    SELECT d.definition INTO def FROM journal.forecast_runs r
      JOIN journal.definition_versions d ON d.version = r.label_version WHERE r.run_id = NEW.run_id;
    labels := def -> 'targets' -> NEW.target -> 'labels';
    IF labels IS NULL THEN
        RAISE EXCEPTION '% is not a target of the run''s label version', NEW.target USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.predicted_label IS NOT NULL AND NOT labels ? NEW.predicted_label THEN
        RAISE EXCEPTION '%: % is not in the vocabulary', NEW.target, NEW.predicted_label
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.distribution IS NOT NULL THEN
        IF (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(NEW.distribution) k)
           IS DISTINCT FROM (SELECT array_agg(v ORDER BY v) FROM jsonb_array_elements_text(labels) v) THEN
            RAISE EXCEPTION '%: the distribution must cover exactly the target''s classes', NEW.target
                USING ERRCODE = 'check_violation';
        END IF;
        FOR item IN SELECT * FROM jsonb_each_text(NEW.distribution) LOOP
            IF item.value !~ '^[0-9]+/[1-9][0-9]*$' THEN
                RAISE EXCEPTION '%: % is not an exact fraction', NEW.target, item.value
                    USING ERRCODE = 'check_violation';
            END IF;
            p := split_part(item.value, '/', 1)::NUMERIC / split_part(item.value, '/', 2)::NUMERIC;
            IF p < 0 OR p > 1 THEN
                RAISE EXCEPTION '%: probability % outside [0, 1]', NEW.target, item.value
                    USING ERRCODE = 'check_violation';
            END IF;
            total := total + p;
        END LOOP;
        IF abs(total - 1) > 1e-12 THEN
            RAISE EXCEPTION '%: the probabilities sum to %, not 1', NEW.target, total USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER forecast_predictions_check BEFORE INSERT ON journal.forecast_predictions
    FOR EACH ROW EXECUTE FUNCTION journal.check_forecast_prediction();

-- Events are stamped by the database clock, whatever the client sends.
CREATE FUNCTION journal.stamp_forecast_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.at := clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE TRIGGER forecast_run_events_stamp BEFORE INSERT ON journal.forecast_run_events
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_forecast_event();

CREATE TRIGGER forecast_runs_append_only BEFORE UPDATE OR DELETE ON journal.forecast_runs
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER forecast_runs_no_truncate BEFORE TRUNCATE ON journal.forecast_runs
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER forecast_evidence_append_only BEFORE UPDATE OR DELETE ON journal.forecast_evidence
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER forecast_evidence_no_truncate BEFORE TRUNCATE ON journal.forecast_evidence
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER forecast_predictions_append_only BEFORE UPDATE OR DELETE ON journal.forecast_predictions
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER forecast_predictions_no_truncate BEFORE TRUNCATE ON journal.forecast_predictions
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER forecast_run_events_append_only BEFORE UPDATE OR DELETE ON journal.forecast_run_events
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER forecast_run_events_no_truncate BEFORE TRUNCATE ON journal.forecast_run_events
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
