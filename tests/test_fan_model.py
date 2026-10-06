# tests/test_fan_model.py
"""
The learned fan (forecaster/fan_model.py, chunk 5): on a market that moves more or less
than the baseline expects by a factor only a feature reveals, the gradient-boosted multiplier finds it
and beats the baseline on the checks; it ignores a feature of noise; its multipliers
stay in bounds, are deterministic, and reach the pre-open slice through the nearest
trained horizon.
"""

from dataclasses import replace

import numpy as np
import pytest

from forecaster import fan_harness as fh
from forecaster import fan_model as fm
from forecaster.fan_benchmark import DAY_SLOTS
from forecaster.fan_features import Feature, FeatureTable
from tests.test_fan_checks import BLOCK, setup  # noqa: F401 - the shared synthetic market and manifest

SMALL = {**fm.SETTINGS, "min_samples_leaf": 100}      # the synthetic checks train on 8 sessions, not 150


@pytest.fixture(scope="module")
def mis_sized(setup):
    """The market moving 2x the baseline's sigma on odd sessions and 0.5x on even ones (realised variance x4 / x0.25;
    the baseline's own variance - and so base.log_sigma - unchanged), and a feature table: 'NQ.hint' reveals the
    factor, 'ES.noise' is noise."""
    days, frames, manifest = setup
    sessions = sorted(frames)
    rng = np.random.default_rng(9)
    factor = {d: (4.0 if i % 2 else 0.25) for i, d in enumerate(sessions)}
    wrong = {}
    for d, f in frames.items():
        anchor = f.log_price[np.isfinite(f.log_price)][0]
        wrong[d] = replace(f, log_price=anchor + np.sqrt(factor[d]) * (f.log_price - anchor))
    X = np.empty((len(sessions), DAY_SLOTS, 2), dtype=np.float32)
    X[:, :, 0] = np.log([factor[d] for d in sessions])[:, None]
    X[:, :, 1] = rng.normal(size=(len(sessions), DAY_SLOTS))
    table = FeatureTable(sessions, "NQ", [Feature("NQ.hint", "NQ", "own", "hint"),
                                          Feature("ES.noise", "ES", "index_futures", "noise")], X)
    return wrong, manifest, table, factor


def test_the_model_learns_a_width_a_feature_reveals_and_beats_the_baseline(mis_sized):
    wrong, manifest, table, _ = mis_sized
    res = fh.run_checks(wrong, manifest, "NQ", lambda: fm.GBMScale(table, ["NQ.hint"], "hinted", SMALL))
    assert res["candidate"] == "hinted"
    assert res["features"] == ["base.log_sigma", "base.release_ahead", "base.minute", "base.weekday", "NQ.hint"]
    assert res["features_hash"] == fm.feature_hash(res["features"])
    for k in ("h5", "h15", "h60"):
        assert res["verdicts"][k] == "better" and res["results"][k]["diff_share"] < -0.05
    blind = fh.run_checks(wrong, manifest, "NQ", lambda: fm.GBMScale(table, ["ES.noise"], "noise", SMALL))
    assert blind["verdicts"]["h15"] != "better" and blind["results"]["h15"]["diff_share"] > -0.01   # noise: no gain


def test_multipliers_follow_the_factor_stay_in_bounds_and_are_deterministic(mis_sized):
    wrong, _, table, factor = mis_sized
    sessions = sorted(wrong)
    train = fh.Rows.concat([fh.frame_rows(wrong[d], every=5) for d in sessions[:24]])
    a, b = fm.GBMScale(table, None, "a", SMALL), fm.GBMScale(table, None, "b", SMALL)   # None: every feature
    a.fit(train)
    b.fit(train)
    for d in sessions[24:28]:
        rows = fh.frame_rows(wrong[d])
        m = a.predict(rows)
        assert np.array_equal(m, b.predict(rows))
        assert np.all((m >= fm.BOUNDS[0]) & (m <= fm.BOUNDS[1]))
        at15 = np.median(m[rows.horizon == 15])
        assert (at15 > 1.25) if factor[d] > 1 else (at15 < 0.8)          # wider where v2 is too narrow
        slice16 = m[(rows.horizon == 16) & (rows.slot == fh.PRE_OPEN_ORIGIN)]
        assert len(slice16) == 1                                         # the slice reads the 15-minute model
    assert sorted(a.models) == sorted(fh.REPORT_HORIZONS) and a.features[0].name == "base.log_sigma"


def test_every_candidate_reads_an_explicit_feature_list(mis_sized):
    _, _, table, _ = mis_sized
    assert fm.FEATURE_SETS["own"]("NQ") == ["NQ.rv5", "NQ.rv15", "NQ.rv60", "NQ.rv240", "NQ.ret15", "NQ.ret60",
                                            "NQ.chg", "NQ.vol60", "NQ.age", "NQ.day_rv"]
    assert fm.FEATURE_SETS["own_ivx"]("NQ")[-3:] == ["NQ.rv5d", "VXN.level", "VXN.iv_rv"]
    assert "VXN.iv_rv" not in fm.FEATURE_SETS["own_ivx_linear"]("NQ")         # a linear model gets the two sides
    with pytest.raises(ValueError):
        fm.candidate("gbm_own", table)                                       # this table has no NQ.rv5: no silent drop
    with pytest.raises(ValueError):
        fm.candidate("gbm_everything", table)
    with pytest.raises(ValueError):
        fm.candidate("crps_ivx", table)                                      # trains on the CRPS: needs the frames


def test_the_crps_kit_is_the_manifests_crps_and_its_slope(mis_sized):
    wrong, _, _, _ = mis_sized
    days = sorted(wrong)[:3]
    rows = fh.Rows.concat([fh.frame_rows(wrong[d]) for d in days])
    kit = fm.CRPSKit(wrong)
    P = kit.prepare(rows)
    s = np.sqrt(rows.var) * np.exp(np.random.default_rng(1).normal(0, 0.3, len(rows)))
    loss, slope = kit.loss_grad(P, s)
    for d in days:
        fr = wrong[d]
        for h in (1, 15, 16, 240):
            sel = (rows.session.astype(str) == d) & (rows.horizon == h)
            want = s[sel] * fh.crps(rows.y[sel] / s[sel], fr.shape[fh.FRAME_HORIZONS.index(h)])
            assert np.allclose(loss[sel], want, rtol=1e-10, atol=1e-15)
    eps = 1e-7 * s
    up, _ = kit.loss_grad(P, s + eps)
    down, _ = kit.loss_grad(P, s - eps)
    smooth = np.abs(up - down - 2 * eps * slope) < 1e-6 * np.abs(up - down).max()   # away from a kink
    assert smooth.mean() > 0.99 and np.allclose(((up - down) / (2 * eps))[smooth], slope[smooth], rtol=1e-6)


def test_the_constant_benchmark_finds_the_crps_minimising_scale(mis_sized):
    wrong, _, _, _ = mis_sized
    days = sorted(wrong)
    rows = fh.Rows.concat([fh.frame_rows(wrong[d], every=5) for d in days[:12]])
    c = fm.ConstantCRPS(wrong)
    c.fit(rows)
    sub = rows.take(rows.horizon == 15)
    kit = fm.CRPSKit(wrong)
    P = kit.prepare(sub)
    total = lambda k: kit.loss_grad(P, P.sb * k)[0].sum()
    grid = np.exp(np.linspace(-1, 1, 401))
    best = grid[np.argmin([total(k) for k in grid])]
    assert c.scale[15] == pytest.approx(best, rel=0.01) and total(c.scale[15]) <= total(best) * (1 + 1e-9)


def test_the_crps_trained_and_linear_models_find_the_revealed_width(mis_sized):
    wrong, manifest, table, _ = mis_sized
    small = {**SMALL, "max_iter": 40, "learning_rate": 0.2}
    crps = fh.run_checks(wrong, manifest, "NQ", lambda: fm.CRPSBoost(table, wrong, ["NQ.hint"], "crps", small))
    lin = fh.run_checks(wrong, manifest, "NQ", lambda: fm.LinearScale(table, ["NQ.hint"], "lin"))
    for res in (crps, lin):
        assert res["verdicts"]["h15"] == "better" and res["results"]["h15"]["diff_share"] < -0.05
    noise = fh.run_checks(wrong, manifest, "NQ", lambda: fm.CRPSBoost(table, wrong, ["ES.noise"], "noise", small))
    assert noise["results"]["h15"]["diff_share"] > crps["results"]["h15"]["diff_share"] + 0.03


def test_an_out_of_sample_shape_is_fitted_and_scored(mis_sized):
    wrong, manifest, table, _ = mis_sized
    make = lambda: fm.ShapeOOS(lambda: fm.LinearScale(table, ["NQ.hint"], "lin"), "lin_shape", block=4, start=4)
    shaped = fh.run_checks(wrong, manifest, "NQ", make)
    plain = fh.run_checks(wrong, manifest, "NQ", lambda: fm.LinearScale(table, ["NQ.hint"], "lin"))
    first, last = shaped["checks"][0], shaped["checks"][-1]
    assert first["own_shape"] is None and first["fitted"]["residuals"]["15"] < fm.ShapeOOS.MIN_RESIDUALS  # too few:
    assert last["own_shape"] and last["fitted"]["residuals"]["15"] >= fm.ShapeOOS.MIN_RESIDUALS          # v2's shape
    q = last["fitted"]["shape"]["15"]
    assert np.allclose(q, -np.array(q)[::-1]) and len(q) == len(fh.TAU)                # symmetric, every level
    for d in shaped["per_session"]:                                                     # the same multipliers ...
        assert shaped["per_session"][d]["h15"][0] == plain["per_session"][d]["h15"][0]
    assert shaped["results"]["h15"]["other_crps_bps"] != plain["results"]["h15"]["other_crps_bps"]   # ... another shape
    cal = shaped["calibration"]["cand|h15|all"]
    assert set(cal["interval_score_bps"]) == {"0.50", "0.80", "0.90", "0.95"} and cal["interval_score_bps"]["0.90"] > 0


def test_a_frozen_definition_predicts_exactly_as_the_fitted_model(mis_sized):
    wrong, manifest, table, _ = mis_sized
    days = sorted(wrong)
    cand = fm.LinearScale(table, ["NQ.hint"], "lin_pois_ivx")
    cand.fit(fh.Rows.concat([fh.frame_rows(wrong[d], every=5) for d in days[:20]]))
    experiment = {"version": "fan_test", "definition_hash": "abc", "definition": {
        "gate": {"pass": "p", "inconclusive": "i", "fail": "f"}, "targets": {"primary": "NQ"},
        "horizons": {"primary": {"minutes": 15}}}}
    definition = fm.frozen_definition("lin_pois_ivx", cand, experiment, {"version": "fan_rw_v2"}, days[:20],
                                      {"feature_format": 3, "frame_format": 1})
    assert definition["features"][-1] == "NQ.hint" and definition["training_sessions"] == days[:20]
    frozen = fm.frozen_predictor(definition, table)
    for d in days[20:24]:
        rows = fh.frame_rows(wrong[d])
        assert np.array_equal(frozen.predict(rows), cand.predict(rows))            # from the stored state alone
    fm.check_columns(frozen, definition)
    res = fh.score_fixed(wrong, days[20:], frozen, {**manifest, "data": {"excluded": {"NQ": [days[21]]}}}, "NQ")
    assert res["verdict"] in ("pass", "inconclusive", "fail", "no interval") and res["sessions"]["excluded"] == [days[21]]
    assert len(res["sessions"]["scored"]) == len(days) - 21 and set(res["per_session"]) == set(res["sessions"]["scored"])
    with pytest.raises(ValueError):
        fm.frozen_definition("gbm_own_ivx", cand, experiment, {}, days, {"feature_format": 3, "frame_format": 1})
