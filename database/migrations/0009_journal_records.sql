-- 0009_journal_records.sql
-- Versioned pre-open evidence snapshots and realised outcomes for the NQ
-- prompt-v2 implementation (docs/nq_prompt_v2.md), in their own schema.
--
--   journal.definition_versions : immutable registry of label, convention and
--                                 snapshot definitions, each with a definition
--                                 hash (contracts/nq_prompt_v2.py)
--   journal.snapshots           : one frozen evidence package per session,
--                                 profile and source payload, archiving the
--                                 candles and records it was computed from
--   journal.outcomes            : realised labels and measurements of a snapshot
--                                 under a label version; a recomputation that
--                                 differs becomes the next outcome_revision
--
-- All three are append-only: UPDATE, DELETE and TRUNCATE are rejected.

CREATE SCHEMA journal;

CREATE FUNCTION journal.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'journal.% is append-only: % rejected. Record a new version, snapshot or '
                    'outcome_revision instead.', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TABLE journal.definition_versions (
    version         TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('labels', 'convention', 'snapshot')),
    definition_hash TEXT NOT NULL,
    definition      JSONB NOT NULL,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE journal.snapshots (
    snapshot_id             UUID PRIMARY KEY,
    symbol                  TEXT NOT NULL,
    contract_id             BIGINT NOT NULL REFERENCES contracts (contract_id),
    session_date            DATE NOT NULL,
    snapshot_version        TEXT NOT NULL REFERENCES journal.definition_versions (version),
    convention_version      TEXT NOT NULL REFERENCES journal.definition_versions (version),
    cutoff_at               TIMESTAMPTZ NOT NULL,
    rth_open_at             TIMESTAMPTZ NOT NULL,
    data_mode               TEXT NOT NULL,
    pit_availability_status TEXT NOT NULL,
    source_payload_hash     TEXT NOT NULL,
    payload                 JSONB NOT NULL,
    built_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    supersedes_snapshot_id  UUID REFERENCES journal.snapshots (snapshot_id),
    CHECK (cutoff_at < rth_open_at),
    CHECK (data_mode IN ('live_capture', 'historical_reconstruction', 'historical_as_observed')),
    CHECK (pit_availability_status IN ('verified', 'unverified_historical')),
    -- only a live capture can be verified, and a live capture exists only before the open
    CHECK (pit_availability_status = 'unverified_historical' OR data_mode = 'live_capture'),
    CHECK (data_mode <> 'live_capture' OR built_at < rth_open_at),
    UNIQUE (contract_id, session_date, snapshot_version, convention_version, source_payload_hash)
);

CREATE INDEX idx_journal_snapshots_session ON journal.snapshots (session_date, snapshot_version);

CREATE TABLE journal.outcomes (
    snapshot_id      UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    label_version    TEXT NOT NULL REFERENCES journal.definition_versions (version),
    outcome_revision INTEGER NOT NULL CHECK (outcome_revision >= 1),
    labels           JSONB NOT NULL,      -- {target: {"label", "reason", "detail"}}
    measurements     JSONB NOT NULL,
    source_digest    TEXT NOT NULL,       -- hash of the realised bars read
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id, label_version, outcome_revision)
);

-- Kinds of the referenced versions, and every outcome label against the
-- registered vocabulary: exactly the version's targets, each a label from its
-- vocabulary or None with a registered reason.
CREATE FUNCTION journal.check_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT kind FROM journal.definition_versions WHERE version = NEW.snapshot_version) <> 'snapshot' THEN
        RAISE EXCEPTION '% is not a snapshot version', NEW.snapshot_version USING ERRCODE = 'check_violation';
    END IF;
    IF (SELECT kind FROM journal.definition_versions WHERE version = NEW.convention_version) <> 'convention' THEN
        RAISE EXCEPTION '% is not a convention version', NEW.convention_version USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION journal.check_outcome() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    def    JSONB;
    target TEXT;
    item   JSONB;
BEGIN
    SELECT definition INTO def FROM journal.definition_versions
     WHERE version = NEW.label_version AND kind = 'labels';
    IF def IS NULL THEN
        RAISE EXCEPTION '% is not a label version', NEW.label_version USING ERRCODE = 'check_violation';
    END IF;
    IF (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(NEW.labels) k)
       IS DISTINCT FROM (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(def -> 'targets') k) THEN
        RAISE EXCEPTION 'outcome targets must be exactly those of %', NEW.label_version
            USING ERRCODE = 'check_violation';
    END IF;
    FOR target, item IN SELECT * FROM jsonb_each(NEW.labels) LOOP
        IF jsonb_typeof(item -> 'label') = 'string' THEN
            IF NOT (def -> 'targets' -> target -> 'labels') ? (item ->> 'label') THEN
                RAISE EXCEPTION '%: % is not in the vocabulary of %', target, item ->> 'label', NEW.label_version
                    USING ERRCODE = 'check_violation';
            END IF;
            IF jsonb_typeof(item -> 'reason') = 'string' THEN
                RAISE EXCEPTION '%: a label carries no reason', target USING ERRCODE = 'check_violation';
            END IF;
        ELSIF NOT (def -> 'reasons') ? coalesce(item ->> 'reason', '') THEN
            RAISE EXCEPTION '%: an unavailable label needs a registered reason, got %', target, item ->> 'reason'
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER snapshots_check BEFORE INSERT ON journal.snapshots
    FOR EACH ROW EXECUTE FUNCTION journal.check_snapshot();
CREATE TRIGGER outcomes_check BEFORE INSERT ON journal.outcomes
    FOR EACH ROW EXECUTE FUNCTION journal.check_outcome();

CREATE TRIGGER definition_versions_append_only BEFORE UPDATE OR DELETE ON journal.definition_versions
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER definition_versions_no_truncate BEFORE TRUNCATE ON journal.definition_versions
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER snapshots_append_only BEFORE UPDATE OR DELETE ON journal.snapshots
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER snapshots_no_truncate BEFORE TRUNCATE ON journal.snapshots
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER outcomes_append_only BEFORE UPDATE OR DELETE ON journal.outcomes
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER outcomes_no_truncate BEFORE TRUNCATE ON journal.outcomes
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();

-- The latest outcome revision of every snapshot and label version, one row per target.
CREATE VIEW journal.outcome_labels AS
SELECT s.session_date, s.symbol, s.contract_id, s.snapshot_version, o.snapshot_id, o.label_version,
       o.outcome_revision, o.computed_at, t.key AS target_id,
       t.value ->> 'label' AS label, t.value ->> 'reason' AS reason, t.value ->> 'detail' AS detail
  FROM (SELECT DISTINCT ON (snapshot_id, label_version) *
          FROM journal.outcomes ORDER BY snapshot_id, label_version, outcome_revision DESC) o
  JOIN journal.snapshots s ON s.snapshot_id = o.snapshot_id
  CROSS JOIN LATERAL jsonb_each(o.labels) t;
