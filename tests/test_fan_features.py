# tests/test_fan_features.py
"""
The intermarket experiment's features (forecaster/fan_features.py, chunk 4): their tags,
no look-ahead within a day or across sessions, missing until there is history, the
previous regular close (13:00 after an early close), known values on a deterministic
market, and the rows' feature matrix.
"""

from datetime import date

import numpy as np
import pytest

from features import calendar as cal
from forecaster import fan_features as ff
from forecaster import fan_harness as fh
from forecaster.fan_benchmark import DAY_SLOTS
from forecaster.fan_panel import Panel

A = 1e-4                       # every minute's move on the deterministic market
SYMBOLS = ["NQ", "VIX", "SPY"]


def _panel(seed=None):
    """Every open session from 2025-10-20 to 2025-12-05 (Thanksgiving closed, 2025-11-28 an early close). Without a
    seed: each minute moves +-A alternately (r^2 = A^2 everywhere); with one, a Gaussian walk."""
    sessions = [s.session_date.isoformat() for s in cal.sessions_between(date(2025, 10, 20), date(2025, 12, 5))]
    n, k = len(sessions), len(SYMBOLS)
    if seed is None:
        r = np.tile(np.where(np.arange(DAY_SLOTS) % 2 == 0, A, -A), (n, k, 1))
    else:
        r = np.random.default_rng(seed).normal(0, A, (n, k, DAY_SLOTS))
    close = 100 * np.exp(np.cumsum(r, axis=2))
    return Panel(sessions, list(SYMBOLS), close, np.zeros((n, k, DAY_SLOTS), np.float32),
                 np.full((n, k, DAY_SLOTS), 50, np.float32), np.ones((n, k), np.int64))


START = {s: "2025-10-20" for s in SYMBOLS}


def _col(table, name):
    return [f.name for f in table.features].index(name)


def test_features_are_tagged_with_their_instrument_and_group():
    cat = ff.catalogue(["NQ", "ES", "VIX", "SPY"], "NQ")
    by = {f.name: f for f in cat}
    assert [f.name for f in cat[:4]] == list(ff.BASE) and all(f.group == "base" for f in cat[:4])
    assert by["NQ.rv15"].group == "own" and by["ES.rv15"].group == "index_futures"
    assert by["VIX.level"].group == "volatility" and "VIX.vol60" not in by          # an index has no volume
    assert "SPY.vol60" in by and "SPY.level" not in by and by["SPY.chg"].instrument == "SPY"
    assert ff.catalogue(["ES"], "ES")[4].group == "own"                             # the target is always 'own'


def test_the_usual_is_the_mean_of_the_sessions_before_with_enough_of_them():
    A_ = np.arange(6, dtype=float)[:, None] * np.ones((6, 2))
    src = np.array([True, True, False, True, True, True])
    u = ff._usual(A_, src, k=2, kmin=2)
    assert np.isnan(u[:2]).all()                                  # fewer than two sources before
    assert u[2, 0] == 0.5 and u[3, 0] == 0.5 and u[4, 0] == 2.0 and u[5, 0] == 3.5   # session 2 is never a source


def test_nothing_after_the_origin_or_in_a_later_session_is_read():
    panel = _panel(seed=4)
    table = ff.build(panel, "NQ", START)
    i, t = 25, 700
    later = _panel(seed=4)
    later.close[i, :, t + 1:] *= 1.02
    later.close[i + 1:] *= 0.97
    again = ff.build(later, "NQ", START)
    assert np.array_equal(table.X[:i], again.X[:i], equal_nan=True)
    assert np.array_equal(table.X[i, :t + 1], again.X[i, :t + 1], equal_nan=True)
    assert not np.array_equal(table.X[i, t + 1:], again.X[i, t + 1:], equal_nan=True)


def test_a_feature_is_missing_until_it_has_history_and_before_its_instrument_starts():
    panel = _panel(seed=5)
    table = ff.build(panel, "NQ", {**START, "VIX": panel.sessions[12]})
    nq, vix = _col(table, "NQ.rv60"), _col(table, "VIX.rv60")
    full = [cal.session(date.fromisoformat(d)).schedule == "full" for d in panel.sessions]
    first = np.flatnonzero(np.cumsum(full) > ff.NORM_MIN)[0]          # the first session with NORM_MIN full ones before
    assert np.isnan(table.X[:first, 800, nq]).all() and np.isfinite(table.X[first:, 800, nq]).all()
    assert np.isnan(table.X[:12, :, _col(table, "VIX.age")]).all()     # nothing before its first complete session
    vfirst = 12 + np.flatnonzero(np.cumsum(full[12:]) > ff.NORM_MIN)[0]
    assert np.isnan(table.X[:vfirst, 800, vix]).all() and np.isfinite(table.X[vfirst, 800, vix])


def test_known_values_on_a_deterministic_market():
    panel = _panel()
    i, t = len(panel.sessions) - 1, 900
    lp = np.log(panel.close[i, 0])
    lp[t - 59:t + 1] = lp[t - 60] + np.cumsum(np.where(np.arange(60) % 2 == 0, 2 * A, -2 * A))   # twice the move
    lp[t + 1:] = lp[t] + np.cumsum(np.where(np.arange(DAY_SLOTS - t - 1) % 2 == 0, -A, A))
    panel.close[i, 0] = np.exp(lp)
    table = ff.build(panel, "NQ", START)
    assert table.X[i, t, _col(table, "NQ.rv60")] == pytest.approx(np.log(4 + ff.RATIO_FLOOR), rel=1e-4)
    assert table.X[i, t, _col(table, "NQ.rv240")] == pytest.approx(np.log(1.75 + ff.RATIO_FLOOR), rel=1e-4)
    assert table.X[i, t, _col(table, "NQ.rv5")] == pytest.approx(np.log(4 + ff.RATIO_FLOOR), rel=1e-4)
    assert table.X[i - 1, t, _col(table, "NQ.rv60")] == pytest.approx(np.log(1 + ff.RATIO_FLOOR), rel=1e-4)
    trend = _panel()
    trend.close[i, 0, t - 14:t + 1] = trend.close[i, 0, t - 15] * np.exp(A * np.arange(1, 16))   # 15 minutes up
    tt = ff.build(trend, "NQ", START)
    assert tt.X[i, t, _col(tt, "NQ.ret15")] == pytest.approx(np.sqrt(15), rel=1e-3)
    assert table.X[i, t, _col(table, "SPY.vol60")] == pytest.approx(0.0, abs=1e-6)          # usual volume
    assert table.X[i, t, _col(table, "VIX.level")] == pytest.approx(np.log(panel.close[i, 1, t]), rel=1e-6)


def test_implied_over_realised_reads_the_targets_previous_five_days():
    panel = _panel()
    table = ff.build(panel, "NQ", START)
    c = _col(table, "VIX.iv_rv")
    day = (DAY_SLOTS - 1) * A * A                         # every minute but the first moves A
    i, t = 12, 700
    want = np.log((panel.close[i, 1, t] / 100) ** 2 / ff.TRADING_DAYS / day)
    assert table.X[i, t, c] == pytest.approx(want, rel=1e-5)
    assert np.isnan(table.X[:ff.IV_RV_SESSIONS, :, c]).all()          # five sessions of the target first
    assert "NQ.iv_rv" not in [f.name for f in table.features] and "SPY.iv_rv" not in [f.name for f in table.features]
    rv5d = table.X[i, t, _col(table, "NQ.rv5d")]
    assert rv5d == pytest.approx(np.log(day), rel=1e-6)                 # the comparison's other side
    assert np.isnan(table.X[:ff.IV_RV_SESSIONS, :, _col(table, "NQ.rv5d")]).all()


def test_the_change_since_the_close_reads_13_00_after_an_early_close():
    panel = _panel(seed=6)
    early = panel.sessions.index("2025-11-28")
    base = ff.build(panel, "NQ", START)
    c = _col(base, "NQ.chg")
    moved = _panel(seed=6)
    moved.close[early, 0, 1139] *= 1.01                 # the bar ending 13:00 on the early close
    assert not np.allclose(ff.build(moved, "NQ", START).X[early + 1, :, c], base.X[early + 1, :, c],
                           equal_nan=True)
    after = _panel(seed=6)
    after.close[early, 0, 1319] *= 1.01                 # 16:00 is not that day's close
    assert np.array_equal(ff.build(after, "NQ", START).X[early + 1, :, c], base.X[early + 1, :, c], equal_nan=True)


def test_the_rows_matrix_joins_by_session_and_slot():
    panel = _panel(seed=7)
    table = ff.build(panel, "NQ", START)
    days = np.array([panel.sessions[20], panel.sessions[30]], dtype="datetime64[D]")
    rows = fh.Rows(days, np.array([600, 1000]), np.array([15, 15]), np.array([1, 3]), np.array([False, True]),
                   np.array([1e-6, 4e-6]), np.array([0.0, 0.0]), np.array([0.67, 0.67]))
    X, feats = table.matrix(rows)
    assert [f.name for f in feats[:4]] == list(ff.BASE) and len(feats) == 4 + len(table.features)
    assert np.allclose(X[:, 0], 0.5 * np.log([1e-6, 4e-6])) and list(X[:, 1]) == [0, 1] and list(X[:, 2]) == [600, 1000]
    weekdays = [date.fromisoformat(panel.sessions[k]).weekday() for k in (20, 30)]
    assert list(X[:, 3]) == weekdays
    assert np.array_equal(X[1, 4:], table.X[30, 1000], equal_nan=True)
    own, feats_own = table.matrix(rows, keep=lambda f: f.group == "own")
    assert {f.group for f in feats_own} == {"base", "own"} and own.shape[1] == len(feats_own)
    with pytest.raises(KeyError):
        table.matrix(fh.Rows(*(np.array([v[0]]) for v in (np.array(["2026-01-05"], dtype="datetime64[D]"),
                                                            [600], [15], [1], [False], [1e-6], [0.0], [0.67]))))


def test_spearman():
    x = np.arange(100.0)
    assert ff.spearman(x, x ** 3) == pytest.approx(1.0) and ff.spearman(x, -x) == pytest.approx(-1.0)
    assert ff.spearman(x[:10], x[:10]) is None                  # too few
