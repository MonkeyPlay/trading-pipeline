# tests/test_ml_split.py
"""
The session-date split (forecaster/ml_split.py, contracts/nq_ml.SPLIT) that replaced the row-position
tuning cut, the legacy cut it keeps for reproducing the registered v1 artifacts, and the corrected study
(research/ml_pooled_split.py). Pure: no database.
"""

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from contracts import nq_ml as ml
from forecaster import ml_split as sp

LABELS = ("bearish", "bullish", "neutral_band")


def _days(n, start=date(2025, 9, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _pooled_rows(coverage, shuffle_seed=None):
    """Rows of several instruments with unequal coverage: ``{instrument: dates}``; instrument-major (as
    ml_train.dataset builds them) unless shuffled."""
    rows = [(s, d) for s, ds in coverage.items() for d in ds]
    if shuffle_seed is not None:
        rng = np.random.default_rng(shuffle_seed)
        rows = [rows[i] for i in rng.permutation(len(rows))]
    return [d for _, d in rows], [s for s, _ in rows]


# --------------------------------------------------------------------------
# The defect, demonstrated
# --------------------------------------------------------------------------

def test_the_legacy_row_cut_validates_on_rty_only_on_dates_also_in_training():
    """The balanced example of the review: 100 dates per instrument, rows instrument first - the last 25 % of the
    rows are RTY's 75 latest-but-not-all dates, every one of them also in training (NQ's and ES's rows)."""
    days = _days(100)
    dates, inst = _pooled_rows({"NQ": days, "ES": days, "RTY": days})
    diag = sp.legacy_diagnostics(dates, inst, ml.TUNING["validation_share"])
    assert diag["validation_rows"] == 75 and diag["validation_instruments"] == {"RTY": 75}
    assert diag["validation_dates"] == 75 and diag["validation_dates_also_in_training"] == 75
    assert diag["training_after_validation_start"]          # NQ's and ES's later dates were in training


def test_the_legacy_tuner_is_the_v1_algorithm_unchanged():
    """tune_rows_legacy reproduces the v1 tuner exactly (the registered artifacts were trained with it)."""
    from forecaster import ml_model as mm
    rng = np.random.default_rng(5)
    X = rng.normal(size=(160, 4))
    y = np.array([LABELS[i] for i in rng.integers(0, 3, 160)])

    def v1(X, y, family):                                    # the code of forecaster/ml_model.tune at 539685e
        y = np.asarray(y)
        cut = int(round(len(y) * (1 - ml.TUNING["validation_share"])))
        scores = []
        for params in mm.grid(family):
            m = mm.pipeline(family, params).fit(X[:cut], y[:cut])
            scores.append({"params": params, "brier": float(mm.brier(mm.probabilities(m, X[cut:]), y[cut:]).mean())})
        best = min(scores, key=lambda s: (s["brier"], json.dumps(s["params"], sort_keys=True)))
        return mm.pipeline(family, best["params"]).fit(X, y), best["params"], scores
    for family in ("logit", "gbm"):
        a, b = mm.tune_rows_legacy(X, y, family), v1(X, y, family)
        assert a[1] == b[1] and a[2] == b[2]
        assert np.array_equal(mm.probabilities(a[0], X), mm.probabilities(b[0], X))
        same = mm.tune(X, y, family, split="legacy_rows")
        assert same[1] == b[1] and np.array_equal(mm.probabilities(same[0], X), mm.probabilities(b[0], X))


def test_the_registered_v1_definitions_are_unchanged():
    """nq_ml_features_v1 and nq_forecast_schema_v3 are pinned by p1_ml_forward_v2: their registered hashes (computed
    from the code at 539685e, before this change) must not move."""
    from contracts import nq_prompt_v2 as defs
    assert ml.features_record()["definition_hash"] == \
        "32915f6c6671a61a3b17095ac8644e7c93b8ec8fa7c5f3f72328fe96dbe1e22d"
    assert ml.schema_record()["definition_hash"] == "7066978249e5ca4922f93b0f85d0c51995a9de949a8d1effc02ae7ea92e7b116"
    assert defs._hash(ml.promotion_rule()) == "2ef83021d5869b2e6074252c92df2d1311d86fab5d5bacbb5c158e8934941210"
    assert defs.label_record()["definition_hash"] == \
        "b0b69fe196c4661fcf58aadc5cc49516b335bc2d4e62f614b8593945fdd2ad98"


# --------------------------------------------------------------------------
# The date split
# --------------------------------------------------------------------------

def test_the_inner_split_is_by_date_with_unequal_coverage_and_shuffled_rows():
    """Unequal instrument coverage (RTY starts late, ES misses dates) in a shuffled row order: the split is the same
    as for the sorted rows, its dates are disjoint and chronological, the embargo date is in neither side, every
    instrument's rows of a date are on one side, and the objective is NQ's validation rows."""
    days = _days(100)
    cov = {"NQ": days, "ES": [d for i, d in enumerate(days) if i % 7], "RTY": days[40:]}
    for seed in (None, 1, 2):
        dates, inst = _pooled_rows(cov, seed)
        fold = sp.inner_split(dates, ml.TUNING["validation_share"], 1)
        sp.check(fold)
        assert fold == sp.inner_split(sorted(dates), ml.TUNING["validation_share"], 1)
        assert len(fold.test) == 25 and len(fold.embargo) == 1 and len(fold.train) == 74
        assert max(fold.train) < fold.embargo[0] < min(fold.test)
        train, val = sp.mask(dates, fold.train), sp.objective(dates, inst, fold, "NQ")
        assert not (train & sp.mask(dates, fold.embargo)).any() and not (train & sp.mask(dates, fold.test)).any()
        assert {inst[i] for i in np.flatnonzero(val)} == {"NQ"} and val.sum() == 25
        for d in set(dates):                                   # every row of a date on one side
            sides = {("train" if d in fold.train else "embargo" if d in fold.embargo else "test")}
            assert len(sides) == 1
        # nothing chosen from a row's position: a different row order gives the same masks on the same rows
        order = np.argsort([f"{d}{s}" for d, s in zip(dates, inst)])
        ref_d, ref_s = [dates[i] for i in order], [inst[i] for i in order]
        assert np.array_equal(train[order], sp.mask(ref_d, fold.train))
        assert np.array_equal(val[order], sp.objective(ref_d, ref_s, fold, "NQ"))


def test_check_refuses_overlap_disorder_and_a_missing_embargo_gap():
    a = _days(10)
    sp.check(sp.DateFold(tuple(a[:6]), (a[6],), tuple(a[7:])))
    with pytest.raises(AssertionError):
        sp.check(sp.DateFold(tuple(a[:7]), (a[6],), tuple(a[7:])))           # a training date in the embargo
    with pytest.raises(AssertionError):
        sp.check(sp.DateFold(tuple(a[:8]), (), tuple(a[7:])))                # training reaches the test block
    with pytest.raises(AssertionError):
        sp.check(sp.DateFold(tuple(a[:6]), (a[9],), tuple(a[7:9])))          # the embargo after the test block


def test_outer_and_inner_folds_are_chronological_and_match_the_development_design():
    from forecaster import ml_eval
    days = _days(279)
    folds = ml_eval.date_folds(days)
    assert len(folds) == 8 and [len(f.test) for f in folds][-1] == 19
    for f in folds:
        sp.check(f)
        assert len(f.embargo) == ml.DEV_EVALUATION["embargo_sessions"]
    assert ml_eval.folds(279) == [(len(f.train), len(f.train) + 1, len(f.train) + 1 + len(f.test)) for f in folds]
    inner = sp.inner_folds(days[:200], blocks=3, block=20, embargo=1, min_train=60)
    assert [f.test[0] for f in inner] == [days[140], days[160], days[180]]
    for f in inner:
        sp.check(f)
        assert max(f.test) < days[200]


def test_the_tuner_scores_nq_validation_rows_and_trains_on_earlier_dates(monkeypatch):
    """forecaster/ml_model.tune (date split): every grid point is fitted on every instrument's rows of the inner
    training dates and scored on NQ's rows of the validation dates only; the best is refitted on every row."""
    from forecaster import ml_model as mm
    days = _days(80)
    cov = {"NQ": days, "ES": days[5:], "RTY": days[30:]}
    dates, inst = _pooled_rows(cov, 3)
    code = {"NQ": 0, "ES": 1, "RTY": 2}
    pos = {d: i for i, d in enumerate(days)}
    rng = np.random.default_rng(0)
    X = np.column_stack([[pos[d] for d in dates], [code[s] for s in inst], rng.normal(size=len(dates))]).astype(float)
    y = [LABELS[i % 3] for i in range(len(dates))]
    fits, scored = [], []
    real_fit = mm.pipeline

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        def fit(self, Xa, ya):
            fits.append(np.asarray(Xa))
            self.inner.fit(Xa, ya)
            return self

        def predict_proba(self, Xa):
            scored.append(np.asarray(Xa))
            return self.inner.predict_proba(Xa)

        @property
        def classes_(self):
            return self.inner.classes_
    monkeypatch.setattr(mm, "pipeline", lambda family, params: Spy(real_fit(family, params)))
    model, params, scores = mm.tune(X, y, "logit", dates=dates, instruments=inst)
    fold = sp.inner_split(dates, ml.TUNING["validation_share"], ml.SPLIT["embargo_sessions"])
    first_val = pos[fold.test[0]]
    assert len(scores) == len(mm.grid("logit"))
    for a in fits[:-1]:                                    # the grid points: inner training dates, all instruments
        assert a[:, 0].max() < first_val - 1 and set(a[:, 1]) == {0.0, 1.0, 2.0}
    for a in scored:                                       # validation: NQ's rows of the validation dates
        assert set(a[:, 1]) == {0.0} and a[:, 0].min() == first_val and len(a) == len(fold.test)
    assert len(fits[-1]) == len(dates)                     # the refit on the whole window


def test_the_tuner_refuses_the_date_split_without_dates():
    from forecaster import ml_model as mm
    X, y = np.zeros((10, 2)), [LABELS[i % 3] for i in range(10)]
    with pytest.raises(ValueError):
        mm.tune(X, y, "logit")
    with pytest.raises(ValueError):
        mm.tune(X, y, "logit", dates=_days(10), split="rows")


def test_training_rows_carry_dates_and_instruments_and_v1_training_keeps_the_legacy_split(monkeypatch):
    from forecaster import ml_model as mm
    from forecaster import ml_train
    data = _dataset(40)
    rows = ml_train.training_rows(data, ml.ML_POOLED_VERSION, data.days[:30])
    assert len(rows.dates) == len(rows.y) == len(rows.instruments) == rows.X.shape[0]
    assert rows.per == {"NQ": 30, "ES": 30, "RTY": 30} and rows.instruments[0] == "NQ" and rows.instruments[-1] == "RTY"
    single = ml_train.training_rows(data, ml.ML_NQ_VERSION, data.days[:30])
    assert single.dates == data.days[:30] and set(single.instruments) == {"NQ"}
    seen = []
    monkeypatch.setattr(mm, "tune", lambda X, y, family, **kw: seen.append(kw["split"]) or (None, {}, []))
    monkeypatch.setattr(mm, "save", lambda *a, **k: {})
    ml_train.train(None, data=data, until=data.days[30])
    assert seen == ["legacy_rows"] * 3                      # the registered v1 artifacts' reproduction


def _dataset(n, start=date(2020, 1, 6)):
    """A minimal ml_train.Data: NQ rows per config and pooled rows of three instruments (dates long before any
    stored study's)."""
    from forecaster.ml_train import Data
    days = _days(n, start)
    rng = np.random.default_rng(2)
    y = {d: LABELS[i % 3] for i, d in enumerate(days)}
    X = {cfg: pd.DataFrame(rng.normal(size=(n, len(cols))), index=days, columns=cols) for cfg, cols in ml.CONFIGS.items()}
    idx, rows, py = [], [], []
    for s in ml.POOLED_INSTRUMENTS:
        for i, d in enumerate(days):
            idx.append((s, d))
            r = list(rng.normal(size=len(ml.POOLED_FEATURES)))
            r[ml.POOLED_FEATURES.index("ret_on")] = float(i)
            rows.append(r)
            py.append(LABELS[(i + len(s)) % 3])
    PX = pd.DataFrame(rows, columns=ml.POOLED_FEATURES, index=pd.MultiIndex.from_tuples(idx))
    return Data([], days, y, None, {}, X, PX, pd.Series(py, index=PX.index, dtype=object))


# --------------------------------------------------------------------------
# The corrected study and the v1 study's reproduction
# --------------------------------------------------------------------------

def test_the_corrected_study_diagnoses_the_legacy_cut_on_every_fold():
    from research import ml_pooled_split as study
    data = _dataset(150)
    diag = study.inner_diagnostics(data)
    assert len(diag) == 2
    for f in diag:
        assert set(f["legacy"]["validation_instruments"]) == {"RTY"}
        assert f["legacy"]["validation_dates_also_in_training"] == f["legacy"]["validation_dates"]
        assert f["corrected"]["objective_rows_nq"] == f["corrected"]["test"][2] > 0


def test_the_corrected_study_refits_both_splits_through_the_production_fold_code():
    from research import ml_pooled_split as study
    data = _dataset(140)
    before, log_b = study.outer_refits(data, [], "legacy_rows")
    after, log_a = study.outer_refits(data, [], "dates")
    assert set(before) == set(after) and set(before["pooled/gbm"]) == set(data.days[120:])
    assert {e["split"] for e in log_b} == {"legacy_rows"} and {e["split"] for e in log_a} == {"dates"}
    assert all("inner" in e for e in log_a)
    rep = study.reproduction(before)                        # the v1 files are of other sessions: nothing compared
    assert all(r["sessions"] == 0 and not r["reproduced"] for r in rep.values())


def test_the_v1_study_reproduces_its_runs_with_the_legacy_split(monkeypatch):
    """research/ml_study_run's frozen refits (the controls' path) call the production fold code with the v1 split,
    so ml_study_v1 stays reproducible; the corrected study passes 'dates'."""
    from types import SimpleNamespace
    from forecaster import ml_eval
    from research import ml_study_run as run
    data = _dataset(140)
    calls = []
    monkeypatch.setattr(ml_eval, "_fold", lambda *a, **kw: calls.append(kw.get("split")) or ({}, {}))
    run._frozen_on(SimpleNamespace(data=data, abstain=[]), data.y, {k: data.py[k] for k in data.PX.index})
    assert calls and set(calls) == {"legacy_rows"}


def test_the_v1_study_files_are_hashed_and_unchanged():
    from research import ml_pooled_split as study
    h = study.v1_hashes()
    assert "predictions_preopen.csv" in h and "manifest.json" in h
    with open(f"{study.V1_DIR}/manifest.json") as f:
        stages = json.load(f)["stages"]
    assert stages["predict_preopen"]["files"]["predictions_preopen.csv"] == h["predictions_preopen.csv"]
