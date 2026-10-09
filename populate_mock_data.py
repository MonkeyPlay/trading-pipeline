# populate_mock_data.py
"""
Populate the database with realistic mock 1-minute bars for the last few
trading days, so the dashboard can be verified without a live IB connection.

Covers every instrument in MOCK_CONTRACTS, each fabricated around its own index
level: the ES, NQ and RTY futures, and cash VIX as pre-open context for them.

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
from database.queries import upsert_contract, save_bars_by_day
from features.session_windows import convert_utc_to_ny, get_trading_day_date
from config import Config

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
        init_database(dsn, apply=True)                # a development database

    logger.info(f"Populating mock data into {describe_dsn(dsn)}")
    conn = get_db_connection(dsn)
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=8)

        for contract_id, symbol, base_price, vol_multiplier in MOCK_CONTRACTS:
            _populate_instrument(conn, contract_id, symbol, base_price, vol_multiplier, start_date, end_date)

        logger.info("Successfully populated all mock trading pipeline tables!")
    except Exception:
        logger.exception("Failed to populate database")
        raise
    finally:
        conn.close()


def _populate_instrument(conn, contract_id, symbol, base_price, vol_multiplier, start_date, end_date):
    """Fabricates and stores the bars of one contract. A cash index gets no volume."""
    instrument = Config.instrument(symbol)
    expiry = Config.expiry_for(symbol)

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate a dev DB with mock data")
    parser.add_argument("--db", default=None, help="Target PostgreSQL URL (default: DEV_DATABASE_URL)")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate all tables first")
    parser.add_argument("--force", action="store_true", help="Allow --reset even on a DB with real data")
    args = parser.parse_args()
    main(reset=args.reset, dsn=args.db, force=args.force)
