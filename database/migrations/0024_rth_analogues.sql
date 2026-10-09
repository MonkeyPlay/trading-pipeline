-- 0024_rth_analogues.sql
-- The RTH analogue sets (contracts/nq_rth.py, docs/rth_analogues.md): which earlier
-- sessions opened most like the session in progress, one set per elapsed minute of its
-- first hour - separate from the pre-open analogue sets (migration 0012), which do not
-- change.
--
--   journal.definition_versions   gains the kind 'rth_matcher'
--   journal.rth_analogue_sets     one per session, matcher version, window (elapsed
--                                 minutes from the 09:30 ET open) and input digest: the
--                                 cutoff, the pre-open snapshot the context was taken
--                                 from, the pool, the target's features, the data
--                                 quality, and - stamped by the database clock - when it
--                                 was stored and whether that was live (within 30 minutes
--                                 of its cutoff) or a historical reconstruction.
--                                 ``checkpoint`` marks the 15, 30 and 60 minute windows
--                                 (09:45, 10:00, 10:30 ET) on the same row
--   journal.rth_analogue_members  its up to five sessions: rank, similarity, comparable
--                                 weight and per-feature components
--
-- Append-only, like the rest of the journal: a revised input makes a new set beside the
-- old one, which stays.

ALTER TABLE journal.definition_versions DROP CONSTRAINT definition_versions_kind_check;
ALTER TABLE journal.definition_versions ADD CONSTRAINT definition_versions_kind_check
    CHECK (kind IN ('labels', 'convention', 'snapshot', 'annotation', 'matcher', 'forecast_schema',
                    'forecast_algorithm', 'issue_policy', 'experiment', 'fan_experiment', 'fan_model',
                    'fan_forward', 'rth_matcher'));

CREATE TABLE journal.rth_analogue_sets (
    set_id              UUID PRIMARY KEY,
    symbol              TEXT NOT NULL,
    session_date        DATE NOT NULL,
    contract_id         INTEGER NOT NULL,
    matcher_version     TEXT NOT NULL REFERENCES journal.definition_versions (version),
    context_snapshot_id UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    elapsed_minutes     SMALLINT NOT NULL CHECK (elapsed_minutes BETWEEN 1 AND 60),
    checkpoint          SMALLINT GENERATED ALWAYS AS
                            (CASE WHEN elapsed_minutes IN (15, 30, 60) THEN elapsed_minutes END) STORED,
    cutoff_at           TIMESTAMPTZ NOT NULL,          -- the open + elapsed_minutes: the end of the last bar matched
    input_digest        TEXT NOT NULL,
    pool_size           INTEGER NOT NULL CHECK (pool_size >= 0),
    pool_hash           TEXT NOT NULL,
    excluded            JSONB NOT NULL,
    mean_similarity     NUMERIC,
    target_features     JSONB NOT NULL,
    quality             JSONB NOT NULL,
    code_revision       TEXT NOT NULL,
    data_mode           TEXT NOT NULL CHECK (data_mode IN ('live', 'historical_reconstruction')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (symbol, session_date, matcher_version, elapsed_minutes, input_digest)
);

CREATE INDEX idx_rth_analogue_sets_window
    ON journal.rth_analogue_sets (symbol, session_date, matcher_version, elapsed_minutes, created_at);

CREATE TABLE journal.rth_analogue_members (
    set_id            UUID NOT NULL REFERENCES journal.rth_analogue_sets (set_id),
    rank              SMALLINT NOT NULL CHECK (rank BETWEEN 1 AND 5),
    session_date      DATE NOT NULL,
    contract_id       INTEGER NOT NULL,
    snapshot_id       UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    similarity        NUMERIC NOT NULL,
    comparable_weight NUMERIC NOT NULL CHECK (comparable_weight >= 75),
    components        JSONB NOT NULL,
    PRIMARY KEY (set_id, rank)
);

-- The database clock, not the caller, says when a set was stored and whether that was live: within 30 minutes of
-- its cutoff (contracts/nq_rth.LIVE_MAX_LAG).
CREATE FUNCTION journal.stamp_rth_analogue_set() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.created_at := clock_timestamp();
    NEW.data_mode := CASE WHEN NEW.created_at <= NEW.cutoff_at + interval '30 minutes'
                          THEN 'live' ELSE 'historical_reconstruction' END;
    RETURN NEW;
END;
$$;

CREATE TRIGGER rth_analogue_sets_stamp BEFORE INSERT ON journal.rth_analogue_sets
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_rth_analogue_set();

CREATE TRIGGER rth_analogue_sets_append_only BEFORE UPDATE OR DELETE ON journal.rth_analogue_sets
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER rth_analogue_sets_no_truncate BEFORE TRUNCATE ON journal.rth_analogue_sets
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER rth_analogue_members_append_only BEFORE UPDATE OR DELETE ON journal.rth_analogue_members
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER rth_analogue_members_no_truncate BEFORE TRUNCATE ON journal.rth_analogue_members
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
