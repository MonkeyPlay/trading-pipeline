# tests/test_fan_panel.py
"""
The point-in-time panel (forecaster/fan_panel.py): a minute's value is the close of
the last bar closed by then, with its age; nothing after the minute is read; a day is
read on its active contract, carry included, so a roll never mixes contracts; and,
against a disposable database, the loader, its agreement with the fan's own grid, the
audit and the CLI.
"""

import os
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg
import pytest

from contracts import nq_prompt_v2 as defs
from forecaster import fan_panel as fp
from forecaster.fan_benchmark import DAY_SLOTS, day_start

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)


def test_a_minute_reads_the_last_bar_closed_by_its_end():
    start = 1_000_000
    minutes = np.array([start - 30, start, start + 1, start + 5, start + DAY_SLOTS])    # the last is the next day's
    closes = np.array([9.0, 10.0, 11.0, 15.0, 99.0])
    close, age, volume = fp.series(start, minutes, closes, np.array([1, 2, 3, 4, 5], dtype=np.float32))
    assert close[0] == 10.0 and age[0] == 0                       # the bar starting in the slot closes with it
    assert close[2:5].tolist() == [11.0] * 3 and age[2:5].tolist() == [1, 2, 3]
    assert close[5] == 15.0 and close[-1] == 15.0 and age[-1] == DAY_SLOTS - 1 - 5
    assert 99.0 not in close                                      # the next day is never read
    assert volume[[0, 1, 5]].tolist() == [2, 3, 4] and volume.sum() == 9


def test_a_day_carries_from_before_it_within_the_carry_only():
    start = 1_000_000
    close, age, _ = fp.series(start, np.array([start - 30, start + 10]), np.array([9.0, 10.0]), np.ones(2))
    assert close[0] == 9.0 and age[0] == 30 and age[9] == 39 and close[10] == 10.0
    close, age, _ = fp.series(start, np.array([start - 3 * DAY_SLOTS]), np.array([9.0]), np.ones(1), carry=DAY_SLOTS)
    assert np.isnan(close).all() and np.isnan(age).all()


def test_the_panel_cannot_see_ahead():
    rng = np.random.default_rng(4)
    start = 2_000_000
    minutes = np.sort(rng.choice(np.arange(start - 500, start + DAY_SLOTS), 900, replace=False))
    closes = rng.normal(100, 1, len(minutes))
    full = fp.series(start, minutes, closes, np.ones(len(minutes)))
    for t in (0, 37, 600, 1439):
        keep = minutes <= start + t
        cut = fp.series(start, minutes[keep], closes[keep], np.ones(keep.sum()))
        for a, b in zip(full, cut):
            assert np.array_equal(a[:t + 1], b[:t + 1], equal_nan=True)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

DAYS = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]
OLD, NEW, VIX = 901, 902, 903           # NQ's expiring and next contracts; the VIX index


def _bars(cid, days, price, minutes):
    """Bars of contract ``cid`` at the grid ``minutes`` (slots from 18:00 ET) of each of ``days``."""
    rows = []
    for k, d in enumerate(days):
        start = day_start(date.fromisoformat(d))
        for s in minutes:
            rows.append({"contract_id": cid, "timestamp_utc": (start + timedelta(minutes=int(s))).strftime(
                "%Y-%m-%d %H:%M:%S"), "open": price(k, s), "high": price(k, s) + 1, "low": price(k, s) - 1,
                "close": price(k, s), "volume": 10})
    return rows


@pytest.fixture(scope="module")
def stored():
    from database.connection import get_db_connection, reset_database
    from database.queries import save_bars_by_day, set_active_contracts, upsert_contract
    from features.session_windows import enrich_candle_timezones
    reset_database(DSN)
    conn = get_db_connection(DSN)
    upsert_contract(conn, OLD, "NQ", "20260320", "CME")
    upsert_contract(conn, NEW, "NQ", "20260619", "CME")
    upsert_contract(conn, VIX, "VIX", None, "CBOE")
    futures = np.arange(0, 1380)
    vix = np.r_[fp._slot_of("03:15"):fp._slot_of("09:15"), fp._slot_of("09:30"):fp._slot_of("16:15")]
    rows = (_bars(OLD, DAYS, lambda k, s: 20000.0 + k * 10 + s / 1000, futures)
            + _bars(NEW, DAYS[1:3], lambda k, s: 20100.0 + (k + 1) * 10 + s / 1000, futures)   # warm-up days
            + _bars(NEW, DAYS[3:], lambda k, s: 20100.0 + (k + 3) * 10 + s / 1000, futures[10:])   # no 18:00-18:09
            + _bars(VIX, DAYS, lambda k, s: 18.0 + k + s / 10000, vix))
    df = enrich_candle_timezones(pd.DataFrame(rows))
    save_bars_by_day(conn, df[["contract_id", "timestamp_utc", "trading_day", "session_scope", "open", "high",
                               "low", "close", "volume"]].to_dict("records"))
    set_active_contracts(conn, "NQ", {**{d: OLD for d in DAYS[:3]}, **{d: NEW for d in DAYS[3:]}}, "test")
    set_active_contracts(conn, "VIX", {d: VIX for d in DAYS}, "test")
    yield conn
    conn.close()


@needs_db
def test_a_day_is_read_on_its_active_contract_carry_included(stored):
    panel = fp.load_panel(stored, DAYS[1:], ["NQ", "VIX"])
    assert panel.contract[:, 0].tolist() == [OLD, OLD, NEW, NEW]
    close, age, _ = panel.get("2026-03-05", "NQ")                 # the roll: the new contract's first day
    assert close[0] == pytest.approx(20100.0 + 2 * 10 + 1379 / 1000)    # carried from its own warm-up day
    assert age[0] == 1 + 60 and close[10] == pytest.approx(20100.0 + 3 * 10 + 10 / 1000) and age[10] == 0
    assert (close > 20090).all()                                  # never the expiring contract's prices
    vix_close, vix_age, _ = panel.get("2026-03-03", "VIX")
    last = fp._slot_of("16:14") - DAY_SLOTS                       # the day before's last bar, on the earlier grid
    assert vix_close[0] == pytest.approx(18.0 + 0 + (last + DAY_SLOTS) / 10000) and vix_age[0] == 0 - last
    assert vix_age[fp._slot_of("09:29")] == 15                    # the pause before the open


@needs_db
def test_the_panel_agrees_with_the_fans_own_grid(stored):
    from forecaster.fan_data import load_days
    panel = fp.load_panel(stored, DAYS, ["NQ"])
    for day in load_days(stored, "NQ", date(2026, 3, 2), date(2026, 3, 6)):
        close, _, _ = panel.get(day.session_date.isoformat(), "NQ")
        seen = np.isfinite(day.last_price)
        assert np.allclose(close[seen], day.last_price[seen])


@needs_db
def test_the_audit_and_its_cli_read_development_only(stored, tmp_path, capsys):
    from database import journal_store as store
    from scripts.fan import main
    rows = {r["symbol"]: r for r in fp.audit(fp.load_panel(stored, DAYS, ["NQ", "VIX"]),
                                             {"NQ": DAYS, "VIX": DAYS[1:]})}
    assert rows["NQ"]["hours"] == "18:00-17:00" and rows["VIX"]["hours"] == "03:15-09:15, 09:30-16:15"
    assert rows["VIX"]["median_age"]["09:29"] == 15 and rows["VIX"]["not_complete"] == ["2026-03-02"]
    assert rows["NQ"]["no_bar"] == [] and rows["NQ"]["bars_per_session"] == 1380
    manifest = {"split": {"development": {"sessions": DAYS[:3], "last": DAYS[2]},
                          "holdout": {"sessions": DAYS[3:], "first": DAYS[3], "last": DAYS[4]}},
                "instruments": {"availability": {"NQ": {"status": "included"}, "VIX": {"status": "included"}}}}
    store.register_version(stored, defs._record("fan_panel_test", "fan_experiment", manifest))
    assert main(["--db", DSN, "panel-audit", "--name", "fan_panel_test", "--report-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "3 development sessions (2026-03-02 to 2026-03-04)" in out
    report = (tmp_path / "fan_panel_audit_fan_panel_test_2026-03-02_2026-03-04.md").read_text()
    assert "| VIX | " in report and "03:15-09:15, 09:30-16:15" in report
