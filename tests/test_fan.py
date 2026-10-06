# tests/test_fan.py
"""
The benchmark price fan (contracts/fan.py, forecaster/fan_*.py, scripts/fan.py):
the minute grid, the estimators on markets with a known volatility, no look-ahead,
the scoring arithmetic and calibration, and - against a disposable database - the
loader and the CLI.
"""

import json
import math
import os
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import psycopg
import pytest

from contracts import fan as F
from contracts import nq_prompt_v2 as defs
from features import calendar as cal
from forecaster.fan_benchmark import (DAY_SLOTS, Day, InsufficientHistory, Release, crps_normal, day_start, end_slot,
                                      event_multipliers, fan_from, fit, horizon_variances, norm_cdf, seasonal,
                                      slot_at, slot_instant, slot_of_time)
from forecaster.fan_scoring import recent_accuracy, score_sessions, summarise

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)
UTC = timezone.utc
RELEASE_SLOT = slot_of_time(time(8, 30))
SLOTS = np.arange(DAY_SLOTS)
RTH = (SLOTS >= 930) & (SLOTS < 1320)
# A known per-minute sigma: 1 bp overnight, an RTH U-shape from 3 bp; nothing in the 17:00-18:00 halt.
SIGMA = np.where(RTH, 3e-4 * (1 + 2 * np.exp(-(SLOTS - 930) / 20) + 0.8 * np.exp(-(1320 - SLOTS) / 25)), 1e-4)
SIGMA[1380:] = 0.0


def market(n=60, seed=1, release_every=0, release_mult=9.0, levels=False, first=date(2026, 3, 2)):
    """``n`` full futures sessions of a Gaussian walk with per-minute sigma SIGMA (x sqrt(level) with ``levels``),
    a high-tier 08:30 release every ``release_every`` sessions (variance x ``release_mult`` in its minute)."""
    rng = np.random.default_rng(seed)
    sessions = [s for s in cal.sessions_between(first, first + timedelta(days=3 * n)) if s.schedule == "full"][:n]
    days, price, level = [], 20000.0, 1.0
    for k, s in enumerate(sessions):
        if levels:
            level = float(np.exp(0.8 * np.log(level) + rng.normal(0, 0.25)))
        mult = np.ones(DAY_SLOTS)
        rel = ()
        if release_every and k % release_every == 0:
            mult[RELEASE_SLOT] = release_mult
            rel = (Release(RELEASE_SLOT, "high", "Consumer Price Index", slot_instant(s.session_date, RELEASE_SLOT)),)
        r = rng.normal(0, 1, DAY_SLOTS) * SIGMA * np.sqrt(level * mult)
        closes = price * np.exp(np.cumsum(r))
        closes[1380:] = np.nan
        price = float(closes[1379])
        days.append(Day(s.session_date, "full", end_slot("FUT", "full"), closes, True, rel, True))
    return days


# --------------------------------------------------------------------------
# The grid
# --------------------------------------------------------------------------

def test_slots_and_day_ends():
    assert (slot_of_time(time(18, 0)), slot_of_time(time(4, 0)), slot_of_time(time(9, 30))) == (0, 600, 930)
    assert slot_of_time(time(16, 0)) == 1320
    assert end_slot("FUT", "full") == 1380 and end_slot("FUT", "early_close") == 1155
    assert end_slot("STK", "full") == 1440 and end_slot("STK", "early_close") == 1380
    with pytest.raises(ValueError):
        end_slot("IND", "full")
    # Monday after the spring DST change: the session opens Sunday 18:00 EDT (22:00 UTC)
    assert day_start(date(2026, 3, 9)) == datetime(2026, 3, 8, 22, 0, tzinfo=UTC)
    open_0930 = cal.ny_instant(date(2026, 3, 9), time(9, 30))
    assert slot_at(date(2026, 3, 9), open_0930) == 930 and slot_instant(date(2026, 3, 9), 930) == open_0930


def test_last_price_and_returns_keep_the_price_without_a_trade():
    closes = np.full(DAY_SLOTS, np.nan)
    closes[[3, 4, 7]] = [100.0, 101.0, 99.0]
    day = Day(date(2026, 3, 9), "full", 1380, closes)
    assert np.isnan(day.last_price[2]) and day.last_price[6] == 101.0 and day.last_price[1379] == 99.0
    r = day.returns
    assert np.isnan(r[:4]).all() and r[5] == 0.0 and math.isclose(r[7], math.log(99 / 101))
    assert np.isnan(r[1380:]).all()


# --------------------------------------------------------------------------
# Estimation
# --------------------------------------------------------------------------

def test_seasonal_recovers_the_intraday_pattern_and_skips_release_windows():
    days = market(40, seed=2, release_every=2, release_mult=400.0)
    S = seasonal(days)
    for slot in (300, 700, 960, 1100, 1300, RELEASE_SLOT):      # the x400 release minutes are left out of S
        assert 0.85 < math.sqrt(S[slot]) / SIGMA[slot] < 1.15, (slot, math.sqrt(S[slot]) / SIGMA[slot])
    assert (S[1380:] == 0).all()                                # the halt


def test_event_multipliers_estimate_shrink_and_fall_back():
    days = market(120, seed=3, release_every=1)
    S = SIGMA ** 2
    mult, occ = event_multipliers(S, days)
    assert occ["high"] == 120 and occ["fomc"] == 0
    assert 6.5 < mult["high"][0] < 12.0                 # truth 9 in the release minute
    assert 0.99 < mult["high"][1] < 1.4                 # nothing after it
    assert list(mult["fomc"]) == list(F.EVENT_GROUPS["fomc"]["fallback"])
    # One extreme release cannot carry the estimate: three releases' weight sit on the fallback.
    one = market(1, seed=4, release_every=1, release_mult=10_000.0)
    m1, _ = event_multipliers(S, one)
    single = (one[0].returns[RELEASE_SLOT] ** 2 / S[RELEASE_SLOT])
    assert m1["high"][0] < (single + 3 * F.EVENT_GROUPS["high"]["fallback"][0]) / 4 + 1e-9
    assert m1["high"][0] < single


def test_a_stock_fan_does_not_widen_while_the_market_is_closed():
    rng = np.random.default_rng(11)
    trading = (SLOTS < 120) | (SLOTS >= 600)            # after hours to 20:00, closed, premarket from 04:00
    days, price = [], 500.0
    for s in [s for s in cal.sessions_between(date(2026, 3, 2), date(2026, 4, 30)) if s.schedule == "full"][:30]:
        r = np.where(trading, rng.normal(0, 2e-4, DAY_SLOTS), 0.0)
        closes = price * np.exp(np.cumsum(r))
        closes[~trading] = np.nan
        price = float(closes[-1])
        days.append(Day(s.session_date, "full", end_slot("STK", "full"), closes))
    model = fit(days[-1], days[:-1])
    assert (model.S[121:600] == 0).all() and (model.S[650:1440] > 0).all()
    fan = fan_from(model, days[-1].returns, 100, float(days[-1].closes[100]))
    closed = (fan.slots > 126) & (fan.slots < 600)
    assert np.ptp(fan.sigma[closed]) == 0               # flat from the last after-hours minute to 04:00
    assert fan.sigma[-1] > fan.sigma[closed][0]


def test_fit_reads_only_earlier_complete_sessions():
    days = market(30, seed=5)
    with pytest.raises(InsufficientHistory):
        fit(days[8], days)                              # 8 earlier sessions
    later_changed = [d if d.session_date <= days[20].session_date else
                     Day(d.session_date, d.schedule, d.end, d.closes * 1.5, d.complete) for d in days]
    a, b = fit(days[20], days), fit(days[20], later_changed)
    assert np.array_equal(a.S, b.S) and a.level_long == b.level_long
    incomplete = [d if i != 15 else Day(d.session_date, d.schedule, d.end, d.closes, False) for i, d in enumerate(days)]
    assert days[15].session_date.isoformat() not in fit(days[20], incomplete).seasonal_sessions


def test_a_fan_never_reads_the_bars_after_its_origin():
    days = market(25, seed=6)
    target, t = days[-1], 1000
    model = fit(target, days[:-1])
    cut = target.closes.copy()
    cut[t + 1:] = np.nan
    shocked = target.closes.copy()
    shocked[t + 1:] *= 1.07
    fans = [fan_from(model, Day(target.session_date, "full", 1380, c).returns, t, float(target.closes[t]))
            for c in (target.closes, cut, shocked)]
    for f in fans[1:]:
        assert np.array_equal(f.sigma, fans[0].sigma) and np.array_equal(f.prices, fans[0].prices)


def test_scoring_variances_equal_the_issued_fan():
    days = market(25, seed=7, release_every=3, levels=True)
    target = days[-1]
    model = fit(target, days[:-1])
    V = horizon_variances(model, target.returns, F.SCORE_HORIZONS)
    for t in (5, 700, 935, 1200):
        fan = fan_from(model, target.returns, t, float(target.last_price[t]))
        for i, h in enumerate(F.SCORE_HORIZONS):
            if t + h < 1380:
                assert math.isclose(V["full"][i][t], fan.sigma[h - 1] ** 2, rel_tol=1e-9)
            else:
                assert np.isnan(V["full"][i][t])
    fan = fan_from(model, target.returns, 1000, 21000.0)
    assert len(fan.minutes) == 379                      # to the 16:59 bar
    mid = F.QUANTILES.index(0.5)
    assert np.allclose(fan.prices[:, mid], 21000.0)     # zero drift: the median stays at the origin price
    assert (np.diff(fan.prices[:, 0]) <= 1e-9).all()    # the 1% quantile only widens


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def test_normal_cdf_and_crps():
    z = np.linspace(-6, 6, 241)
    assert np.max(np.abs(norm_cdf(z) - np.array([0.5 * (1 + math.erf(x / math.sqrt(2))) for x in z]))) < 2e-7
    for y, s in ((0.0, 1.0), (0.7, 0.5), (-2.0, 1.3)):
        grid = np.linspace(-15, 15, 300001)
        cdf = 0.5 * (1 + np.vectorize(math.erf)(grid / (s * math.sqrt(2))))
        numeric = np.trapezoid((cdf - (grid >= y)) ** 2, grid)
        assert math.isclose(float(crps_normal(np.array(y), np.array(s))), numeric, rel_tol=1e-4)


def test_the_fan_is_calibrated_on_a_known_market():
    days = market(60, seed=8, release_every=5, levels=True)
    res = score_sessions(days, days[45].session_date, days[-1].session_date)
    assert len(res["scores"]) == 15 and not res["skipped"]
    summary = {e["horizon"]: e for e in summarise(res["scores"])}
    for h in (5, 15, 60):
        full = summary[h]["variants"]["full"]
        assert 0.93 < full["z_rms"] < 1.07, (h, full["z_rms"])
        assert abs(full["coverage"]["0.90"] - 0.90) < 0.03, (h, full["coverage"])
        assert summary[h]["paired"]["seasonal-flat"]["diff_bps"] < 0   # the intraday pattern beats one variance
    rel = summary[1]["release_ahead"]
    assert rel["seasonal_events"]["z_rms"] < rel["seasonal"]["z_rms"]   # the bump widens the release minute


def test_recent_accuracy_scores_the_last_sessions_before_a_day():
    """The chart's accuracy fade: the last sessions before the day, walk-forward - skill against flat, coverage."""
    days = market(40, seed=8)
    target = days[-1].session_date
    acc = recent_accuracy(days, target, sessions=10)
    assert acc["sessions"] == 10 and acc["last"] == days[-2].session_date.isoformat()   # never the day itself
    rows = {r["horizon"]: r for r in acc["horizons"]}
    assert set(rows) == set(F.SCORE_HORIZONS)
    for h in (1, 15, 60):
        assert rows[h]["skill"] > 0                         # the intraday pattern knows more than one variance
        assert abs(rows[h]["cover90"] - 0.90) < 0.04 and 0.4 < rows[h]["cover50"] < 0.6
    assert recent_accuracy(days, days[0].session_date, sessions=10) == {"sessions": 0, "first": None, "last": None,
                                                                         "horizons": []}


def test_scoring_skips_sessions_it_cannot_fit():
    days = market(14, seed=9)
    days[12] = Day(days[12].session_date, "full", 1380, days[12].closes, False)
    res = score_sessions(days, days[0].session_date, days[-1].session_date)
    assert res["skipped"] == {"insufficient_history": 10, "incomplete": 1}
    assert len(res["scores"]) == 3


def test_the_definition_is_registrable():
    rec = F.fan_record()
    assert rec["version"] == F.FAN_VERSION and rec["kind"] == "forecast_algorithm"
    assert rec["definition_hash"] == F.fan_record()["definition_hash"]
    json.loads(defs.canonical_json(rec["definition"]))
    assert F.event_group("FOMC rate decision", "high") == "fomc"
    assert F.event_group("FOMC minutes", "moderate") == "moderate" and F.event_group("x", "low") is None


# --------------------------------------------------------------------------
# Database: the loader and the CLI
# --------------------------------------------------------------------------

CID = 701


def _store(conn, days):
    import pandas as pd
    from database.queries import save_bars_by_day
    from features.session_windows import enrich_candle_timezones
    for d in days:
        idx = [slot_instant(d.session_date, s) for s in range(d.end) if np.isfinite(d.closes[s])]
        c = d.closes[np.isfinite(d.closes)]
        o = np.concatenate([[c[0]], c[:-1]])
        df = pd.DataFrame({"timestamp_utc": pd.DatetimeIndex(idx).strftime("%Y-%m-%d %H:%M:%S"), "open": o,
                           "high": np.maximum(o, c) + 0.25, "low": np.minimum(o, c) - 0.25, "close": c,
                           "volume": 10, "contract_id": CID})
        df = enrich_candle_timezones(df)
        save_bars_by_day(conn, df[["contract_id", "timestamp_utc", "trading_day", "session_scope", "open", "high",
                                   "low", "close", "volume"]].to_dict("records"))


@pytest.fixture(scope="module")
def stored():
    from database.connection import get_db_connection, reset_database
    from database.queries import set_active_contracts, upsert_contract
    reset_database(DSN)
    conn = get_db_connection(DSN)
    days = market(16, seed=10, release_every=4)
    upsert_contract(conn, CID, "NQ", "20261218", "CME", local_symbol="NQZ6")
    _store(conn, days)
    set_active_contracts(conn, "NQ", {d.session_date.isoformat(): CID for d in days}, "test")
    with conn:
        conn.execute("INSERT INTO economic_event_coverage (source, covered_from, covered_to) "
                     "VALUES ('bls', %s, %s);", (days[0].session_date, days[-1].session_date))
        for d in days:
            for r in d.releases:
                conn.execute("INSERT INTO economic_events (source, event_key, scheduled_at, name, tier) "
                             "VALUES ('bls', %s, %s, %s, 'high');", (f"cpi:{d.session_date}", r.at, r.name))
    yield conn, days
    conn.close()


@needs_db
def test_load_days_rebuilds_the_grid(stored):
    from forecaster.fan_data import load_days
    conn, days = stored
    loaded = load_days(conn, "NQ", days[0].session_date, days[-1].session_date)
    assert [d.session_date for d in loaded] == [d.session_date for d in days]
    for a, b in zip(loaded, days):
        assert np.allclose(a.closes[:1380], b.closes[:1380]) and np.isnan(a.closes[1380:]).all()
        assert a.complete and a.releases_known and a.contract_id == CID
        assert [r.slot for r in a.releases] == [r.slot for r in b.releases]
    as_of = cal.ny_instant(days[-1].session_date, time(10, 0))
    partial = load_days(conn, "NQ", days[-1].session_date, days[-1].session_date, as_of=as_of)[0]
    last = int(np.flatnonzero(np.isfinite(partial.closes))[-1])
    assert slot_instant(days[-1].session_date, last + 1) == as_of and not partial.complete


@needs_db
def test_cli_now_and_score(stored, tmp_path, capsys):
    import scripts.fan as cli
    conn, days = stored
    out = tmp_path / "fan.json"
    assert cli.main(["--db", DSN, "now", "--symbol", "NQ", "--as-of", f"{days[-1].session_date} 08:00",
                     "--json", str(out)]) == 0
    fan = json.loads(out.read_text())
    assert fan["version"] == F.FAN_VERSION and fan["origin"]["slot"] == slot_of_time(time(7, 59))
    assert len(fan["prices"]) == len(fan["minutes"]) == 1380 - 1 - fan["origin"]["slot"]
    assert len(fan["prices"][0]) == len(F.QUANTILES)
    assert cli.main(["--db", DSN, "score", "--symbol", "NQ", "--start", str(days[11].session_date),
                     "--end", str(days[-1].session_date), "--report-dir", str(tmp_path)]) == 0
    report = next(tmp_path.glob("fan_rw_v1_NQ_*.md")).read_text()
    assert "## The fan as issued (full)" in report and "5 scored" in report
    assert cli.main(["--db", DSN, "now", "--symbol", "NQ", "--as-of", "2026-10-03 12:00"]) == 1   # a Saturday
    assert "closed" in capsys.readouterr().out
