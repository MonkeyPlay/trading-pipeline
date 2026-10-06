# tests/test_fan_v2.py
"""
The benchmark fan, version 2 (forecaster/fan_v2.py) and the experiment's scoring
harness (forecaster/fan_harness.py): releases placed and estimated by name, earnings at
the close, the fat-tailed shape, no look-ahead, the manifest's CRPS - and, against a
disposable database, the baseline gate decided once.
"""

import os
from datetime import date, time, timedelta

import numpy as np
import psycopg
import pytest

from contracts import fan as F
from contracts import nq_prompt_v2 as defs
from features import calendar as cal
from forecaster import fan_benchmark as fb
from forecaster import fan_harness as fh
from forecaster import fan_v2 as v2
from forecaster.fan_benchmark import DAY_SLOTS, Day, Release, end_slot, slot_instant, slot_of_time
from tests.test_fan import RELEASE_SLOT, SIGMA, market

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)


def test_v1_stays_what_was_registered():
    """fan_rw_v1 is registered (2026-10-06): v2 lives beside it, never in it."""
    assert F.fan_record()["definition_hash"].startswith("d92d72a7e16b6ef9")
    assert F.fan_v2_record()["version"] == "fan_rw_v2" and F.FAN_V2["base"] == "fan_rw_v1"


def _day(releases, d=date(2026, 6, 10)):
    return Day(d, "full", end_slot("FUT", "full"), np.full(DAY_SLOTS, np.nan), True, tuple(releases), True)


def test_earnings_are_placed_at_the_close_and_other_releases_at_their_minute():
    d = date(2026, 6, 10)
    rel = lambda hhmm, name, source: Release(slot_of_time(time(*map(int, hhmm.split(":")))), "moderate" if source ==
                                             F.EARNINGS_SOURCE else "high", name,
                                             slot_instant(d, slot_of_time(time(*map(int, hhmm.split(":"))))), source)
    day = _day([rel("08:30", "Consumer Price Index", "bls"), rel("16:13", "AAPL earnings release (8-K 2.02)",
                F.EARNINGS_SOURCE), rel("09:05", "TSLA earnings release (8-K 2.02)", F.EARNINGS_SOURCE),
                rel("18:30", "NVDA earnings release (8-K 2.02)", F.EARNINGS_SOURCE)])
    assert v2.placed(day) == [("Consumer Price Index", "high", slot_of_time(time(8, 30))),
                              ("AAPL earnings release (8-K 2.02)", "earnings", slot_of_time(time(16, 0)))]
    windows = [(lo, hi) for name, g, b, lo, hi in v2.release_windows(day) if g == "earnings"]
    assert windows == [(1320, 1325), (1325, 1335), (1335, 1350), (1350, 1380)]          # 16:00 to the 17:00 halt


def _two_releases(n=60, seed=5):
    """A known market whose 08:30 release alternates: CPI (variance x 60 in its minute) and ISM (x 2)."""
    rng = np.random.default_rng(seed)
    sessions = [s for s in cal.sessions_between(date(2026, 3, 2), date(2026, 3, 2) + timedelta(days=3 * n))
                if s.schedule == "full"][:n]
    days, price = [], 20000.0
    for k, s in enumerate(sessions):
        mult, rel = np.ones(DAY_SLOTS), ()
        if k % 3 == 0:
            name, x = ("Consumer Price Index", 60.0) if k % 6 == 0 else ("ISM Manufacturing PMI", 2.0)
            mult[RELEASE_SLOT] = x
            rel = (Release(RELEASE_SLOT, "high", name, slot_instant(s.session_date, RELEASE_SLOT), "bls"),)
        closes = price * np.exp(np.cumsum(rng.normal(0, 1, DAY_SLOTS) * SIGMA * np.sqrt(mult)))
        closes[1380:] = np.nan
        price = float(closes[1379])
        days.append(Day(s.session_date, "full", end_slot("FUT", "full"), closes, True, rel, True))
    return days


def test_each_release_gets_its_own_multiplier_shrunk_towards_its_group():
    days = _two_releases()
    S = fb.seasonal(days[:40], windows=v2.release_windows)
    gm, nm, occ = v2.multipliers(S, days)
    cpi, ism, group = nm["Consumer Price Index"][1][0], nm["ISM Manufacturing PMI"][1][0], gm["high"][0]
    assert cpi > group > ism >= 1.0
    for name, est in (("Consumer Price Index", cpi), ("ISM Manufacturing PMI", ism)):
        mine = [d for d in days if d.releases and d.releases[0].name == name]
        raw = sum(d.returns[RELEASE_SLOT] ** 2 for d in mine) / (S[RELEASE_SLOT] * len(mine))
        assert min(raw, group) < est < max(raw, group)          # its own estimate, pulled towards the group's
    assert occ["name:Consumer Price Index"] == 10 and occ["high"] == 20
    model = v2.fit(days[-1], days[:-1])
    assert model.E[RELEASE_SLOT] == 1.0 or model.E[RELEASE_SLOT] in (cpi, ism)        # the target's own release


def test_the_shape_learns_fat_tails_and_stays_symmetric():
    rng = np.random.default_rng(3)
    errs = [{h: rng.standard_t(3, 2000) / np.sqrt(3) for h in (1, 15)} for _ in range(25)]
    shape = v2.shape_from(errs, (1, 15))
    q = shape.at(15)
    assert shape.sessions == 25 and q[0] < v2.NORMAL_Q[0] and abs(q[49]) < abs(v2.NORMAL_Q[49])   # tails, centre
    assert np.allclose(q, -q[::-1]) and abs(q[99] + q[100]) < 1e-12                           # symmetric, no drift
    assert v2.shape_from(errs[:19], (1, 15)).sessions == 0                                     # too few: the normal
    grid = v2.Shape(np.array([1, 10]), np.vstack([np.zeros(200), np.ones(200)]), 1)
    assert grid.at(1)[0] == 0 and grid.at(10)[0] == 1 and grid.at(1000)[0] == 1
    assert grid.at(np.sqrt(10))[0] == pytest.approx(0.5)                                       # log minutes


def test_a_v2_fan_never_reads_the_bars_after_its_origin():
    days = market(30, seed=11, release_every=5)
    target = days[-1]
    model = v2.fit(target, days[:-1])
    shape = v2.shape_from([v2.errors(v2.fit(d, days[:i]), d) for i, d in enumerate(days[:-1]) if i >= 10])
    t = 700
    fan = v2.fan_from(model, shape, target.returns, t, float(target.last_price[t]), 90)
    later = target.closes.copy()
    later[t + 1:1380] *= 1.01
    altered = Day(target.session_date, target.schedule, target.end, later, True, target.releases, True)
    again = v2.fan_from(v2.fit(altered, days[:-1]), shape, altered.returns, t, float(altered.last_price[t]), 90)
    assert np.array_equal(fan.prices, again.prices)
    assert np.all(np.diff(fan.prices, axis=1) > 0) and np.allclose(fan.prices[:, 6], target.last_price[t])


def test_the_manifests_crps_matches_the_closed_form_for_the_normal():
    z = np.linspace(-4, 4, 41)
    assert np.allclose(fh.crps(z, fh.NORMAL_Q), fb.crps_normal(z, np.ones_like(z)), rtol=5e-3, atol=2e-4)


def test_a_wider_version_scores_worse_on_identical_origins():
    days = market(24, seed=12)
    rows = []
    for i in range(12, 24):
        m = fb.fit(days[i], days[:i])
        rows.append(fh.compare_session(days[i], (m, lambda h: fh.NORMAL_Q), (m, lambda h: 1.6 * fh.NORMAL_Q),
                                       horizons=(5, 15)))
    p = fh.paired(rows, "h15")
    assert p["sessions"] == 12 and p["diff_bps"] > 0 and fh._verdict(p) == "worse"
    same = fh.paired([{k: (a, a, n) for k, (a, b, n) in r.items()} for r in rows], "h15")
    assert same["diff_bps"] == 0 and fh._verdict(same) == "inconclusive"
    assert "pre_open_16" in rows[0] and rows[0]["pre_open_16"][2] == 1                  # the slice's one origin


@needs_db
def test_the_baseline_gate_is_decided_once(tmp_path, capsys):
    from database import journal_store as store
    from database.connection import get_db_connection, reset_database
    from database.queries import set_active_contracts, upsert_contract
    from scripts.fan import main
    from tests.test_fan import CID, _store
    reset_database(DSN)
    conn = get_db_connection(DSN)
    days = market(26, seed=13, release_every=4)
    upsert_contract(conn, CID, "NQ", "20261218", "CME", local_symbol="NQZ6")
    _store(conn, days)
    set_active_contracts(conn, "NQ", {d.session_date.isoformat(): CID for d in days}, "test")
    dates = [d.session_date.isoformat() for d in days]
    manifest = {"targets": {"primary": "NQ"}, "horizons": {"primary": {"minutes": 15}},
                "baselines": {"v2_rule": "v2 replaces v1 when its interval lies below zero"},
                "data": {"excluded": {"NQ": []}},
                "split": {"development": {"sessions": dates, "last": dates[-1]},
                          "checks": {"blocks": [{"sessions": {"first": dates[14], "last": dates[-1]}}]},
                          "holdout": {"first": "2027-01-04", "last": "2027-03-31", "sessions": []}}}
    store.register_version(conn, defs._record("fan_gate_test", "fan_experiment", manifest))
    assert main(["--db", DSN, "baseline-gate", "--name", "fan_gate_test", "--report-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "12 check sessions scored" in out and "Decision (stored as result" in out
    stored = [r for r in store.experiment_results(conn, "fan_gate_test") if r["results"]["kind"] == "baseline_gate"]
    assert len(stored) == 1 and stored[0]["results"]["baseline"] in ("fan_rw_v1", "fan_rw_v2")
    assert store.get_version(conn, "fan_rw_v2")["definition_hash"] == F.fan_v2_record()["definition_hash"]
    assert (tmp_path / "fan_rw_v2_gate_fan_gate_test.md").exists()
    assert main(["--db", DSN, "baseline-gate", "--name", "fan_gate_test"]) == 0
    assert "it is not run again" in capsys.readouterr().out
    assert len(store.experiment_results(conn, "fan_gate_test")) == 1
    conn.close()
