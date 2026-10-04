-- 0016_live_capture.sql
-- Guideline revision 2, 3D: live capture with receipt provenance.
--
--   journal.live_captures        one row per run of the scheduled pre-open job
--                                (forecaster/live_capture.py), for one session and
--                                profile, with the code revision
--   journal.live_capture_events  its steps - bars requested and received, stale,
--                                snapshot frozen or reused, annotated, matched,
--                                forecast issued or late, failed - each stamped by
--                                the database clock, so end-to-end timing is measured
--                                on the server and a client clock cannot shorten it
--   journal.bar_receipts         every 1m bar a capture received, as received, stamped
--                                by the database clock: the evidence that a live
--                                snapshot's bars were known when it was frozen
--
-- Append-only, like the rest of the journal.

CREATE TABLE journal.live_captures (
    capture_id    UUID PRIMARY KEY,
    session_date  DATE NOT NULL,
    profile       TEXT NOT NULL,
    contract_id   BIGINT REFERENCES contracts (contract_id),
    code_revision TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX idx_live_captures_session ON journal.live_captures (session_date, profile, started_at);

CREATE TABLE journal.live_capture_events (
    event_id   BIGSERIAL PRIMARY KEY,
    capture_id UUID NOT NULL REFERENCES journal.live_captures (capture_id),
    event      TEXT NOT NULL CHECK (event IN ('bars_requested', 'bars_received', 'stale', 'snapshot_frozen',
                                              'snapshot_reused', 'annotated', 'matched', 'forecast', 'failed')),
    at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    detail     JSONB
);

CREATE TABLE journal.bar_receipts (
    receipt_id   BIGSERIAL PRIMARY KEY,
    capture_id   UUID NOT NULL REFERENCES journal.live_captures (capture_id),
    contract_id  BIGINT NOT NULL REFERENCES contracts (contract_id),
    interval     TEXT NOT NULL,
    price_type   TEXT NOT NULL,
    bar_start_at TIMESTAMPTZ NOT NULL,
    open         DOUBLE PRECISION NOT NULL,
    high         DOUBLE PRECISION NOT NULL,
    low          DOUBLE PRECISION NOT NULL,
    close        DOUBLE PRECISION NOT NULL,
    volume       BIGINT NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX idx_bar_receipts_bar ON journal.bar_receipts (contract_id, bar_start_at);
CREATE INDEX idx_bar_receipts_capture ON journal.bar_receipts (capture_id);

-- Times are the database's, whatever the client sends.
CREATE FUNCTION journal.stamp_received_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.received_at := clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE FUNCTION journal.stamp_event_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.at := clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE FUNCTION journal.stamp_started_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.started_at := clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE TRIGGER bar_receipts_stamp BEFORE INSERT ON journal.bar_receipts
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_received_at();
CREATE TRIGGER live_capture_events_stamp BEFORE INSERT ON journal.live_capture_events
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_event_at();
CREATE TRIGGER live_captures_stamp BEFORE INSERT ON journal.live_captures
    FOR EACH ROW EXECUTE FUNCTION journal.stamp_started_at();

CREATE TRIGGER live_captures_append_only BEFORE UPDATE OR DELETE ON journal.live_captures
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER live_captures_no_truncate BEFORE TRUNCATE ON journal.live_captures
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER live_capture_events_append_only BEFORE UPDATE OR DELETE ON journal.live_capture_events
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER live_capture_events_no_truncate BEFORE TRUNCATE ON journal.live_capture_events
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER bar_receipts_append_only BEFORE UPDATE OR DELETE ON journal.bar_receipts
    FOR EACH ROW EXECUTE FUNCTION journal.reject_mutation();
CREATE TRIGGER bar_receipts_no_truncate BEFORE TRUNCATE ON journal.bar_receipts
    FOR EACH STATEMENT EXECUTE FUNCTION journal.reject_mutation();
