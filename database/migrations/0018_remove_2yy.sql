-- 0018_remove_2yy.sql
-- 2YY (Micro 2-Year Yield futures) is dropped entirely: removed from config.py on
-- 2026-10-06 - it held 26 complete sessions, too little history to use - and its
-- stored data goes with it: bars, session days, collection runs, active-contract
-- days, its intermarket source (us2y_yield_fut) and its contracts. A CSV copy of every
-- row removed from the production store is in data/backups/2yy_removed_20261006/.
--
-- Not touched: the journal. Snapshots built while 2YY was collected keep the
-- us2y_yield_fut values frozen in their payloads - append-only evidence of what was
-- known at their cutoff - though those values can no longer be traced to stored bars.

DELETE FROM bars WHERE contract_id IN (SELECT contract_id FROM contracts WHERE symbol = '2YY');
DELETE FROM session_days WHERE contract_id IN (SELECT contract_id FROM contracts WHERE symbol = '2YY');
DELETE FROM collection_runs WHERE contract_id IN (SELECT contract_id FROM contracts WHERE symbol = '2YY');
DELETE FROM active_contracts WHERE symbol = '2YY';
DELETE FROM asset_sources WHERE symbol = '2YY' OR asset = 'us2y_yield_fut';
DELETE FROM contracts WHERE symbol = '2YY';
