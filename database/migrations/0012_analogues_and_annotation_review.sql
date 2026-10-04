-- 0012_analogues_and_annotation_review.sql
-- Guideline stage 2: the structural analogues of each session, the Claude
-- structure-annotation attempts, and the outcome-blind review of annotations.
--
--   journal.definition_versions     gains the kind 'matcher' (contracts/nq_preopen.py)
--   journal.analogue_sets           one per target annotation, matcher, label version,
--                                   candidate pool and attached outcome revisions:
--                                   pool size and hash, exclusions by reason, mean
--                                   similarity, the outcome summary (raw counts,
--                                   denominators, smoothed baseline, prior)
--   journal.analogue_members        its up to five sessions: rank, snapshot and
--                                   annotation, similarity, comparable weight,
--                                   per-feature components, the outcome revision used
--   journal.annotation_attempts     every LLM annotation request, failures included:
--                                   a failed attempt is kept, never filled in from
--                                   another run (Appendix A, A4)
--   journal.annotation_review_*     a set of sessions whose pre-open annotations a
--                                   person checks without seeing the outcome, and the
--                                   verdicts (append-only; the latest per field counts)
--
-- Append-only, like the rest of the journal.

ALTER TABLE journal.definition_versions DROP CONSTRAINT definition_versions_kind_check;
ALTER TABLE journal.definition_versions ADD CONSTRAINT definition_versions_kind_check
    CHECK (kind IN ('labels', 'convention', 'snapshot', 'annotation', 'matcher'));

CREATE TABLE journal.analogue_sets (
    set_id               UUID PRIMARY KEY,
    target_snapshot_id   UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    target_annotation_id UUID NOT NULL REFERENCES journal.structure_annotations (annotation_id),
    matcher_version      TEXT NOT NULL REFERENCES journal.definition_versions (version),
    label_version        TEXT NOT NULL REFERENCES journal.definition_versions (version),
    data_mode            TEXT NOT NULL CHECK (data_mode IN ('live_capture', 'historical_reconstruction',
                                                            'historical_as_observed')),
    pool_size            INTEGER NOT NULL CHECK (pool_size >= 0),
    pool_hash            TEXT NOT NULL,
    excluded             JSONB NOT NULL,
    outcome_digest       TEXT NOT NULL,          -- hash of the members' outcome revisions
    mean_similarity      NUMERIC,
    outcome_summary      JSONB NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (target_annotation_id, matcher_version, label_version, pool_hash, outcome_digest)
);

CREATE INDEX idx_analogue_sets_target ON journal.analogue_sets (target_snapshot_id, matcher_version, created_at);

CREATE TABLE journal.analogue_members (
    set_id              UUID NOT NULL REFERENCES journal.analogue_sets (set_id),
    rank                SMALLINT NOT NULL CHECK (rank BETWEEN 1 AND 5),
    snapshot_id         UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    annotation_id       UUID NOT NULL REFERENCES journal.structure_annotations (annotation_id),
    session_date        DATE NOT NULL,
    similarity          NUMERIC NOT NULL,
    comparable_weight   NUMERIC NOT NULL CHECK (comparable_weight >= 75),
    components          JSONB NOT NULL,
    outcome_revision    INTEGER,                  -- null: no outcome under the label version
    outcome_computed_at TIMESTAMPTZ,
    PRIMARY KEY (set_id, rank)
);

CREATE TABLE journal.annotation_attempts (
    attempt_id       UUID PRIMARY KEY,
    snapshot_id      UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    protocol_version TEXT NOT NULL REFERENCES journal.definition_versions (version),
    model            TEXT NOT NULL,
    request_hash     TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('ok', 'invalid', 'refused', 'error')),
    error            TEXT,
    response         JSONB,
    usage            JSONB,
    annotation_id    UUID REFERENCES journal.structure_annotations (annotation_id),
    started_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ NOT NULL,
    CHECK ((status = 'ok') = (annotation_id IS NOT NULL))
);

CREATE TABLE journal.annotation_review_sets (
    review_set       TEXT PRIMARY KEY,
    protocol_version TEXT NOT NULL REFERENCES journal.definition_versions (version),
    snapshot_version TEXT NOT NULL REFERENCES journal.definition_versions (version),
    selection        JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE journal.annotation_review_members (
    review_set    TEXT NOT NULL REFERENCES journal.annotation_review_sets (review_set),
    snapshot_id   UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    annotation_id UUID NOT NULL REFERENCES journal.structure_annotations (annotation_id),
    position      INTEGER NOT NULL CHECK (position >= 1),
    reasons       JSONB NOT NULL,
    PRIMARY KEY (review_set, snapshot_id),
    UNIQUE (review_set, position)
);

CREATE TABLE journal.annotation_review_verdicts (
    verdict_id    BIGSERIAL PRIMARY KEY,
    review_set    TEXT NOT NULL,
    snapshot_id   UUID NOT NULL,
    annotation_id UUID NOT NULL REFERENCES journal.structure_annotations (annotation_id),
    field         TEXT NOT NULL,
    shown_value   TEXT NOT NULL,
    verdict       TEXT NOT NULL CHECK (verdict IN ('agree', 'disagree', 'unsure')),
    note          TEXT,
    reviewer      TEXT,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (review_set, snapshot_id) REFERENCES journal.annotation_review_members (review_set, snapshot_id)
);

-- The verdict that counts: the latest per set, session and field.
CREATE VIEW journal.annotation_review_latest AS
SELECT DISTINCT ON (v.review_set, v.snapshot_id, v.field)
       v.review_set, v.snapshot_id, s.session_date, v.annotation_id, v.field, v.shown_value, v.verdict, v.note,
       v.reviewer, v.recorded_at
  FROM journal.annotation_review_verdicts v
  JOIN journal.snapshots s ON s.snapshot_id = v.snapshot_id
 ORDER BY v.review_set, v.snapshot_id, v.field, v.verdict_id DESC;

CREATE FUNCTION journal.check_analogue_set() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT kind FROM journal.definition_versions WHERE version = NEW.matcher_version) <> 'matcher' THEN
        RAISE EXCEPTION '% is not a matcher version', NEW.matcher_version USING ERRCODE = 'check_violation';
    END IF;
    IF (SELECT kind FROM journal.definition_versions WHERE version = NEW.label_version) <> 'labels' THEN
        RAISE EXCEPTION '% is not a label version', NEW.label_version USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER analogue_sets_check BEFORE INSERT ON journal.analogue_sets
    FOR EACH ROW EXECUTE FUNCTION journal.check_analogue_set();

CREATE TRIGGER analogue_sets_append_only BEFORE UPDATE OR DELETE ON journal.analogue_sets
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER analogue_sets_no_truncate BEFORE TRUNCATE ON journal.analogue_sets
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER analogue_members_append_only BEFORE UPDATE OR DELETE ON journal.analogue_members
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER analogue_members_no_truncate BEFORE TRUNCATE ON journal.analogue_members
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER annotation_attempts_append_only BEFORE UPDATE OR DELETE ON journal.annotation_attempts
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER annotation_attempts_no_truncate BEFORE TRUNCATE ON journal.annotation_attempts
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER annotation_review_sets_append_only BEFORE UPDATE OR DELETE ON journal.annotation_review_sets
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER annotation_review_members_append_only BEFORE UPDATE OR DELETE ON journal.annotation_review_members
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER annotation_review_members_no_truncate BEFORE TRUNCATE ON journal.annotation_review_members
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER annotation_review_verdicts_append_only BEFORE UPDATE OR DELETE ON journal.annotation_review_verdicts
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER annotation_review_verdicts_no_truncate BEFORE TRUNCATE ON journal.annotation_review_verdicts
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
