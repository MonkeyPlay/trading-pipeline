-- 0013_prior_digest_and_inference_ledger.sql
-- Guideline revision 2 (stage 2 audit): the analogue prior's provenance, and
-- durable accounting of every LLM (inference) request.
--
--   journal.analogue_sets.prior_digest   hash of the prior manifest - every earlier
--                                        session and outcome revision the prior was
--                                        built from (nq_match_p1_v2); a revised
--                                        outcome of a session that is not an analogue
--                                        changes the prior, so it makes a new set.
--                                        Null for nq_match_p1_v1 sets, which did not
--                                        record it
--   journal.inference_requests           one row per LLM request, written before it is
--                                        sent, archiving the exact canonical request
--                                        with its prompt, schema and evidence hashes and
--                                        the code revision: a crash after sending leaves
--                                        the row without an attempt, so no billed request
--                                        goes unrecorded, and an answer is validated
--                                        against what was sent, never a rebuilt request
--   journal.inference_batches / _batch_requests  a Batch API batch, recorded as soon
--                                        as its id is known, and the requests in it (the
--                                        request id is the batch custom_id), so an
--                                        interrupted run can collect it later instead of
--                                        sending it again
--   journal.annotation_attempts.request_id  the request an attempt answers (at most one
--                                        attempt per request); raw_text keeps an answer
--                                        that was not JSON
--
-- Append-only, like the rest of the journal.

ALTER TABLE journal.analogue_sets ADD COLUMN prior_digest TEXT;

DO $$
DECLARE c TEXT;
BEGIN
    SELECT conname INTO c FROM pg_constraint
     WHERE conrelid = 'journal.analogue_sets'::regclass AND contype = 'u';
    EXECUTE format('ALTER TABLE journal.analogue_sets DROP CONSTRAINT %I', c);
END;
$$;

ALTER TABLE journal.analogue_sets ADD CONSTRAINT analogue_sets_identity
    UNIQUE NULLS NOT DISTINCT (target_annotation_id, matcher_version, label_version, pool_hash, outcome_digest,
                               prior_digest);

COMMENT ON COLUMN journal.analogue_sets.outcome_digest IS 'hash of the members'' outcome revisions';
COMMENT ON COLUMN journal.analogue_sets.prior_digest IS
    'hash of the prior manifest (earlier sessions and outcome revisions); null before nq_match_p1_v2';

CREATE TABLE journal.inference_requests (
    request_id       UUID PRIMARY KEY,
    snapshot_id      UUID NOT NULL REFERENCES journal.snapshots (snapshot_id),
    protocol_version TEXT NOT NULL REFERENCES journal.definition_versions (version),
    model            TEXT NOT NULL,
    request          JSONB NOT NULL,          -- the exact canonical request sent
    request_hash     TEXT NOT NULL,           -- sha256 of its canonical JSON
    prompt_sha256    TEXT NOT NULL,
    schema_sha256    TEXT NOT NULL,
    evidence_sha256  TEXT NOT NULL,
    code_revision    TEXT NOT NULL,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'batch')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX idx_inference_requests_snapshot ON journal.inference_requests (snapshot_id, protocol_version);

CREATE TABLE journal.inference_batches (
    batch_id      TEXT PRIMARY KEY,
    submitted_at  TIMESTAMPTZ NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 1),
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE journal.inference_batch_requests (
    batch_id   TEXT NOT NULL REFERENCES journal.inference_batches (batch_id),
    request_id UUID PRIMARY KEY REFERENCES journal.inference_requests (request_id)
);

ALTER TABLE journal.annotation_attempts
    ADD COLUMN request_id UUID REFERENCES journal.inference_requests (request_id),
    ADD COLUMN raw_text TEXT;
CREATE UNIQUE INDEX annotation_attempts_one_per_request ON journal.annotation_attempts (request_id)
    WHERE request_id IS NOT NULL;

CREATE TRIGGER inference_requests_append_only BEFORE UPDATE OR DELETE ON journal.inference_requests
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER inference_requests_no_truncate BEFORE TRUNCATE ON journal.inference_requests
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER inference_batches_append_only BEFORE UPDATE OR DELETE ON journal.inference_batches
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER inference_batches_no_truncate BEFORE TRUNCATE ON journal.inference_batches
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER inference_batch_requests_append_only BEFORE UPDATE OR DELETE ON journal.inference_batch_requests
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER inference_batch_requests_no_truncate BEFORE TRUNCATE ON journal.inference_batch_requests
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
