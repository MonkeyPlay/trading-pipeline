# tests/test_ml_study.py
"""
ml_study_v1's machinery (research/ml_study.py, research/ml_study_data.py) on synthetic data - no database:
class order and full-vector scoring, chronological folds with the embargo and purge, the pooled split by
session date in the production fold code, training-only blending (outer test labels cannot move a
prediction), a planted signal is learnable and noise is not, the residual model's limit is the prior,
the RTH rows' window rules, and later bars never change a cutoff's features.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from contracts import nq_ml as ml
from features import calendar as cal
from research import ml_study as st
from research import ml_study_data as dd


def _problem(n_sessions=200, rows_per=1, signal=0.0, seed=0, classes=3):
    rng = np.random.default_rng(seed)
    n = n_sessions * rows_per
    x = rng.normal(size=(n, 4))
    logits = np.log(np.array([0.42, 0.45, 0.13]))[None, :] + signal * x[:, [0]] * np.array([-1.0, 1.0, 0.0])
    P = st.softmax(logits)
    y = (rng.random(n)[:, None] > np.cumsum(P, axis=1)).sum(axis=1)
    if classes == 2:
        y = np.where(y == 2, 0, y)
    X = pd.DataFrame(x, columns=["a", "b", "c", "d"])
    sessions = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(n_sessions)]
    return st.Problem(
        name="synthetic", sessions=sessions, row_session=np.repeat(np.arange(n_sessions), rows_per),
        keys=np.tile(np.arange(rows_per) * 30 + 30, n_sessions), phase=np.zeros(n, int), X=X, y=y,
        feature_sets={"nq": ["a", "b"], "multi": ["a", "b", "c", "d"]},
        cp_grid=[{"grain": "none", "context": None, "kappa": 5}, {"grain": "none", "context": "a", "kappa": 20}],
        cp_context={"a": x[:, 0]}), P


# --------------------------------------------------------------------------
# scores and class order
# --------------------------------------------------------------------------

def test_the_brier_score_uses_the_full_vector_in_class_order():
    assert st.CLASSES == ("bearish", "bullish", "neutral_band") == tuple(ml.CLASSES)
    P = np.array([[0.5, 0.3, 0.2]])
    y = np.array([1])                                           # bullish realised
    assert st.brier(P, y)[0] == pytest.approx(0.25 + 0.49 + 0.04)
    assert st.brier(P, y)[0] != pytest.approx((1 - 0.3) ** 2)     # the realised class alone is not the score
    assert st.logloss(P, y)[0] == pytest.approx(-np.log(0.3))
    assert st.brier(np.array([[0.0, 1.0, 0.0]]), y)[0] == 0 and st.brier(np.array([[1.0, 0.0, 0.0]]), y)[0] == 2


def test_probabilities_are_mapped_by_class_not_position():
    problem, _ = _problem(classes=2)                             # no neutral_band row in training
    train = np.arange(150)
    rows = np.arange(150, 200)
    P = st.fit_predict(problem, "LR", {"features": "nq", "C": 1.0}, train, rows)
    assert P.shape == (50, 3) and np.allclose(P.sum(axis=1), 1)
    assert np.all(P[:, 2] == 0)                                 # the class the training rows lacked gets 0
    m = st.make_sklearn("LR", {"C": 1.0}).fit(problem.X[["a", "b"]].to_numpy()[train], problem.y[train])
    assert list(m.classes_) == [0, 1]
    assert np.allclose(P[:, :2], m.predict_proba(problem.X[["a", "b"]].to_numpy()[rows]), atol=1e-12)


# --------------------------------------------------------------------------
# folds, embargo, purge
# --------------------------------------------------------------------------

def test_outer_folds_are_the_development_comparisons_and_inner_folds_stay_inside_training():
    from forecaster import ml_eval
    for n in (279, 318, 141):
        mine = st.outer_folds(n)
        theirs = ml_eval.folds(n)
        assert [(len(f.train), f.test[0], f.test[-1] + 1) for f in mine] == theirs
        for f in mine:
            assert max(f.train) < min(f.embargo) < min(f.test) and len(f.embargo) == 1
            inner = st.inner_folds(f.train)
            assert inner and all(set(g.train) | set(g.test) | set(g.embargo) <= set(f.train) for g in inner)
            assert all(max(g.train) < min(g.test) for g in inner)


def test_check_fold_refuses_overlap_and_label_windows_reaching_the_test_block():
    days = [f"2026-01-{i:02d}" for i in range(1, 31)]
    ok = st.Fold(tuple(range(10)), tuple(range(11, 15)), (10,))
    st.check_fold(ok, days, days)
    with pytest.raises(AssertionError):
        st.check_fold(st.Fold(tuple(range(12)), tuple(range(11, 15)), ()), days, days)
    ends = list(days)
    ends[9] = days[11]                                         # a training label ending inside the test block
    with pytest.raises(AssertionError):
        st.check_fold(ok, ends, days)


def test_the_pooled_fit_trains_on_training_dates_only(monkeypatch):
    """forecaster/ml_eval._fold - the production fold code: every instrument's training row of the pooled model
    is from a training date, none from the embargoed or test sessions (column ret_on carries the date index)."""
    from forecaster import ml_eval
    from forecaster import ml_model as mm
    days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(40)]
    rng = np.random.default_rng(1)
    labels = ["bearish", "bullish", "neutral_band"]
    y = {d: labels[i % 3] for i, d in enumerate(days)}
    X = {cfg: pd.DataFrame(rng.normal(size=(40, len(cols))), index=days, columns=cols)
         for cfg, cols in ml.CONFIGS.items()}
    idx, rows, py = [], [], []
    for s in ml.POOLED_INSTRUMENTS:
        for i, d in enumerate(days):
            idx.append((s, d))
            r = list(rng.normal(size=len(ml.POOLED_FEATURES)))
            r[ml.POOLED_FEATURES.index("ret_on")] = float(i)
            rows.append(r)
            py.append(labels[(i + len(s)) % 3])
    PX = pd.DataFrame(rows, columns=ml.POOLED_FEATURES, index=pd.MultiIndex.from_tuples(idx))
    py = pd.Series(py, index=PX.index, dtype=object)
    seen = []
    real = mm.tune

    def spy(Xa, ya, family):
        seen.append(np.asarray(Xa))
        return real(Xa, ya, family)
    monkeypatch.setattr(mm, "tune", spy)
    preds, entry = ml_eval._fold(X, PX, y, py, set(), days, 25, 26, 36)
    pooled = [a for a in seen if a.shape[1] == len(ml.POOLED_FEATURES)]
    assert len(pooled) == 2                                     # the pooled logit and boosted fits
    for a in pooled:
        used = set(a[:, ml.POOLED_FEATURES.index("ret_on")].astype(int))
        assert used == set(range(25))                           # training dates only: not 25 (embargo), not 26..35
        assert len(a) == 3 * 25                                 # every instrument's row of those dates
    assert set(preds["pooled/gbm"]) == set(days[26:36])        # scored on NQ's test rows


# --------------------------------------------------------------------------
# selection, blending, controls
# --------------------------------------------------------------------------

def test_outer_predictions_never_read_an_outer_test_label():
    problem, _ = _problem(n_sessions=180, rows_per=2, signal=0.6, seed=3)
    fold = st.outer_folds(180)[1]
    a = st.outer_fold(problem, fold)
    test_rows = np.flatnonzero(np.isin(problem.row_session, fold.test))
    flipped = st.Problem(**{**problem.__dict__, "y": problem.y.copy()})
    flipped.y[test_rows] = (flipped.y[test_rows] + 1) % 3      # every outer test label changed
    flipped.y[np.isin(problem.row_session, fold.embargo)] = -1  # and the embargoed session's labels gone
    b = st.outer_fold(flipped, fold)
    for fam in a["preds"]:
        assert np.array_equal(a["preds"][fam], b["preds"][fam]), fam
    assert a["best"] == b["best"]


def test_a_planted_signal_is_learned_and_noise_is_not():
    strong, P = _problem(n_sessions=260, signal=1.2, seed=5)
    folds = st.outer_folds(260)
    run = st.run_problem(strong, folds, families=("LR", "RES"))
    rows = sorted(run["preds"]["PRIOR"])
    gain = {}
    for fam in ("LR", "RES"):
        b_f = st.brier(np.array([run["preds"][fam][r] for r in rows]), strong.y[rows]).mean()
        b_p = st.brier(np.array([run["preds"]["PRIOR"][r] for r in rows]), strong.y[rows]).mean()
        gain[fam] = b_p - b_f
    assert gain["LR"] > 0.03 and gain["RES"] > 0.03
    noise, _ = _problem(n_sessions=260, signal=0.0, seed=6)
    run = st.run_problem(noise, folds, families=("LR", "RES"))
    for fam in ("LR", "RES"):
        b_f = st.brier(np.array([run["preds"][fam][r] for r in rows]), noise.y[rows]).mean()
        b_p = st.brier(np.array([run["preds"]["PRIOR"][r] for r in rows]), noise.y[rows]).mean()
        assert b_f - b_p > -0.01                                # no material skill from noise


def test_the_residual_model_returns_the_prior_when_fully_penalised_and_the_blend_is_clipped():
    problem, _ = _problem(n_sessions=100, rows_per=3, signal=1.0, seed=7)
    train, rows = np.arange(240), np.arange(240, 300)
    P = st.fit_predict(problem, "RES", {"features": "multi", "C": 1e-9}, train, rows)
    assert np.allclose(P, st.base_prior(problem, train, rows), atol=1e-6)
    prior = st.base_prior(problem, train, rows)
    assert st.blend_weight(prior, prior, problem.y[rows]) == 0.0
    oracle = st.onehot(problem.y[rows])
    assert st.blend_weight(oracle, prior, problem.y[rows]) == 1.0
    assert st.oracle_gain(problem, np.repeat(st.laplace(problem.y)[None, :], len(problem.y), axis=0)) \
        == pytest.approx(0.0)


def test_the_same_clock_prior_and_conditional_cells_use_training_rows_only():
    problem, _ = _problem(n_sessions=50, rows_per=4, seed=8)
    train, rows = np.arange(120), np.arange(120, 200)
    P = st.base_prior(problem, train, rows)
    for r, p in zip(rows, P):
        same = train[problem.keys[train] == problem.keys[r]]
        assert np.allclose(p, st.laplace(problem.y[same]))
    big = st.fit_predict(problem, "CP", {"grain": "clock", "context": None, "kappa": 1e12}, train, rows)
    assert np.allclose(big, st.laplace(problem.y[train]))      # kappa -> infinity: the global frequencies
    flipped = problem.y.copy()
    flipped[rows] = 0
    again = st.fit_predict(st.Problem(**{**problem.__dict__, "y": flipped}), "CP",
                           {"grain": "clock", "context": "a", "kappa": 10}, train, rows)
    assert np.allclose(again, st.fit_predict(problem, "CP", {"grain": "clock", "context": "a", "kappa": 10},
                                             train, rows))


def test_shuffling_moves_whole_sessions():
    problem, _ = _problem(n_sessions=30, rows_per=3, seed=9)
    sh = st.shuffled(problem, 4)
    perm = np.random.default_rng(4).permutation(30)
    for s in range(30):
        assert list(sh.y[problem.row_session == s]) == list(problem.y[problem.row_session == perm[s]])


def test_power_of_the_registered_rule_never_reaches_eighty_percent_at_its_own_threshold():
    assert st.sessions_needed(0.1, 0.01, 0.9833, point=0.01) is None
    assert st.power(10000, 0.1, 0.01, 0.9833, point=0.01) <= 0.5 + 1e-9
    n = st.sessions_needed(0.1, 0.03, 0.9833, point=0.01)
    assert n and st.power(n, 0.1, 0.03, 0.9833, point=0.01) >= 0.8 > st.power(n - 1, 0.1, 0.03, 0.9833, point=0.01)
    assert st.sessions_needed(0.1, 0.03, 0.9833, margin=0.01) > n           # the material rule needs more
    assert st.detectable(60, 0.1, 0.9833, point=0.01) > 0.03


# --------------------------------------------------------------------------
# the RTH rows
# --------------------------------------------------------------------------

def _session(day="2026-10-06", seed=0):
    s = cal.session(day)
    L = int((s.scheduled_close_at - s.rth_open_at).total_seconds() // 60)
    rng = np.random.default_rng(seed)
    n = dd.PRE + L
    c = 20000 + np.cumsum(rng.normal(0, 2, size=n))
    o = np.r_[c[0], c[:-1]]
    h, low = np.maximum(o, c) + 1, np.minimum(o, c) - 1
    v = rng.integers(50, 500, size=n).astype(float)
    bars = {}
    for sym, scale in (("NQ", 1.0), ("ES", 0.3), ("RTY", 0.1), ("VXN", 0.001)):
        bars[sym] = dd.Minutes(1, o * scale, h * scale, low * scale, c * scale, v.copy())
    sd = dd.SessionData(day, s, L, bars, prev_close=float(c[dd.PRE - 1000]), prev_high=float(c.max()),
                        prev_low=float(c.min()))
    sd.buckets = dd.buckets_from_minutes(bars["NQ"], s.rth_open_at)
    sd.asof = {"sigma_NQ": 2.0, "sigma_ES": 1e-4, "sigma_RTY": 1e-4, "sigma_VXN": 1e-4, "atr_d": 300.0,
               "volbase": np.full(390, 1000.0), "beta": 1.0, "sd_resid": 1e-4}
    return sd


def test_later_bars_never_change_a_cutoffs_features():
    sd = _session()
    events = np.array([int((sd.session.rth_open_at + timedelta(minutes=95)).timestamp())])
    for c in st.RTH_CUTOFFS:
        before = dd.features_at(sd, c, events)
        bars = {}
        for sym, mn in sd.bars.items():
            k = dd.PRE + c
            arrs = [x.copy() for x in (mn.o, mn.h, mn.l, mn.c, mn.v)]
            for x in arrs[:4]:
                x[k:] *= 1.05
            arrs[4][k:] *= 7
            bars[sym] = dd.Minutes(mn.cid, *arrs)
        from dataclasses import replace
        after = dd.features_at(replace(sd, bars=bars, buckets=dd.buckets_from_minutes(bars["NQ"],
                                                                                     sd.session.rth_open_at)),
                               c, events)
        assert before.keys() == after.keys()
        for k in before:
            assert before[k] == after[k] or (np.isnan(before[k]) and np.isnan(after[k])), (c, k)
    assert dd.features_at(sd, 90, events)["ev_recent"] == 0.0 and dd.features_at(sd, 120, events)["ev_recent"] == 1.0


def test_two_minute_atr_matches_the_evidence_convention():
    from decimal import Decimal
    from features import nq_evidence as ev
    sd = _session(seed=3)
    for c in (30, 200):
        t_c = sd.session.rth_open_at + timedelta(minutes=c)
        nq = sd.bars["NQ"]
        bars = []
        for i in range(dd.PRE + c):
            start = sd.session.rth_open_at + timedelta(minutes=i - dd.PRE)
            bars.append((start, Decimal(str(float(nq.o[i]))), Decimal(str(float(nq.h[i]))), Decimal(str(float(nq.l[i]))),
                         Decimal(str(float(nq.c[i]))), int(nq.v[i])))
        ref = ev._two_minute_atr(ev.aggregate(bars, 2, t_c), t_c)
        assert ref["status"] == "valid"
        assert dd.atr2m(sd, t_c) == pytest.approx(float(ref["exact"]), rel=1e-9)


def test_rth_windows_never_cross_the_close_and_missing_bars_leave_no_label():
    sd = _session()
    assert dd.label_at(sd, 360, 30, "cutoff", 5.0)["reason"] == "ok"
    assert dd.label_at(sd, 360, 60, "cutoff", 5.0)["reason"] == "crosses_close"
    assert dd.label_at(sd, 360, 30, "delayed", 5.0)["reason"] == "crosses_close"   # 360 + 14 + 30 > 390
    early = _session("2025-11-28")                              # a 13:00 close: 210 minutes
    assert early.L == 210 and dd.label_at(early, 180, 30, "cutoff", 5.0)["reason"] == "ok"
    assert dd.label_at(early, 180, 15, "delayed", 5.0)["reason"] == "ok"                 # 180 + 14 + 15 = 209
    assert dd.label_at(early, 180, 30, "delayed", 5.0)["reason"] == "crosses_close"
    lab = dd.label_at(sd, 60, 15, "delayed", 5.0)
    assert lab["S"] == 74
    nq = sd.bars["NQ"]
    assert lab["move"] == pytest.approx(nq.at(74 + 15 - 1) - nq.at(74 - 1))
    assert lab["band"] == pytest.approx(0.5 * 5.0)
    assert lab["label"] == (1 if lab["move"] > 2.5 else 0 if lab["move"] < -2.5 else 2)
    nq.c[dd.PRE + 80] = np.nan
    assert dd.label_at(sd, 60, 15, "delayed", 5.0)["reason"] == "window_bar_missing"
    assert dd.label_at(sd, 60, 15, "cutoff", 5.0)["reason"] == "ok"                 # its window ends at 75
    nq.c[dd.PRE + 59] = np.nan
    assert dd.label_at(sd, 60, 15, "cutoff", 5.0)["reason"] == "no_cutoff_bar"
    assert dd.label_at(sd, 60, 15, "cutoff", float("nan"))["reason"] == "no_cutoff_bar"
