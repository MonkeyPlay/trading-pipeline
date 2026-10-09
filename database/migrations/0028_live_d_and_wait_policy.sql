-- 0028_live_d_and_wait_policy.sql
-- Live issue policy nq_issue_live_v3 (contracts/nq_forecast.py): arm D in a live capture
-- (forecaster/live_synthesis.py) and a data wait set per capture.
--
-- journal.live_captures gains the capture's settings, recorded when it starts:
--   issue_policy   the live issue policy it ran under
--   wait_limit_s   how long after the cutoff it waits for the bar ending at the cutoff
--   reserve_s      the time it kept before the deadline for issuing (A and B; with D)
--   with_d         whether arm D was part of it
-- (all NULL for captures before this migration).
--
-- journal.live_capture_events gains three steps:
--   synthesis_requested  the synthesis request was recorded in the inference ledger and sent
--   synthesis_skipped    no request this session, and why (no approval, the deadline had passed,
--                        no analogue set, already requested)
--   delivered            the forecast in force at the deadline - the first timely run of D, B, A -
--                        or none; stamped by the database clock like every other step
-- The D run itself is a 'forecast' step, as arms A and B are.

ALTER TABLE journal.live_captures
    ADD COLUMN issue_policy TEXT REFERENCES journal.definition_versions (version),
    ADD COLUMN wait_limit_s INTEGER CHECK (wait_limit_s > 0),
    ADD COLUMN reserve_s    INTEGER CHECK (reserve_s >= 0),
    ADD COLUMN with_d       BOOLEAN;

ALTER TABLE journal.live_capture_events DROP CONSTRAINT live_capture_events_event_check;
ALTER TABLE journal.live_capture_events ADD CONSTRAINT live_capture_events_event_check
    CHECK (event IN ('bars_requested', 'bars_received', 'stale', 'snapshot_frozen', 'snapshot_reused', 'annotated',
                     'matched', 'forecast', 'failed', 'synthesis_requested', 'synthesis_skipped', 'delivered'));
