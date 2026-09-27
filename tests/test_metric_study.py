# tests/test_metric_study.py
"""Feature-metric associations and walk-forward predictability: a planted relation is
found and robust, noise is not, and multiple testing is accounted for."""

import numpy as np
import pandas as pd
import pytest

from forecaster import metric_study as ms
from forecaster import models_v2
from tests.test_labels_v2 import O, S, _bars, _full

NUMERIC = models_v2.SKLEARN_FEATURES["numeric"]


def _data(n=240, beta=0.6, seed=3):
    rng = np.random.default_rng(seed)
    payloads = []
    for _ in range(n):
        p = {f: float(rng.normal()) for f in NUMERIC}
        p.update({f: bool(rng.random() < 0.2) for f in models_v2.SKLEARN_FEATURES["boolean"]})
        p.update(weekday="tue", remaining_event_risk=None)
        payloads.append(p)
    X = models_v2.design_matrix(payloads)
    X.index = [f"d{i:04d}" for i in range(n)]
    x = X["overnight_range_atr"].to_numpy()
    Y = pd.DataFrame({
        "range_15m": 0.3 + 0.05 * (beta * x + rng.normal(size=n)),   # driven by the overnight range
        "return_15m": rng.normal(scale=0.15, size=n),                 # pure noise
    }, index=X.index)
    return X, Y


def test_benjamini_hochberg():
    q = ms._bh(np.array([0.01, 0.04, 0.03, np.nan, 0.5]))
    # sorted 0.01, 0.03, 0.04, 0.5 -> p * m / rank 0.04, 0.06, 0.0533, 0.5 -> running min from the top
    assert q[0] == pytest.approx(0.04)
    assert q[2] == pytest.approx(0.04 * 4 / 3) and q[1] == pytest.approx(0.04 * 4 / 3)
    assert np.isnan(q[3]) and q[4] == pytest.approx(0.5)


def test_planted_relation_is_robust_and_noise_is_not():
    X, Y = _data()
    a = ms.associations(X, Y)
    hit = a[(a["metric"] == "range_15m") & (a["feature"] == "overnight_range_atr")].iloc[0]
    assert hit["rho"] > 0.3 and hit["robust"] and hit["rho_early"] > 0 and hit["rho_late"] > 0
    # everything else is independent noise: at most a stray false discovery
    assert a[a["robust"]].shape[0] <= 2
    assert not a[(a["metric"] == "return_15m")]["robust"].any()


def test_walk_forward_separates_signal_from_noise():
    X, Y = _data()
    sig = ms.walk_forward(X, Y["range_15m"], min_train=120, refit_every=10)
    noise = ms.walk_forward(X, Y["return_15m"], min_train=120, refit_every=10)
    assert sig["n_test"] == 120 and sig["r2"] > 0.1 and sig["gain"] > 2 * sig["gain_se"]
    assert noise["r2"] < 0.05 and not noise["gain"] > 2 * noise["gain_se"]
    assert ms.walk_forward(X.iloc[:50], Y["range_15m"].iloc[:50], min_train=120)["n_test"] == 0


class _Bars:
    def __init__(self, df):
        self.df = df

    def bars(self, contract_id, start, end):
        return self.df, "digest", "key"


def test_collect_measures_metrics_from_bars():
    df = _bars(S, _full(O + 0.1 * np.arange(1, 16)))   # +0.15 A in the first 15 minutes, then flat
    snap = {"session_date": S.session_date, "instrument_id": 1, "features": {"overnight_range_atr": 0.4},
            "reference_values": {"A": 10.0, "ONH": 110.0, "ONL": 90.0}}
    X, Y = ms.collect(_Bars(df), [snap])
    assert X.loc[str(S.session_date), "overnight_range_atr"] == 0.4
    assert Y.loc[str(S.session_date), "return_15m"] == pytest.approx(0.15)
    assert Y.loc[str(S.session_date), "|return_15m|"] == pytest.approx(0.15)
    assert list(Y.columns) == [name for name, _, _ in ms.METRICS]


def test_report_lists_every_metric():
    X, Y = _data(n=160)
    res = {"sessions": 160, "first": "d0000", "last": "d0159", "associations": ms.associations(X, Y),
           "walk_forward": {m: ms.walk_forward(X, Y[m], 120, 10) for m in Y.columns}, "min_train": 120}
    text = ms.format_report(res, top=3)
    assert "== range_15m (magnitude)" in text and "== return_15m (direction)" in text
    assert "overnight_range_atr" in text and "Robust associations:" in text
