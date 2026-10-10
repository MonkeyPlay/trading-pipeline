# tests/test_ml_bundle_models.py
"""
The bundles' model families, partial pooling, calibration and development evaluation
(forecaster/ml_bundle.py, forecaster/ml_bundle_eval.py), and the capability controls: shuffled labels
leave no skill; a planted NQ-only signal is learned by every arm; a planted cross-market signal by M only;
a planted transferable signal helps P. These verify capability on synthetic data, not market skill. Pure.
"""

import io
import json
import math
from datetime import date, timedelta

import joblib
import numpy as np
import pandas as pd
import pytest

from contracts import nq_ml_bundle as mb

COLUMNS = sorted({c for t in mb.TARGETS for c in mb.head_features(t, "M")})


def _days(n, start=date(2020, 1, 6)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _payload():
    return {"schedule": {"schedule": "full"}, "thresholds": {"T": 10, "B": 20, "A": "100"},
            "first_level_candidates": {"levels": {c: {"status": "valid", "value": str(100 + i)}
                                                  for i, c in enumerate(mb.CANDIDATES)}}}


def _world(n=220, instruments=("NQ",), seed=0, label=None, feats=None, keep=None):
    """BundleData whose features are standard normal (``feats`` may overwrite some per row) and whose label of each
    target is ``label(target, row_features, instrument, rng)`` (None: missing); ``keep(i, instrument)`` False drops a
    row's labels."""
    from forecaster import ml_bundle_data as bd
    from forecaster import ml_bundle_features as bf
    rng = np.random.default_rng(seed)
    days = _days(n)
    entries = []
    for i, d in enumerate(days):
        for s in instruments:
            f = {c: float(rng.normal()) for c in COLUMNS}
            if feats:
                f.update(feats(i, s, rng))
            labels, meas = {}, {"O": "100", "T": "10", "B": "20"}
            for t in mb.TARGETS:
                lab = None if keep and not keep(i, s) else label(t, f, s, rng) if label else \
                    mb.CLASSES[t][int(rng.integers(0, len(mb.CLASSES[t])))]
                labels[t] = {"label": lab, "reason": None if lab else "missing_bars"}
            z = {"bullish": 1.5, "bearish": -1.5, "neutral_band": 0.2}
            for t, spec in mb.DIRECTION.items():
                lab = labels[t]["label"]
                if lab:
                    mv = z[lab] * (1 + abs(rng.normal()))
                    meas[spec["close"]] = str(100 + mv * (10 if spec["threshold"] == "T" else 20))
            entries.append({"date": d, "instrument": s, "payload": _payload(), "features": f,
                            "candidates": bf.candidate_row(None), "labels": labels, "measurements": meas})
    return bd.assemble(entries, days, {})


def _prior_brier(train_labels, test_labels, classes):
    p = np.array([(list(train_labels).count(c) + 0.5) / (len(train_labels) + 0.5 * len(classes)) for c in classes])
    return float(np.mean([sum((p[j] - (c == y)) ** 2 for j, c in enumerate(classes)) for y in test_labels]))


def _test_brier(head, data, target, dates, instrument="NQ"):
    from forecaster import ml_bundle as mbun
    idx = np.flatnonzero(data.mask(target, instruments=(instrument,), dates=dates))
    X = data.rows.iloc[idx]
    y = [data.labels[target].iloc[i] for i in idx]
    return float(mbun.brier(head.predict(X), y, mb.CLASSES[target]).mean()), y


# --------------------------------------------------------------------------
# Families
# --------------------------------------------------------------------------

def _direction_label(t, f, s, rng):
    if t not in mb.DIRECTION:
        return mb.CLASSES[t][int(rng.integers(0, len(mb.CLASSES[t])))]
    z = 1.2 * f["ret_30"] + rng.normal()
    return "bullish" if z > 0.6 else "bearish" if z < -0.6 else "neutral_band"


@pytest.mark.parametrize("family,params", [("logit", {"C": 0.1}), ("decomp", {"C": 0.1}), ("decomp_sym", {"C": 0.1}),
                                           ("scale", {"alpha": 10.0}), ("scale_loc", {"alpha": 10.0}),
                                           ("gbm", {"max_iter": 60})])
def test_every_direction_family_gives_a_distribution_and_survives_the_artifact(family, params):
    from forecaster import ml_bundle as mbun
    data = _world(160, label=_direction_label)
    idx = np.flatnonzero(data.mask("direction_15m"))[:120]
    est = mbun._fit(family, params, "direction_15m", "N", data, idx)
    X = data.rows.iloc[120:160]
    P = est.predict(X)
    assert P.shape == (40, 3) and np.all(P >= 0) and np.allclose(P.sum(axis=1), 1)
    again = joblib.load(io.BytesIO(_dump(est)))
    assert np.allclose(again.predict(X), P)
    if family == "decomp_sym":
        c = mb.CLASSES["direction_15m"]
        assert np.allclose(P[:, c.index("bullish")], P[:, c.index("bearish")])


def _dump(obj) -> bytes:
    buf = io.BytesIO()
    joblib.dump(obj, buf)
    return buf.getvalue()


def test_the_decomposition_learns_direction_and_the_scale_model_learns_size():
    """A planted direction signal: decomp's r moves with it, decomp_sym's cannot. A planted size signal (the move's
    spread grows with a feature): the scale model's neutral probability falls as it grows."""
    from forecaster import ml_bundle as mbun
    data = _world(300, label=_direction_label)
    idx = np.flatnonzero(data.mask("direction_15m"))
    est = mbun._fit("decomp", {"C": 1.0}, "direction_15m", "N", data, idx)
    hi, lo = data.rows.iloc[[0]].copy(), data.rows.iloc[[0]].copy()
    hi["ret_30"], lo["ret_30"] = 2.0, -2.0
    c = mb.CLASSES["direction_15m"]
    assert est.predict(hi)[0, c.index("bullish")] > est.predict(lo)[0, c.index("bullish")] + 0.3

    data = _world(400, seed=3)
    idx = np.flatnonzero(data.mask("direction_15m"))
    z = [float(np.random.default_rng(i).normal() * math.exp(0.8 * data.rows.iloc[i]["rv_on"]))
         for i in range(len(data.rows))]                      # the move's spread grows with overnight volatility
    data.moves["direction_15m"] = pd.Series(z)
    data.labels["direction_15m"] = pd.Series(["bullish" if x > 1 else "bearish" if x < -1 else "neutral_band"
                                              for x in z], dtype=object)
    est = mbun._fit("scale", {"alpha": 10.0}, "direction_15m", "N", data, idx)
    big, small = data.rows.iloc[[0]].copy(), data.rows.iloc[[0]].copy()
    big["rv_on"], small["rv_on"] = 2.0, -2.0
    assert est.predict(big)[0, c.index("neutral_band")] < est.predict(small)[0, c.index("neutral_band")] - 0.2


def test_the_candidate_scorer_learns_shared_geometry_and_never_ranks_an_impossible_level():
    """First level planted as 'the nearest candidate on the side of the last 30 minutes': the scorer, sharing one
    geometry effect across candidates, puts most of its probability there; a ruled-out candidate gets 0."""
    from forecaster import ml_bundle as mbun
    from forecaster import ml_bundle_data as bd
    rng = np.random.default_rng(5)
    days = _days(260)
    entries = []
    for i, d in enumerate(days):
        dist = rng.normal(0, 3, len(mb.CANDIDATES))
        dist[dist == 0] = 0.1
        r30 = float(rng.normal())
        side = dist > 0 if r30 > 0 else dist < 0
        if not side.any():
            side = ~side
        cand = {}
        ranked = {True: sorted(abs(x) for x in dist if x > 0), False: sorted(abs(x) for x in dist if x < 0)}
        for k, c in enumerate(mb.CANDIDATES):
            above = dist[k] > 0
            cand.update({f"c_{c}_dist_atr": dist[k], f"c_{c}_abs_dist_atr": abs(dist[k]), f"c_{c}_above": float(above),
                         f"c_{c}_nearest_side": float(abs(dist[k]) == ranked[above][0]),
                         f"c_{c}_rank_side": float(ranked[above].index(abs(dist[k])) + 1), f"c_{c}_coincident": 0.0,
                         f"c_{c}_valid": 1.0, f"c_{c}_possible": float(k != 9)})       # long_ma ruled out
        allowed = [k for k in range(9) if side[k]] or list(range(9))
        target = mb.CANDIDATES[min(allowed, key=lambda k: abs(dist[k]))]
        f = {c: float(rng.normal()) for c in COLUMNS}
        f["ret_30_atr2m"] = r30
        labels = {t: {"label": target if t == "first_level_tested" else mb.CLASSES[t][0], "reason": None}
                  for t in mb.TARGETS}
        entries.append({"date": d, "instrument": "NQ", "payload": _payload(), "features": f, "candidates": cand,
                        "labels": labels, "measurements": {}})
    data = bd.assemble(entries, days, {})
    idx = np.arange(200)
    est = mbun._fit("cand", {"l2": 1.0}, "first_level_tested", "N", data, idx)
    X = data.rows.iloc[200:]
    P = est.predict(X)
    assert np.all(P[:, 9] == 0) and np.allclose(P.sum(axis=1), 1)
    y = [data.labels["first_level_tested"].iloc[i] for i in range(200, 260)]
    hit = np.mean([mb.CLASSES["first_level_tested"][int(np.argmax(p))] == lab for p, lab in zip(P, y)])
    assert hit > 0.6                                            # chance is about 0.1
    again = joblib.load(io.BytesIO(_dump(est)))
    assert np.allclose(again.predict(X), P)


def test_temperature_calibration_is_kept_only_when_it_helps_out_of_fold():
    from forecaster import ml_bundle as mbun
    rng = np.random.default_rng(2)
    classes = mb.CLASSES["direction_15m"]

    def blocks(sharpen):
        out = []
        for _ in range(3):
            true = rng.dirichlet(np.ones(3), size=60)
            y = [classes[rng.choice(3, p=p)] for p in true]
            raw = mbun.temper(true, 1 / sharpen)              # overconfident when sharpen > 1
            out.append({"raw": raw, "X": pd.DataFrame(index=range(60)), "y": y, "n": 200})
        return out
    over = mbun.calibrate("direction_15m", blocks(3.0))
    assert over["chosen"] == "temperature" and over["tau"] > 1.5
    fine = mbun.calibrate("direction_15m", blocks(1.0))
    assert fine["chosen"] == "none" or abs(fine["tau"] - 1) <= 0.3
    assert mbun.calibrate("direction_15m", blocks(3.0)[:1])["chosen"] == "none"   # one block: no cross-fit


# --------------------------------------------------------------------------
# Pooling
# --------------------------------------------------------------------------

def test_partial_pooling_keeps_an_nq_deviation_that_does_not_transfer():
    """ES and RTY follow ret_30 one way, NQ the other: selected on NQ's validation rows, P keeps a deviation (gamma >
    0) and beats complete pooling on NQ's test rows."""
    from forecaster import ml_bundle as mbun

    def label(t, f, s, rng):
        sign = -1.0 if s == "NQ" else 1.0
        z = 1.5 * sign * f["ret_30"] + 0.6 * rng.normal()
        return mb.CLASSES[t][0] if t not in mb.DIRECTION else \
            "bullish" if z > 0.5 else "bearish" if z < -0.5 else "neutral_band"
    data = _world(220, instruments=("NQ", "ES", "RTY"), label=label, seed=4)
    train, test = data.days[:180], data.days[181:]
    full = mbun.fit_head(data, "direction_15m", "P", train, configs=mbun.configurations("direction_15m", "P",
                                                                                       families=("logit",)))
    complete = mbun.fit_head(data, "direction_15m", "P", train, configs=mbun.configurations(
        "direction_15m", "P", families=("logit",), gammas=(0.0,)))
    assert full.params["gamma"] > 0 and complete.params["gamma"] == 0
    assert _test_brier(full, data, "direction_15m", test)[0] < _test_brier(complete, data, "direction_15m", test)[0] - 0.05


# --------------------------------------------------------------------------
# Controls: capability, not market skill
# --------------------------------------------------------------------------

def _skill(arm, data, target="direction_15m", n_train=170, families=("logit",)):
    from forecaster import ml_bundle as mbun
    train, test = data.days[:n_train], data.days[n_train + 1:]
    head = mbun.fit_head(data, target, arm, train, configs=mbun.configurations(target, arm, families=families))
    model, y = _test_brier(head, data, target, test)
    train_y = [data.labels[target].iloc[i] for i in np.flatnonzero(data.mask(target, instruments=("NQ",),
                                                                             dates=train))]
    return model - _prior_brier(train_y, y, mb.CLASSES[target])


def test_control_shuffled_labels_leave_no_skill():
    data = _world(240, instruments=("NQ", "ES", "RTY"), seed=8)            # labels independent of every feature
    for arm in ("N", "M", "P"):
        assert _skill(arm, data) > -0.02


def test_control_a_planted_nq_signal_is_learned_by_every_arm():
    def label(t, f, s, rng):
        z = 1.5 * f["ret_30"] + 0.6 * rng.normal()
        return mb.CLASSES[t][0] if t not in mb.DIRECTION else \
            "bullish" if z > 0.5 else "bearish" if z < -0.5 else "neutral_band"
    data = _world(240, instruments=("NQ", "ES", "RTY"), label=label, seed=9)
    for arm in ("N", "M", "P"):
        assert _skill(arm, data) < -0.08


def test_control_a_planted_cross_market_signal_is_learned_by_m_only():
    """NQ's label follows ES's last 30 minutes (es_ret_30, a context feature N and P never see)."""
    def label(t, f, s, rng):
        z = 1.5 * f["es_ret_30"] + 0.6 * rng.normal()
        return mb.CLASSES[t][0] if t not in mb.DIRECTION else \
            "bullish" if z > 0.5 else "bearish" if z < -0.5 else "neutral_band"
    data = _world(240, label=label, seed=10)
    assert "es_ret_30" in mb.head_features("direction_15m", "M") and \
        "es_ret_30" not in mb.head_features("direction_15m", "N")
    assert _skill("M", data) < -0.08
    assert _skill("N", data) > -0.02


def test_control_a_planted_transferable_signal_helps_p():
    """The same weak mapping in every market, NQ labelled on a third of its sessions only: P, trained on ES's and
    RTY's rows too, beats the NQ-only arm on NQ's test sessions."""
    def label(t, f, s, rng):
        z = 0.8 * f["ret_30"] + 0.8 * rng.normal()
        return mb.CLASSES[t][0] if t not in mb.DIRECTION else \
            "bullish" if z > 0.5 else "bearish" if z < -0.5 else "neutral_band"
    data = _world(330, instruments=("NQ", "ES", "RTY"), label=label, seed=11,
                  keep=lambda i, s: s != "NQ" or i % 3 == 0 or i >= 260)
    p, n = _skill("P", data, n_train=259), _skill("N", data, n_train=259)
    assert p < n - 0.01


# --------------------------------------------------------------------------
# The evaluation pipeline (predict frozen, then scored) on synthetic data
# --------------------------------------------------------------------------

def test_the_evaluation_never_passes_a_test_label_to_a_fit(monkeypatch):
    from forecaster import ml_bundle as mbun
    from forecaster import ml_bundle_eval as ev
    from forecaster import ml_split as sp
    data = _world(150, instruments=("NQ", "ES", "RTY"), label=_direction_label)
    fold = sp.DateFold(tuple(data.days[:120]), (data.days[120],), tuple(data.days[121:141]))
    seen = []
    real = mbun._fit

    def spy(fam, params, target, arm, d, idx, pooled=None):
        seen.append({d.dates[i] for i in idx})
        assert all(isinstance(d.labels[target].iloc[i], str) for i in idx)
        return real(fam, params, target, arm, d, idx, pooled)
    monkeypatch.setattr(mbun, "_fit", spy)
    preds, log = ev.fold_job(data, fold, variants=("N", "P", "P_complete", "P_nqonly", "N_sym"))
    assert seen and all(max(s) <= fold.train[-1] for s in seen)
    view = ev.restricted(data, fold.train)
    assert all(view.labels["direction_15m"].iloc[i] is None for i in np.flatnonzero(np.isin(data.dates, fold.test)))
    got = {(r["variant"], r["target"]) for r in preds}
    assert ("N_sym", "direction_15m") in got and ("N_sym", "first_move_5m") not in got
    assert {r["date"] for r in preds} == set(fold.test)
    nq = log["variants"]["P_nqonly"]["direction_15m"]
    assert nq["rows"] == {"NQ": 120}
    assert log["variants"]["P_complete"]["direction_15m"]["params"].get("gamma", 0.0) == 0   # boosted: complete too


def test_bootstrap_intervals_and_sample_sizes_are_by_session():
    from forecaster import ml_bundle_eval as ev
    rng = np.random.default_rng(1)
    d = rng.normal(-0.01, 0.1, 200)
    b = ev.boot(d)
    assert b["n"] == 200 and abs(b["mean"] - d.mean()) < 1e-12
    lo95, hi95 = b["intervals"]["0.9500"]
    lob, hib = b["intervals"][f"{ev.BONFERRONI:.4f}"]
    assert lob <= lo95 < hi95 <= hib                            # the Bonferroni interval is the wider one
    assert ev.boot(d[:8])["intervals"] == {}                     # fewer than two blocks: no interval
    n = ev.sample_size(0.1, 0.01)
    assert n == math.ceil(((ev.NormalDist().inv_cdf(1 - 0.05 / 42) + ev.NormalDist().inv_cdf(0.8)) * 10) ** 2)
    assert ev.sample_size(0.1, 0.02, margin=0.01) == n           # a margin claim: the same as half the effect
    assert ev.sample_size(0.1, 0.01, margin=0.01) is None
