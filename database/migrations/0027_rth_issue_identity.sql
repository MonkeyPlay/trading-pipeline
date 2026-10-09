-- 0027_rth_issue_identity.sql
-- An RTH analogue set's identity per kind of issue (contracts/nq_rth.py, docs/rth_analogues.md).
--
-- 0024 stored one set per (session, matcher version, window, input digest), so an issue by Auto or
-- by hand whose inputs a backfill had already matched returned the backfill's set, and the issue
-- left no trace. Now an issue is recorded beside such a backfill: one set per input digest and per
-- kind - issued (auto, manual) or backfill. Repeated issues, or repeated backfills, of unchanged
-- inputs still add nothing (database/journal_store.save_rth_set).

ALTER TABLE journal.rth_analogue_sets DROP CONSTRAINT rth_analogue_sets_symbol_session_date_matcher_version_elaps_key;
CREATE UNIQUE INDEX rth_analogue_sets_identity
    ON journal.rth_analogue_sets (symbol, session_date, matcher_version, elapsed_minutes, input_digest,
                                  (issued_by = 'backfill'));
