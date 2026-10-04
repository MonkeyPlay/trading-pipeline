-- 0011_structure_annotations.sql
-- Pre-open structure annotations (guideline stage 2A): the P1 fields of one
-- snapshot - Overnight Structure, Premarket Pattern, trends, the MA fields, Chop
-- Score, Event Risk - as an annotator filled them under a registered protocol.
--
--   journal.definition_versions  gains the kind 'annotation': an annotation protocol
--                                (contracts/nq_preopen.py)
--   journal.structure_annotations one annotation per snapshot, protocol and output:
--                                the rule-based annotator now, the Claude structure
--                                annotation (Appendix A, A1) later under its own
--                                protocol, both in the same shape
--
-- Append-only, like the rest of the journal. A rule-based protocol is deterministic:
-- the store refuses a second, different output for the same snapshot and protocol.

ALTER TABLE journal.definition_versions DROP CONSTRAINT definition_versions_kind_check;
ALTER TABLE journal.definition_versions ADD CONSTRAINT definition_versions_kind_check
    CHECK (kind IN ('labels', 'convention', 'snapshot', 'annotation'));

CREATE TABLE journal.structure_annotations (
    annotation_id    UUID PRIMARY KEY,
    snapshot_id      UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    protocol_version TEXT NOT NULL REFERENCES journal.definition_versions (version),
    annotator        TEXT NOT NULL CHECK (annotator IN ('rules', 'llm')),
    model            TEXT,                          -- llm: the model that answered
    integrity_status TEXT NOT NULL CHECK (integrity_status IN ('ok', 'contaminated')),
    fields           JSONB NOT NULL,
    price_location   JSONB NOT NULL,
    measurements     JSONB NOT NULL,
    output_hash      TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, protocol_version, output_hash),
    CHECK (jsonb_typeof(fields) = 'object'),
    CHECK ((annotator = 'llm') = (model IS NOT NULL))
);

CREATE INDEX idx_structure_annotations_snapshot ON journal.structure_annotations (snapshot_id, protocol_version);

CREATE FUNCTION journal.check_annotation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT kind FROM journal.definition_versions WHERE version = NEW.protocol_version) <> 'annotation' THEN
        RAISE EXCEPTION '% is not an annotation protocol', NEW.protocol_version USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER structure_annotations_check BEFORE INSERT ON journal.structure_annotations
    FOR EACH ROW EXECUTE FUNCTION journal.check_annotation();
CREATE TRIGGER structure_annotations_append_only BEFORE UPDATE OR DELETE ON journal.structure_annotations
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER structure_annotations_no_truncate BEFORE TRUNCATE ON journal.structure_annotations
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
