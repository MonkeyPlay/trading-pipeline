# tests/test_fan_cond_ema.py
"""Conditional EMA Direction (forecaster/fan_cond_ema.py): point in time, the 5-minute bars, and the pipeline on
synthetic data - a planted conditional relationship it must recover, and a no-signal control."""

import numpy as np
import pytest

from forecaster import fan_cond_ema as ce
from forecaster import fan_power as fp
from forecaster import fan_harness as fh
from forecaster.fan_benchmark import DAY_SLOTS
from forecaster.fan_features import Feature, FeatureTable
from forecaster.fan_scoring import PHASE_OF_SLOT, PHASES


def _bars(sessions=3, seed=0):
    rng = np.random.default_rng(seed)
    lp = np.cumsum(rng.normal(0, 3e-4, sessions * DAY_SLOTS)) + np.log(20000)
    close = np.exp(lp).reshape(sessions, DAY_SLOTS)
    age = np.zeros(close.shape, dtype=np.float32)
    quiet = rng.random(close.shape) < 0.2                  # minutes without a bar: carried, age > 0
    age[quiet] = 1
    volume = np.where(quiet, 0, rng.integers(1, 50, close.shape)).astype(float)
    return close, age, volume


@pytest.mark.parametrize("t", [700, 702, 704])           # inside a 5-minute bar, and its last minute
def test_changing_future_bars_changes_no_earlier_feature(t):
    close, age, volume = _bars()
    full = ce.features(close, age, volume)
    s = 1
    c2, a2, v2 = close.copy(), age.copy(), volume.copy()
    c2[s, t + 1:] *= 1.03
    c2[s + 1:] *= 0.95
    a2[s, t + 1:] = 0
    v2[s, t + 1:] = 777
    later = ce.features(c2, a2, v2)
    for k in full:
        np.testing.assert_allclose(later[k][:s], full[k][:s], equal_nan=True, err_msg=k)
        np.testing.assert_allclose(later[k][s, :t + 1], full[k][s, :t + 1], equal_nan=True, err_msg=k)


def test_five_minute_ema_moves_only_when_a_bar_completes():
    close, age, volume = _bars(sessions=2)
    lp = np.log(close.reshape(-1))
    E = ce.emas(lp, age.reshape(-1) == 0)["m5_14"]
    i = np.arange(len(lp))
    changed = np.flatnonzero(np.diff(E) != 0) + 1
    changed = changed[np.isfinite(E[changed])]
    assert len(changed) and (i[changed] % 5 == 4).all()
    # a forming bar's minutes never reach the 5-minute EMA: change the first four minutes of a bar
    k = 2000 - 2000 % 5
    lp2 = lp.copy()
    lp2[k:k + 4] += 0.01
    E2 = ce.emas(lp2, age.reshape(-1) == 0)["m5_14"]
    np.testing.assert_allclose(E2[:k + 4], E[:k + 4], equal_nan=True)


def test_warm_up_is_three_periods_of_own_bars():
    close, age, volume = _bars(sessions=2)
    age[:] = 0
    E = ce.emas(np.log(close.reshape(-1)), np.ones(close.size, dtype=bool))
    assert np.isnan(E["m1_100"][:299]).all() and np.isfinite(E["m1_100"][299])
    assert np.isnan(E["m5_14"][:5 * 42 - 1]).all() and np.isfinite(E["m5_14"][5 * 42 - 1])


# --------------------------------------------------------------------------
# The pipeline on synthetic sessions
# --------------------------------------------------------------------------

SIGN = fp.SIGN


def _synthetic(kappa, n_sessions=150, seed=0):
    """Sessions whose next moves depend on a persistent input stored as m5_gap, by phase (forecaster/fan_power.py)."""
    return fp.synthetic(kappa, n_sessions, seed)[:4]


def test_pipeline_recovers_a_planted_conditional_relationship():
    frames, table, blocks, sessions = _synthetic(kappa=0.05)
    res = ce.run(frames, table, blocks, sessions, make_width=fh.Identity)
    act = res["blocks"][0]["active"]
    assert act["ema|h15"] and act["ema|h5"]
    for h in (5, 15):
        d = res["results"][f"h{h}"]["decision"]
        assert d["ema-zero"]["interval"][1] < 0 and d["ema-context"]["interval"][1] < 0
    assert res["conclusion"]["verdict"] == "pass"
    # the relationship is conditional: the phases that carry a sign gain
    diag = res["results"]["h15"]["diagnostics"]["phase"]
    assert all(diag[p]["ema-zero"]["diff"] < 0 for p in SIGN)


@pytest.mark.parametrize("seed", [1, 2])
def test_no_signal_control_stays_at_zero(seed):
    frames, table, blocks, sessions = _synthetic(kappa=0.0, seed=seed)
    res = ce.run(frames, table, blocks, sessions, make_width=fh.Identity)
    assert res["conclusion"]["verdict"] != "pass"
    assert not any(res["blocks"][0]["active"].values())


def test_forecasts_ignore_future_bars():
    """A fitted arm's shift at an origin is the same whatever happens after it."""
    frames, table, blocks, sessions = _synthetic(kappa=0.05, n_sessions=100)
    shape_of = lambda d, h: frames[d].shape[fh.FRAME_HORIZONS.index(h)]
    width, shifts, _ = ce.fit_block(frames, table, sessions[:-1], shape_of, fh.Identity)
    d = sessions[-1]
    rows = ce.fd._rows(frames, [d], 1)
    early = rows.slot <= 600
    before = shifts[("ema", 15)].zhat(ce.design(table, rows, "ema"))
    X2 = table.X.copy()
    X2[-1, 601:] = np.random.default_rng(9).normal(size=X2[-1, 601:].shape)
    t2 = FeatureTable(table.sessions, table.target, table.features, X2)
    after = shifts[("ema", 15)].zhat(ce.design(t2, rows, "ema"))
    np.testing.assert_array_equal(before[early], after[early])


def test_definition_is_fixed():
    assert ce.definition_hash() == ce.definition_hash()
    assert set(ce.DEFINITION["decision"]) >= {"pass", "fail", "inconclusive"}
