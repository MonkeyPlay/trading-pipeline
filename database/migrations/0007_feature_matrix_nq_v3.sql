-- 0007_feature_matrix_nq_v3.sql
-- The typed-column view of nq_features_v3 snapshots.
--
-- nq_features_v3 keeps every feature name and type of nq_features_v2 (it changes
-- how the daily ATRs tolerate missing sessions and the volatility-index freshness
-- rule), so the view has the same columns; tests/test_forecast_store.py keeps them
-- equal to the catalogue. feature_matrix_nq_v2 stays for the older snapshots.

CREATE VIEW forecast.feature_matrix_nq_v3 AS
SELECT s.snapshot_id, s.session_date, s.instrument_id, s.cutoff_at, s.features_frozen_at,
       s.data_mode, s.pit_availability_status, s.source_revision_id, s.data_quality_status,
       f.*
  FROM forecast.feature_snapshots s,
       jsonb_to_record(s.features) AS f(
           daily_atr_fraction DOUBLE PRECISION,
           daily_volatility_ratio DOUBLE PRECISION,
           atr_1m_14_fraction DOUBLE PRECISION,
           atr_1m_14_relative_30d DOUBLE PRECISION,
           gap_signed_atr DOUBLE PRECISION,
           prior_range_position DOUBLE PRECISION,
           distance_pdh_atr DOUBLE PRECISION,
           distance_pdl_atr DOUBLE PRECISION,
           distance_onh_atr DOUBLE PRECISION,
           distance_onl_atr DOUBLE PRECISION,
           overnight_range_atr DOUBLE PRECISION,
           overnight_range_position DOUBLE PRECISION,
           distance_on_vwap_hlc3_atr DOUBLE PRECISION,
           return_15m_atr DOUBLE PRECISION,
           return_60m_atr DOUBLE PRECISION,
           range_60m_atr DOUBLE PRECISION,
           efficiency_60m DOUBLE PRECISION,
           ema200_distance_5m_atr DOUBLE PRECISION,
           ema9_21_spread_5m_atr DOUBLE PRECISION,
           ema20_slope_15m_atr DOUBLE PRECISION,
           rvol_overnight_30d DOUBLE PRECISION,
           rvol_60m_30d DOUBLE PRECISION,
           prior_rth_return_atr DOUBLE PRECISION,
           prior_rth_range_atr DOUBLE PRECISION,
           prior_rth_close_location DOUBLE PRECISION,
           nq_preopen_return DOUBLE PRECISION,
           es_preopen_return DOUBLE PRECISION,
           rty_preopen_return DOUBLE PRECISION,
           nq_es_relative_return DOUBLE PRECISION,
           nq_es_standardized_divergence DOUBLE PRECISION,
           nq_es_relative_return_60m DOUBLE PRECISION,
           vix_level DOUBLE PRECISION,
           vix_change_points DOUBLE PRECISION,
           vxn_level DOUBLE PRECISION,
           vxn_change_points DOUBLE PRECISION,
           us10y_change_bps DOUBLE PRECISION,
           us2y_change_bps DOUBLE PRECISION,
           yield_curve_10y_2y_change_bps DOUBLE PRECISION,
           dxy_preopen_return DOUBLE PRECISION,
           smh_preopen_return DOUBLE PRECISION,
           dx_fut_preopen_return DOUBLE PRECISION,
           us10y_yield_fut_change_bps DOUBLE PRECISION,
           us2y_yield_fut_change_bps DOUBLE PRECISION,
           yield_fut_curve_10y_2y_change_bps DOUBLE PRECISION,
           gc_preopen_return DOUBLE PRECISION,
           cl_preopen_return DOUBLE PRECISION,
           weekday TEXT,
           monthly_opex_week BOOLEAN,
           days_to_nq_expiry INTEGER,
           roll_transition BOOLEAN,
           is_early_close BOOLEAN,
           remaining_event_risk TEXT,
           has_future_high_event BOOLEAN,
           minutes_to_high_event DOUBLE PRECISION
       )
 WHERE s.feature_version = 'nq_features_v3';
