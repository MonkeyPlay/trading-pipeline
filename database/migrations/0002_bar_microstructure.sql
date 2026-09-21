-- 0002_bar_microstructure.sql
-- Capture the per-bar fields IB already returns (cheap now, painful to backfill
-- from a vendor later) and denormalise the NY trading day onto every bar so that
-- coverage checks and feature queries don't have to recompute it every time.
--
--   wap         : volume-weighted average price of the bar (IB BarData.wap)
--   bar_count   : number of trades in the bar (IB BarData.barCount)
--   trading_day : 'YYYY-MM-DD' NY session the bar belongs to (18:00 ET rollover)
--
-- Existing rows get NULL trading_day; run  python -m database.backfill  to fill them.

ALTER TABLE bars ADD COLUMN wap REAL;
ALTER TABLE bars ADD COLUMN bar_count INTEGER;
ALTER TABLE bars ADD COLUMN trading_day TEXT;

CREATE INDEX IF NOT EXISTS idx_bars_trading_day ON bars (contract_id, interval, trading_day);
