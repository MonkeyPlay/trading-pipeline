-- 0005_bar_receipts.sql
-- Real-time bars with a per-bar receive time.
--
-- collector/live_stream.py subscribes to IB's keep-up-to-date 1-minute bars and,
-- the moment a minute is final, does two things:
--
--   bar_receipts : appends the bar exactly as received, with received_at - the
--                  wall-clock time the pipeline knew that minute's final value.
--                  Append-only: a later, different value for the same minute
--                  (a late update, the 09:28 confirmation fetch) is a new row
--                  with the next revision, never an edit. This is the
--                  point-in-time evidence a live snapshot cites.
--
--   bars         : upserts the same bar (is_completed = 0, so its day stays
--                  PARTIAL and the regular collector re-downloads it later), so
--                  the feature builder reads live and downloaded bars through
--                  one path. When save_trading_day() replaces a day, live bars
--                  newer than the download are carried over from bar_receipts,
--                  so a download racing the stream cannot drop them.

CREATE TABLE bar_receipts (
    contract_id  BIGINT NOT NULL REFERENCES contracts (contract_id),
    interval     TEXT NOT NULL,
    price_type   TEXT NOT NULL,
    bar_start_at TIMESTAMPTZ NOT NULL,           -- hypertable time dimension
    received_at  TIMESTAMPTZ NOT NULL,           -- when this final value was known
    revision     INTEGER NOT NULL,               -- 1 = first final value of the minute
    trading_day  DATE NOT NULL,                  -- NY session date, as in bars
    session_scope TEXT NOT NULL,
    open         DOUBLE PRECISION NOT NULL,
    high         DOUBLE PRECISION NOT NULL,
    low          DOUBLE PRECISION NOT NULL,
    close        DOUBLE PRECISION NOT NULL,
    volume       BIGINT NOT NULL,
    wap          DOUBLE PRECISION,
    bar_count    INTEGER,
    finalised_by TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'IBKR_LIVE',
    stream_id    TEXT,                           -- one id per streamer process
    stored_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (contract_id, interval, price_type, bar_start_at, revision),
    CHECK (revision >= 1),
    CHECK (received_at >= bar_start_at),
    CHECK (session_scope IN ('RTH', 'ETH')),
    -- next_bar      : the stream moved on to a later minute
    -- timer         : no later update arrived within the grace period
    -- initial_fill  : part of the history IB sends when a stream opens
    -- late_update   : IB changed a minute already finalised
    -- confirm_fetch : a one-off historical request re-read the minute
    CHECK (finalised_by IN ('next_bar', 'timer', 'initial_fill', 'late_update', 'confirm_fetch'))
);

SELECT create_hypertable('bar_receipts', by_range('bar_start_at', INTERVAL '30 days'));

CREATE INDEX idx_bar_receipts_day ON bar_receipts (contract_id, interval, price_type, trading_day);

CREATE OR REPLACE FUNCTION reject_bar_receipt_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'bar_receipts is append-only: % rejected. A changed value is a new revision.', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER bar_receipts_append_only BEFORE UPDATE OR DELETE ON bar_receipts
    FOR EACH ROW EXECUTE FUNCTION reject_bar_receipt_mutation();

COMMENT ON COLUMN bars.source IS
    'IBKR for a downloaded day; IBKR_LIVE for a bar written by the real-time stream '
    '(its receive time is in bar_receipts).';
