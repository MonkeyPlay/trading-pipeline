-- 0028_live_synthesis_events.sql
-- Arm D in a live capture (forecaster/live_synthesis.py): three more capture steps.
--
--   synthesis_requested  the synthesis request was recorded in the inference ledger and sent
--   synthesis_skipped    no request this session, and why (no approval, the deadline had passed,
--                        no analogue set, already requested)
--   delivered            the forecast in force at the deadline - the first timely run of D, B, A -
--                        or none; stamped by the database clock like every other step
--
-- The D run itself is a 'forecast' step, as arms A and B are.

ALTER TABLE journal.live_capture_events DROP CONSTRAINT live_capture_events_event_check;
ALTER TABLE journal.live_capture_events ADD CONSTRAINT live_capture_events_event_check
    CHECK (event IN ('bars_requested', 'bars_received', 'stale', 'snapshot_frozen', 'snapshot_reused', 'annotated',
                     'matched', 'forecast', 'failed', 'synthesis_requested', 'synthesis_skipped', 'delivered'));
