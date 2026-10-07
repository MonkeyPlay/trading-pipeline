# tests/test_fan_im_direction.py
"""Intermarket Direction (forecaster/fan_im_direction.py): point in time across markets, and the shared pipeline on
synthetic sessions - a planted intermarket relationship recovered, a no-signal control quiet."""

import numpy as np
import pytest

from forecaster import fan_cond_ema as ce
from forecaster import fan_harness as fh
from forecaster import fan_im_direction as im
from forecaster import fan_power as fp
from forecaster.fan_benchmark import DAY_SLOTS


def _markets(sessions=25, seed=0):
    rng = np.random.default_rng(seed)
    common = rng.normal(0, 3e-4, sessions * DAY_SLOTS)
    close, age = {}, {}
    for s, b in (("NQ", 1.2), ("ES", 1.0), ("RTY", 0.9)):
        lp = np.cumsum(b * common + rng.normal(0, 1e-4, common.size)) + np.log(5000)
        close[s] = np.exp(lp).reshape(sessions, DAY_SLOTS)
        age[s] = (rng.random((sessions, DAY_SLOTS)) < 0.1).astype(np.float32)
    return close, age


@pytest.mark.parametrize("t", [700, 704])
def test_no_feature_reads_a_later_bar_of_any_market(t):
    close, age = _markets()
    full = im.im_features(close, age)
    s = 22
    for m in ("NQ", "ES", "RTY"):
        c2 = {k: v.copy() for k, v in close.items()}
        a2 = {k: v.copy() for k, v in age.items()}
        c2[m][s, t + 1:] *= 1.04
        c2[m][s + 1:] *= 0.9
        a2[m][s, t + 1:] = 0
        later = im.im_features(c2, a2)
        for k in full:
            np.testing.assert_allclose(later[k][:s], full[k][:s], equal_nan=True, err_msg=f"{m} {k}")
            np.testing.assert_allclose(later[k][s, :t + 1], full[k][s, :t + 1], equal_nan=True, err_msg=f"{m} {k}")


def test_beta_reads_only_earlier_sessions_and_finds_the_relation():
    close, age = _markets()
    f = im.im_features(close, age)
    lp_nq, lp_es = np.log(close["NQ"]), np.log(close["ES"])
    i = 20
    assert np.isnan(f["NQ.rel_es15"][:im.BETA_MIN_SESSIONS]).all()
    close2 = {k: v.copy() for k, v in close.items()}
    close2["NQ"][i:] = close2["ES"][i:] ** 0.5 * 70             # a different relation from session i on
    f2 = im.im_features(close2, age)
    np.testing.assert_allclose(f2["NQ.rel_es15"][:i], f["NQ.rel_es15"][:i], equal_nan=True)
    beta = im._beta(np.diff(lp_nq, axis=1, prepend=np.nan), np.diff(lp_es, axis=1, prepend=np.nan),
                    np.isfinite(np.diff(lp_nq, axis=1, prepend=np.nan)))
    assert abs(beta[-1] - 1.08) < 0.05                       # 1.2 x 9 / (9 + 1): ES's own noise attenuates the slope


def test_comove_codes_together_and_diverging():
    close, age = _markets()
    f = im.im_features(close, age)
    v = f["NQ.comove15"][1:]
    assert set(np.unique(v[np.isfinite(v)])) <= {-1.0, 0.0, 1.0}
    assert np.nanmean(v == 0) < 0.3                         # mostly together: the markets share their moves


def _synthetic(kappa, seed=0, n_sessions=150):
    return fp.synthetic(kappa, n_sessions, seed, names=im.inputs("intermarket"), planted="ES.ret5")[:4]


def test_pipeline_recovers_a_planted_intermarket_relationship():
    frames, table, blocks, sessions = _synthetic(0.05)
    res = ce.run(frames, table, blocks, sessions, make_width=fh.Identity, spec=im.SPEC)
    act = res["blocks"][0]["active"]
    assert act["intermarket|h5"] and act["intermarket|h15"]
    assert not act["own|h5"] and not act["own|h15"]
    assert res["conclusion"]["verdict"] == "pass"
    assert set(res["results"]["h15"]["diagnostics"]) == {"phase", "comove15", "divergence"}


@pytest.mark.parametrize("seed", [4, 5])
def test_no_signal_control_stays_at_zero(seed):
    frames, table, blocks, sessions = _synthetic(0.0, seed)
    res = ce.run(frames, table, blocks, sessions, make_width=fh.Identity, spec=im.SPEC)
    assert not any(res["blocks"][0]["active"].values())
    assert res["conclusion"]["verdict"] == "fail"


def test_definition_names_the_shared_machinery():
    assert ce.definition_hash() in im.DEFINITION["machinery"]
    assert len(im.definition_hash()) == 16
