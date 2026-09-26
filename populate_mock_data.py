# populate_mock_data.py
"""
Populate the database with realistic mock trading data — bars, feature
snapshots, predictions, outcomes and analogue matches — for the last few
trading days, so the dashboard can be verified without a live IB connection.

Covers every instrument in MOCK_CONTRACTS, each fabricated around its own index
level: the ES, NQ and RTY futures get the full set of rows, and cash VIX gets
bars only, as pre-open context for them.

    python populate_mock_data.py                 # append to the dev DB
    python populate_mock_data.py --reset         # drop & recreate the dev DB first
    python populate_mock_data.py --db postgresql://...   # target a specific database

Writes to Config.DEV_DATABASE_URL by default. Refuses to --reset any database that
contains non-MOCK bars (i.e. real collected data) unless --force is given.
"""

import os
import sys
import argparse
import random
import logging
from datetime import datetime, timedelta

import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database.connection import describe_dsn, get_db_connection, init_database, reset_database
from database.queries import (
    upsert_contract,
    save_bars_by_day,
    save_feature_snapshot,
    save_prediction,
    save_outcome,
    save_analogue_matches,
)
from features.session_windows import convert_utc_to_ny, get_trading_day_date
from config import Config
from forecaster.client import MODEL_VERSION, PROMPT_VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

NY_TZ = pytz.timezone("America/New_York")

# The instruments to fabricate, as (contract_id, symbol, base_price, vol_multiplier).
# Contract ids are arbitrary but stable, so re-running without --reset updates rather
# than duplicates. Metadata (tick size, multiplier, exchange, sec_type) comes from Config.
#
# vol_multiplier is relative to the price-proportional default: VIX swings a few
# percent on a normal day, an order of magnitude more than the index it measures, so
# scaling its moves off its own level alone would leave it almost flat.
MOCK_CONTRACTS = [
    (12345678, "NQ", 19500.0, 1.0),
    (12345679, "ES", 5800.0, 1.0),
    (12345680, "RTY", 2300.0, 1.0),
    (12345681, "VIX", 18.0, 12.0),
]

# The point-based constants below were tuned against NQ near 19_500. Scaling them
# by price keeps the fabricated moves proportionally similar on ES and RTY, whose
# index levels differ by an order of magnitude.
_REFERENCE_PRICE = 19500.0


def _scale(base_price):
    return base_price / _REFERENCE_PRICE


def generate_mock_candles(contract_id, start_date, end_date, base_price,
                          has_volume=True, vol_multiplier=1.0):
    """
    Generates 1-minute OHLCV candles (ETH + RTH) as a simple random walk.

    ``has_volume=False`` produces index bars: a cash index is not traded, so IB
    reports no volume for it and neither does this. ``vol_multiplier`` widens the
    price-proportional move size for an instrument that is inherently jumpier.
    """
    candles = []
    scale = _scale(base_price) * vol_multiplier
    current_price = base_price
    current_day = start_date.date()
    last_day = end_date.date()

    while current_day <= last_day:
        if current_day.weekday() >= 5:  # skip Sat/Sun session starts
            current_day += timedelta(days=1)
            continue

        prev_day = current_day - timedelta(days=1)
        if prev_day.weekday() == 5:  # Saturday evening -> no session
            current_day += timedelta(days=1)
            continue

        eth_start = NY_TZ.localize(datetime(prev_day.year, prev_day.month, prev_day.day, 18, 0))
        eth_end = NY_TZ.localize(datetime(current_day.year, current_day.month, current_day.day, 17, 0))

        t = eth_start
        while t < eth_end:
            if t.hour == 17:  # daily 17:00-18:00 maintenance gap
                t += timedelta(minutes=1)
                continue

            is_rth = (t.hour > 9 or (t.hour == 9 and t.minute >= 30)) and t.hour < 16
            session_scope = "RTH" if is_rth else "ETH"
            vol = (8.0 if is_rth else 3.0) * scale

            o = current_price + random.normalvariate(0, vol * 0.1)
            c = o + random.normalvariate(0, vol * 0.2)
            h = max(o, c) + abs(random.normalvariate(0, vol * 0.15))
            low = min(o, c) - abs(random.normalvariate(0, vol * 0.15))
            v = int(random.lognormvariate(6, 1) if is_rth else random.lognormvariate(4, 0.8))

            ts_utc = t.astimezone(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")
            candles.append({
                "contract_id": contract_id,
                "timestamp_utc": ts_utc,
                "interval": "1m",
                "open": round(o, 2), "high": round(h, 2), "low": round(low, 2), "close": round(c, 2),
                "volume": max(1, v) if has_volume else 0,
                "price_type": "TRADES",
                "session_scope": session_scope,
                "source": "MOCK",
                "is_completed": 1,
                "wap": round((h + low + c) / 3, 2),
                "bar_count": max(1, v // 5) if has_volume else 0,
                "trading_day": get_trading_day_date(convert_utc_to_ny(ts_utc)),
            })
            current_price = c
            t += timedelta(minutes=1)

        current_day += timedelta(days=1)

    return candles


def _direction_to_bias(direction):
    return {"UP": "BULLISH", "DOWN": "BEARISH", "FLAT": "NEUTRAL"}[direction]


def _assert_mock_safe(dsn):
    """Abort if the target DB holds any non-MOCK bars (i.e. real collected data)."""
    conn = get_db_connection(dsn)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM bars WHERE source IS NOT NULL AND source <> 'MOCK';"
        ).fetchone()
    except Exception:
        return  # table may not exist yet
    finally:
        conn.close()
    if row and row[0]:
        raise SystemExit(
            f"Refusing to --reset '{describe_dsn(dsn)}': it contains {row[0]} non-MOCK bar(s). "
            f"Point --db at a scratch database, or pass --force if you really mean it."
        )


def main(reset=False, dsn=None, force=False):
    dsn = dsn or Config.DEV_DATABASE_URL

    if reset:
        if not force:
            _assert_mock_safe(dsn)
        logger.info(f"Resetting dev database at {describe_dsn(dsn)} ...")
        reset_database(dsn)
    else:
        init_database(dsn)

    logger.info(f"Populating mock data into {describe_dsn(dsn)}")
    conn = get_db_connection(dsn)
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=8)

        trading_days = []
        d = start_date
        while d <= end_date:
            if d.weekday() < 5:
                trading_days.append(d)
            d += timedelta(days=1)

        for contract_id, symbol, base_price, vol_multiplier in MOCK_CONTRACTS:
            _populate_instrument(
                conn, contract_id, symbol, base_price, vol_multiplier,
                start_date, end_date, trading_days,
            )

        logger.info("Successfully populated all mock trading pipeline tables!")
    except Exception:
        logger.exception("Failed to populate database")
        raise
    finally:
        conn.close()


def _populate_instrument(conn, contract_id, symbol, base_price, vol_multiplier,
                         start_date, end_date, trading_days):
    """
    Fabricates bars for one contract, plus snapshots, predictions, outcomes and
    analogues when it is a forecast target. A cash index gets bars only: it is
    pre-open context for the futures, never forecast itself.
    """
    instrument = Config.instrument(symbol)
    expiry = Config.expiry_for(symbol)
    scale = _scale(base_price)

    upsert_contract(
        conn, contract_id=contract_id, symbol=symbol, expiry=expiry,
        exchange=instrument.exchange, currency="USD",
        tick_size=instrument.tick_size, multiplier=instrument.multiplier,
        sec_type=instrument.sec_type,
    )
    logger.info(f"Upserted contract {contract_id} ({symbol} {expiry or 'index'}).")

    candles = generate_mock_candles(
        contract_id, start_date, end_date, base_price,
        has_volume=not instrument.is_index, vol_multiplier=vol_multiplier,
    )
    logger.info(f"{symbol}: generated {len(candles)} mock 1-minute bars.")
    # Written one trading day at a time, the same way the collector stores real data.
    saved = save_bars_by_day(conn, candles, interval="1m", price_type="TRADES", source="MOCK")
    logger.info(f"{symbol}: stored {len(saved)} trading day(s) into session_days + bars.")

    if instrument.is_index:
        return

    for i, t_day in enumerate(trading_days):
        t_day_str = t_day.strftime("%Y-%m-%d")
        cutoff_utc = NY_TZ.localize(
            datetime(t_day.year, t_day.month, t_day.day, 9, 30)
        ).astimezone(pytz.utc).isoformat()

        prev_close = base_price + i * 50 * scale
        gap = random.uniform(-120.0, 120.0) * scale
        direction = "UP" if gap > 15 * scale else ("DOWN" if gap < -15 * scale else "FLAT")
        bias = _direction_to_bias(direction)

        # Synthetic pre-open VIX context. Risk-off opens print a higher VIX, so it
        # leans against the gap rather than being independent noise.
        vix_change = round(random.uniform(-2.0, 2.0) - (gap / scale) * 0.01, 2)
        vix_level = round(max(9.0, 18.0 + vix_change), 2)

        snapshot_id = save_feature_snapshot(
            conn, contract_id=contract_id, timestamp_utc=cutoff_utc,
            previous_rth_high=prev_close + 150.0 * scale,
            previous_rth_low=prev_close - 50.0 * scale,
            previous_rth_close=prev_close,
            overnight_high=prev_close + max(0.0, gap) + 40 * scale,
            overnight_low=prev_close + min(0.0, gap) - 40 * scale,
            overnight_range=(80.0 * scale) + abs(gap),
            gap=gap,
            pre_open_direction=direction,
            historical_volatility=round(random.uniform(0.006, 0.02), 4),
            vwap=prev_close + gap * 0.6,
            raw_features={
                "rth_volume_sma_20": random.randint(150_000, 300_000),
                "atr_14": round(random.uniform(40, 85) * scale, 2),
            },
            feature_version=Config.FEATURE_VERSION,
            vix_pre_open=vix_level,
            vix_change=vix_change,
        )

        bull = round(random.uniform(20.0, 55.0), 1)
        chop = round(random.uniform(15.0, 40.0), 1)
        bear = round(100.0 - bull - chop, 1)
        probabilities = {
            "bullish_continuation_pct": bull,
            "mean_reversion_gap_fill_pct": chop,
            "bearish_rejection_pct": bear,
        }
        scenarios = {
            "primary_scenario": {
                "name": f"{bias.title()} opening drive",
                "description": f"Pre-open gap of {gap:.1f} pts; analogues lean {direction.lower()}.",
                "triggers": [f"Break of overnight extreme near {prev_close + gap:.0f}"],
                "invalidations": [f"Reclaim of prior close {prev_close:.0f}"],
            },
            "alternative_scenario": {
                "name": "Gap fill / mean reversion",
                "description": "Failure at the open rotates price back toward the prior close.",
                "triggers": [f"Rejection at {prev_close + gap:.0f}"],
                "invalidations": [f"Acceptance beyond {prev_close + gap * 1.5:.0f}"],
            },
        }
        raw_resp = (
            f"{symbol} FORECAST {t_day_str}: gap {gap:.1f} pts, bias {bias}. "
            f"P(bull)={bull:.0f}%, P(chop)={chop:.0f}%, P(bear)={bear:.0f}%."
        )

        prediction_id = save_prediction(
            conn, contract_id=contract_id, forecast_cutoff=cutoff_utc,
            snapshot_id=snapshot_id, model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION, opening_bias=bias,
            scenarios=scenarios, probabilities=probabilities,
            raw_response=raw_resp, created_at=cutoff_utc,
        )

        rth_h = prev_close + gap + random.uniform(50, 200) * scale
        rth_l = prev_close + gap - random.uniform(50, 200) * scale
        rth_c = random.uniform(rth_l, rth_h)
        save_outcome(
            conn, contract_id=contract_id, session_date=t_day_str,
            first_15_minute_high=prev_close + gap + 30.0 * scale,
            first_15_minute_low=prev_close + gap - 30.0 * scale,
            first_15_minute_close=prev_close + gap + random.uniform(-10, 10) * scale,
            first_30_minute_high=prev_close + gap + 45.0 * scale,
            first_30_minute_low=prev_close + gap - 45.0 * scale,
            first_30_minute_close=prev_close + gap + random.uniform(-20, 20) * scale,
            initial_balance_high=prev_close + gap + 65.0 * scale,
            initial_balance_low=prev_close + gap - 65.0 * scale,
            rth_high=rth_h, rth_low=rth_l, rth_close=rth_c,
            raw_outcomes={
                "rth_open": prev_close + gap,
                "vwap_rth": round((rth_h + rth_l + rth_c) / 3, 2),
                "daily_high_time": "14:15:00",
                "daily_low_time": "10:05:00",
            },
        )

        # Analogues: only strictly-earlier trading days.
        prior_idx = list(range(i))
        if prior_idx:
            chosen = random.sample(prior_idx, min(3, len(prior_idx)))
            matches = [
                {
                    "match_date": trading_days[idx].strftime("%Y-%m-%d"),
                    "similarity_score": round(random.uniform(0.82, 0.98), 4),
                    "ranking": rank + 1,
                }
                for rank, idx in enumerate(chosen)
            ]
            save_analogue_matches(conn, prediction_id, matches)

        logger.info(
            f"{symbol} {t_day_str}: bias={bias}, snapshot={snapshot_id}, prediction={prediction_id}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate a dev DB with mock data")
    parser.add_argument("--db", default=None, help="Target PostgreSQL URL (default: DEV_DATABASE_URL)")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate all tables first")
    parser.add_argument("--force", action="store_true", help="Allow --reset even on a DB with real data")
    args = parser.parse_args()
    main(reset=args.reset, dsn=args.db, force=args.force)
