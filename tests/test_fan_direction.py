# tests/test_fan_direction.py
"""The direction experiment (forecaster/fan_direction.py): point-in-time features, the arms, the scoring."""

import numpy as np

from forecaster import fan_direction as fd
from forecaster import fan_harness as fh
from forecaster.fan_benchmark import DAY_SLOTS
from forecaster.fan_features import Feature, FeatureTable


def _prices(sessions=4, seed=0):
    rng = np.random.default_rng(seed)
    lp = np.cumsum(rng.normal(0, 3e-4, sessions * DAY_SLOTS)) + np.log(20000)
    close = np.exp(lp).reshape(sessions, DAY_SLOTS)
    volume = rng.integers(1, 50, close.shape).astype(float)
    return close, volume


def test_features_read_nothing_after_the_minute():
    close, volume = _prices()
    full = fd.direction_features(close, volume)
    s, t = 2, 700
    cut_c, cut_v = close.copy(), volume.copy()
    cut_c[s, t + 1:] = cut_c[s, t + 1:] * 1.05                    # a different future
    cut_c[s + 1:] *= 0.9
    cut_v[s, t + 1:] = 999
    later = fd.direction_features(cut_c, cut_v)
    for k in full:
        np.testing.assert_allclose(later[k][:s], full[k][:s], equal_nan=True, err_msg=k)
        np.testing.assert_allclose(later[k][s, :t + 1], full[k][s, :t + 1], equal_nan=True, err_msg=k)


def test_features_are_in_trailing_volatility_units_and_missing_before_warm_up():
    close, volume = _prices()
    f = fd.direction_features(close, volume)
    assert np.isnan(f["ret1"][0, :fd.VOL_WINDOW - 1]).all()
    lp = np.log(close.reshape(-1))
    i = DAY_SLOTS + 500
    r = np.diff(lp)[i - fd.VOL_WINDOW:i]
    sd = np.sqrt(np.mean(r * r))
    assert abs(f["ret1"].reshape(-1)[i] - (lp[i] - lp[i - 1]) / sd) < 1e-9
    for k in fd.DIRECTION_FAMILIES:
        assert np.nanmax(np.abs(f[k])) <= fd.CLIP


def _rows(z_signal=0.0, n_days=40, per_day=300, seed=1):
    """Synthetic rows at both horizons, with one input column that predicts z by ``z_signal``."""
    rng = np.random.default_rng(seed)
    days = np.repeat(np.arange(n_days), per_day)
    slots = np.tile(np.arange(per_day) * 4, n_days)
    parts = []
    X = np.full((n_days, DAY_SLOTS, len(fd.DIRECTION_FAMILIES) + len(fd.CONTEXT_FAMILIES) + 1), np.nan,
                dtype=np.float32)
    X[:, :, :] = rng.normal(size=X.shape)
    x0 = X[days, slots, 0].astype(float)
    for h in fd.HORIZONS:
        z = z_signal * x0 + rng.normal(size=len(days))
        sigma = np.full(len(days), 1e-3)
        parts.append(fh.Rows(np.datetime64("2026-01-01") + days.astype("timedelta64[D]"), slots, np.full(len(days), h),
                             np.zeros(len(days), dtype=int), np.zeros(len(days), dtype=bool), sigma ** 2, z * sigma,
                             np.full(len(days), 0.674)))
    feats = [Feature(f"NQ.{k}", "NQ", fd.GROUP, k) for k in (*fd.DIRECTION_FAMILIES, *fd.CONTEXT_FAMILIES)]
    feats.append(Feature(f"NQ.{fd.EMA_RAW}", "NQ", fd.GROUP, fd.EMA_RAW))
    sessions = [str(np.datetime64("2026-01-01") + np.timedelta64(int(d), "D")) for d in range(n_days)]
    return fh.Rows.concat(parts), FeatureTable(sessions, "NQ", feats, X)


def test_ridge_finds_a_planted_signal_and_shrinks_to_zero_without_one():
    rows, table = _rows(z_signal=0.3)
    sigma = np.sqrt(rows.var)
    r = fd.RidgeShift("NQ")
    r.fit(rows, table, sigma)
    assert r.state[5]["used"] and r.state[5]["coef"][0] > 0.15
    z = rows.y / sigma
    assert np.corrcoef(r.zhat(rows, table), z)[0, 1] > 0.2
    noise, table0 = _rows(z_signal=0.0, seed=3)
    r0 = fd.RidgeShift("NQ")
    r0.fit(noise, table0, np.sqrt(noise.var))
    # chance can still beat zero on the validation sessions (the guard is weak - docs/fan_direction.md), but the
    # chosen penalty keeps the shift small
    assert np.mean(np.abs(r0.zhat(noise, table0))) < 0.05
    assert np.max(np.abs(r.zhat(rows, table))) <= fd.Z_CAP
    again = fd.RidgeShift.from_state("NQ", r.describe())
    np.testing.assert_allclose(again.zhat(rows, table), r.zhat(rows, table))


def test_ema_damped_carries_half_the_slope():
    rows, table = _rows()
    step = table.X[:, :, -1]
    i = table.session_index(rows.session)
    mu = fd.EMADamped().shift(rows, table, np.sqrt(rows.var))
    np.testing.assert_allclose(mu, fd.DAMP * rows.horizon * step[i, rows.slot], rtol=1e-6)


def test_zero_drift_scores_as_the_harness_and_probabilities_add_up():
    rng = np.random.default_rng(5)
    Q = np.sort(rng.standard_t(5, size=fh.K))
    Q = (Q - Q[::-1]) / 2                                    # symmetric, as v2's shape
    rows, _ = _rows(n_days=1, per_day=200)
    fr = fh.Frame("2026-01-01", DAY_SLOTS, np.zeros(DAY_SLOTS), np.zeros((len(fh.FRAME_HORIZONS), DAY_SLOTS)),
                  np.tile(Q, (len(fh.FRAME_HORIZONS), 1)), np.zeros((len(fh.FRAME_HORIZONS), DAY_SLOTS), dtype=bool))
    sigma = np.sqrt(rows.var)
    mu = 0.3 * sigma * np.sign(rng.normal(size=len(rows)))
    rel = {}
    out = fd.score_session(fr, rows, sigma, {"zero": np.zeros(len(rows)), "ridge": mu}, rel)
    s = rows.horizon == 5
    expect = (sigma[s] * fh.crps(rows.y[s] / sigma[s], Q) * 1e4).mean()
    assert abs(out["h5"]["zero"]["crps"] - expect) < 1e-12
    p0 = fd.probabilities(np.zeros(len(rows)), sigma, Q)
    np.testing.assert_allclose(p0["up"], 0.5, atol=1e-12)
    p = fd.probabilities(mu, sigma, Q)
    np.testing.assert_allclose(p["below"] + p["within"] + p["above"], 1.0)
    assert ((p["up"] > 0.5) == (mu > 0)).all()
    assert rel[("zero", 5)]["n"] == s.sum()


def test_blocks_follow_the_checks_then_the_holdout():
    hold = [f"2026-07-{d:02d}" for d in range(1, 31)] + [f"2026-08-{d:02d}" for d in range(1, 31)]
    m = {"split": {"checks": {"blocks": [{"check": 1, "sessions": {"first": "2026-03-03", "last": "2026-04-14"}}]},
                   "holdout": {"sessions": hold}}}
    b = fd.blocks(m)
    assert [x["block"] for x in b] == ["check 1", "holdout 1", "holdout 2"]
    assert b[1]["first"] == hold[0] and b[2]["last"] == hold[-1]
