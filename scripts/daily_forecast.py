#!/usr/bin/env python3
"""
End-to-end daily forecast runner for the NQ Opening Forecast System.

Ties the pieces together and PERSISTS the results:
  1. load recent 1-minute bars for the contract
  2. evaluate realized outcomes for every completed prior session   -> outcomes
  3. compute the pre-open feature snapshot for the target session   -> feature_snapshots
  4. find volatility-normalized historical analogues
  5. request an LLM (or baseline) opening forecast                  -> predictions
  6. store the analogue matches                                     -> analogue_matches

Run from the project root:
    python scripts/daily_forecast.py --expiry 202609
"""

import os
import sys
import argparse
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytz

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config
from database.connection import get_db_connection, init_database
from database.queries import (
    get_contract_by_expiry,
    get_bars,
    get_outcome,
    save_feature_snapshot,
    save_prediction,
    save_analogue_matches,
    save_outcome,
)
from features.calculations import calculate_pre_open_snapshot
from features.session_windows import enrich_candle_timezones, NY_TZ
from forecaster.evaluator import evaluate_session_outcomes
from forecaster.client import ForecastClient
from matching.normalizer import find_analogues

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("daily_forecast")


def _cutoff_utc_iso(session_date_str):
    d = datetime.strptime(session_date_str, "%Y-%m-%d")
    return NY_TZ.localize(datetime(d.year, d.month, d.day, 9, 30)).astimezone(pytz.utc).isoformat()


def _load_bars_df(conn, contract_id, lookback_days):
    start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = get_bars(conn, contract_id, interval="1m", start_utc=start)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


def run(dsn, symbol, expiry, model, lookback_days, target_date=None):
    init_database(dsn)
    conn = get_db_connection(dsn)
    try:
        contract = get_contract_by_expiry(conn, symbol, expiry)
        if contract is None:
            logger.error(f"No contract {symbol} {expiry} in DB. Run the collector first.")
            return 1
        contract_id = contract["contract_id"]

        df = _load_bars_df(conn, contract_id, lookback_days)
        if df.empty:
            logger.error("No bar data available for the requested window.")
            return 1

        enriched = enrich_candle_timezones(df)
        trading_days = sorted(d for d in enriched["trading_day"].dropna().unique())
        if len(trading_days) < 2:
            logger.error("Need at least two trading days of data to build a forecast.")
            return 1

        # --- 2. Backfill realized outcomes for completed sessions ---
        now_ny = datetime.now(NY_TZ)
        for day in trading_days:
            day_dt = datetime.strptime(day, "%Y-%m-%d").date()
            session_over = (day_dt < now_ny.date()) or (day_dt == now_ny.date() and now_ny.hour >= 16)
            if not session_over:
                continue
            if get_outcome(conn, contract_id, day) is not None:
                continue
            outcome = evaluate_session_outcomes(df, day)
            if not outcome or "error" in outcome:
                continue
            save_outcome(
                conn, contract_id=contract_id, session_date=outcome["session_date"],
                first_15_minute_high=outcome["first_15_minute_high"],
                first_15_minute_low=outcome["first_15_minute_low"],
                first_15_minute_close=outcome["first_15_minute_close"],
                first_30_minute_high=outcome["first_30_minute_high"],
                first_30_minute_low=outcome["first_30_minute_low"],
                first_30_minute_close=outcome["first_30_minute_close"],
                initial_balance_high=outcome["initial_balance_high"],
                initial_balance_low=outcome["initial_balance_low"],
                rth_high=outcome["rth_high"], rth_low=outcome["rth_low"], rth_close=outcome["rth_close"],
                raw_outcomes=outcome["raw_outcomes"],
            )
            logger.info(f"Saved realized outcome for {day}.")

        # --- 3. Target session + pre-open snapshot ---
        target = target_date or trading_days[-1]
        snapshot = calculate_pre_open_snapshot(df, target)
        if not snapshot or "error" in snapshot:
            logger.error(f"Cannot compute pre-open snapshot for {target}: {snapshot.get('error')}")
            return 1

        cutoff = _cutoff_utc_iso(target)
        snapshot_id = save_feature_snapshot(
            conn, contract_id=contract_id, timestamp_utc=cutoff,
            previous_rth_high=snapshot["previous_rth_high"],
            previous_rth_low=snapshot["previous_rth_low"],
            previous_rth_close=snapshot["previous_rth_close"],
            overnight_high=snapshot["overnight_high"],
            overnight_low=snapshot["overnight_low"],
            overnight_range=snapshot["overnight_range"],
            gap=snapshot["gap"],
            pre_open_direction=snapshot["pre_open_direction"],
            historical_volatility=snapshot["historical_volatility"],
            vwap=snapshot["vwap"],
            raw_features=snapshot,
            feature_version=Config.FEATURE_VERSION,
        )
        logger.info(f"Saved feature snapshot #{snapshot_id} for {target}.")

        # --- 4. Historical analogues (strictly earlier sessions) ---
        hist_snapshots = []
        for day in trading_days:
            if day >= target:
                continue
            hist = calculate_pre_open_snapshot(df, day)
            if not hist or "error" in hist:
                continue
            out_row = get_outcome(conn, contract_id, day)
            if out_row is not None:
                hist["outcome"] = dict(out_row)
            hist_snapshots.append(hist)

        analogues = find_analogues(snapshot, hist_snapshots, k=5)

        # --- 5. Forecast ---
        client = ForecastClient(model_name=model)
        forecast = client.get_forecast(target, snapshot, analogues)

        prediction_id = save_prediction(
            conn, contract_id=contract_id, forecast_cutoff=cutoff, snapshot_id=snapshot_id,
            model_version=client.model_name, prompt_version=Config.PROMPT_VERSION,
            opening_bias=forecast.get("opening_bias"),
            scenarios=forecast.get("scenarios", {}),
            probabilities=forecast.get("probabilities", {}),
            raw_response=str(forecast),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info(f"Saved prediction #{prediction_id} (bias={forecast.get('opening_bias')}).")

        # --- 6. Analogue matches ---
        if analogues:
            save_analogue_matches(conn, prediction_id, [
                {"match_date": a["match_date"], "similarity_score": a["similarity_score"], "ranking": a["ranking"]}
                for a in analogues
            ])
            logger.info(f"Saved {len(analogues)} analogue matches.")

        logger.info("Daily forecast pipeline completed.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run + persist the daily NQ opening forecast")
    parser.add_argument("--db", default=Config.DATABASE_URL, help="PostgreSQL connection URL")
    parser.add_argument("--symbol", default="NQ")
    parser.add_argument("--expiry", required=True, help="Contract expiry YYYYMM (e.g. 202609)")
    parser.add_argument("--model", default=Config.LLM_MODEL)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--date", default=None, help="Target session YYYY-MM-DD (default: latest available)")
    args = parser.parse_args()

    sys.exit(run(args.db, args.symbol, args.expiry, args.model, args.lookback_days, args.date))
