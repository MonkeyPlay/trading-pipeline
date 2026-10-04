-- 0010_review_sets.sql
-- Human review of the realised outcome labels (guideline stage 1E): a fixed,
-- diverse set of sessions and the reviewer's verdict on each P2 field.
--
--   journal.review_sets     : one named set, the label and snapshot versions it
--                             reviews and the selection rule that chose it
--   journal.review_members  : its sessions (snapshots), in order, each with the
--                             label classes it was chosen to cover
--   journal.review_verdicts : agree / disagree / unsure per session and P2 field,
--                             with the value shown and a note; a re-review is a
--                             new row, the latest per field counts
--
-- Append-only, like the rest of the journal.

CREATE TABLE journal.review_sets (
    review_set       TEXT PRIMARY KEY,
    label_version    TEXT NOT NULL REFERENCES journal.definition_versions (version),
    snapshot_version TEXT NOT NULL REFERENCES journal.definition_versions (version),
    selection        JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE journal.review_members (
    review_set  TEXT NOT NULL REFERENCES journal.review_sets (review_set),
    snapshot_id UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    position    INTEGER NOT NULL CHECK (position >= 1),
    reasons     JSONB NOT NULL,
    PRIMARY KEY (review_set, snapshot_id),
    UNIQUE (review_set, position)
);

CREATE TABLE journal.review_verdicts (
    verdict_id       BIGSERIAL PRIMARY KEY,
    review_set       TEXT NOT NULL,
    snapshot_id      UUID NOT NULL,
    outcome_revision INTEGER NOT NULL CHECK (outcome_revision >= 1),
    field            TEXT NOT NULL,
    shown_value      TEXT NOT NULL,
    verdict          TEXT NOT NULL CHECK (verdict IN ('agree', 'disagree', 'unsure')),
    note             TEXT,
    reviewer         TEXT,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (review_set, snapshot_id) REFERENCES journal.review_members (review_set, snapshot_id)
);

CREATE INDEX idx_review_verdicts_member ON journal.review_verdicts (review_set, snapshot_id, field, verdict_id);

CREATE TRIGGER review_sets_append_only BEFORE UPDATE OR DELETE ON journal.review_sets
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER review_sets_no_truncate BEFORE TRUNCATE ON journal.review_sets
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER review_members_append_only BEFORE UPDATE OR DELETE ON journal.review_members
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER review_members_no_truncate BEFORE TRUNCATE ON journal.review_members
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER review_verdicts_append_only BEFORE UPDATE OR DELETE ON journal.review_verdicts
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER review_verdicts_no_truncate BEFORE TRUNCATE ON journal.review_verdicts
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();

-- The verdict that counts: the latest per set, session and field.
CREATE VIEW journal.review_latest AS
SELECT DISTINCT ON (v.review_set, v.snapshot_id, v.field)
       v.review_set, v.snapshot_id, s.session_date, v.field, v.shown_value, v.verdict, v.note, v.reviewer,
       v.outcome_revision, v.recorded_at
  FROM journal.review_verdicts v
  JOIN journal.snapshots s ON s.snapshot_id = v.snapshot_id
 ORDER BY v.review_set, v.snapshot_id, v.field, v.verdict_id DESC;
