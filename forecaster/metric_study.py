# forecaster/metric_study.py
"""
Which pre-open features carry information about the realised outcome metrics -
direction (signed returns) versus magnitude (ranges, absolute returns,
efficiency). Read-only: nothing is stored and no model or label changes.

For every stored session (one snapshot each) the section 10 metrics are measured
from its bars under the current label parameters, then:

  associations   Spearman rank correlation of each numeric / boolean model input
                 (models_v2.SKLEARN_FEATURES) with each metric, its p-value, the
                 Benjamini-Hochberg q-value over all feature x metric tests, and
                 the correlation in the earlier and the later half of the
                 sessions. An association counts as *robust* when q < 0.05 and
                 both halves agree in sign - a relation that holds in only one
                 part of the year is what made the classifiers lose out of sample.
  walk-forward   per metric, a ridge regression on all model inputs (the model's
                 own preprocessing), refitted every ``refit_every`` sessions on
                 earlier sessions only, against the running mean of the earlier
                 sessions: out-of-sample R^2 and the mean per-session squared-error
                 gain with its standard error.

A metric that the features predict out of sample is a candidate for a new,
magnitude-type target; one that they only correlate with in-sample is not.
"""

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from features import calendar as cal
from features.indicators import finite
from forecaster import labels_v2, models_v2

# (name shown, how it is read from the metrics, kind)
METRICS: List[Tuple[str, Any, str]] = [
    ("return_15m", lambda m: m.get("return_15m_atr"), "direction"),
    ("first_hour_return", lambda m: m.get("first_hour_return_atr"), "direction"),
    ("return_rth", lambda m: m.get("return_rth_atr"), "direction"),
    ("|return_15m|", lambda m: _abs(m.get("return_15m_atr")), "magnitude"),
    ("range_15m", lambda m: m.get("range_15m_atr"), "magnitude"),
    ("efficiency_15m", lambda m: m.get("efficiency_15m"), "magnitude"),
    ("|first_hour_return|", lambda m: _abs(m.get("first_hour_return_atr")), "magnitude"),
    ("|return_rth|", lambda m: _abs(m.get("return_rth_atr")), "magnitude"),
    ("range_rth", lambda m: m.get("range_rth_atr"), "magnitude"),
    ("efficiency_rth_5m", lambda m: m.get("efficiency_rth_5m"), "magnitude"),
]
RIDGE_ALPHAS = tuple(np.logspace(-1, 4, 11))


def _abs(v):
    return None if v is None else abs(v)


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def collect(md, snapshots: Iterable[Dict[str, Any]]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    (X, Y), one row per snapshot in session order: X the model inputs
    (models_v2.design_matrix), Y the outcome metrics of METRICS (NaN when not
    measurable). Both are indexed by session date.
    """
    payloads, rows, dates = [], [], []
    for snap in snapshots:
        s = cal.session(snap["session_date"])
        ref = snap["reference_values"]
        A, ONH, ONL = finite(ref.get("A")), finite(ref.get("ONH")), finite(ref.get("ONL"))
        df, _, _ = md.bars(int(snap["instrument_id"]), s.rth_open_at, s.scheduled_close_at)
        m = labels_v2.compute_metrics(df, s, A, ONH, ONL)["metrics"]
        payloads.append(snap["features"])
        rows.append({name: (np.nan if (v := finite(fn(m))) is None else v) for name, fn, _ in METRICS})
        dates.append(str(snap["session_date"]))
    X = models_v2.design_matrix(payloads)
    X.index = dates
    Y = pd.DataFrame(rows, index=dates, columns=[name for name, _, _ in METRICS])
    return X, Y


# --------------------------------------------------------------------------
# Associations
# --------------------------------------------------------------------------

def _bh(p: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg q-values (NaN p-values stay NaN and are not counted)."""
    q = np.full(len(p), np.nan)
    ok = np.flatnonzero(~np.isnan(p))
    if not len(ok):
        return q
    order = ok[np.argsort(p[ok])]
    m = len(order)
    ranked = p[order] * m / np.arange(1, m + 1)
    q[order] = np.minimum(np.minimum.accumulate(ranked[::-1])[::-1], 1.0)
    return q


def associations(X: pd.DataFrame, Y: pd.DataFrame, min_n: int = 30) -> pd.DataFrame:
    from scipy.stats import spearmanr

    feats = models_v2.SKLEARN_FEATURES["numeric"] + models_v2.SKLEARN_FEATURES["boolean"]
    out = []
    for metric in Y.columns:
        for f in feats:
            both = pd.concat([X[f], Y[metric]], axis=1).dropna()
            n = len(both)
            rec = {"metric": metric, "feature": f, "n": n, "rho": np.nan, "p": np.nan,
                   "rho_early": np.nan, "rho_late": np.nan}
            if n >= min_n and both.iloc[:, 0].nunique() > 1 and both.iloc[:, 1].nunique() > 1:
                rho, p = spearmanr(both.iloc[:, 0], both.iloc[:, 1])
                half = n // 2
                early, late = both.iloc[:half], both.iloc[half:]
                rec.update(rho=float(rho), p=float(p),
                           rho_early=_rho(early), rho_late=_rho(late))
            out.append(rec)
    res = pd.DataFrame(out)
    res["q"] = _bh(res["p"].to_numpy(dtype=float))
    res["robust"] = (res["q"] < 0.05) & (np.sign(res["rho_early"]) == np.sign(res["rho_late"])) \
        & (np.sign(res["rho_early"]) == np.sign(res["rho"]))
    return res


def _rho(df: pd.DataFrame) -> float:
    from scipy.stats import spearmanr

    if df.iloc[:, 0].nunique() < 2 or df.iloc[:, 1].nunique() < 2:
        return np.nan
    return float(spearmanr(df.iloc[:, 0], df.iloc[:, 1])[0])


# --------------------------------------------------------------------------
# Walk-forward prediction
# --------------------------------------------------------------------------

def walk_forward(X: pd.DataFrame, y: pd.Series, min_train: int = 120, refit_every: int = 5) -> Dict[str, Any]:
    """
    Ridge on the model inputs vs the running mean, each session predicted from
    earlier sessions only. Returns n_test, r2 (1 - SSE_model / SSE_mean) and
    gain / gain_se (mean per-session squared-error reduction; > 0 is better).
    """
    import warnings

    from sklearn.linear_model import RidgeCV
    from sklearn.pipeline import Pipeline

    keep = y.notna().to_numpy()
    X, y = X[keep].reset_index(drop=True), y[keep].reset_index(drop=True)
    if len(y) <= min_train:
        return {"n_test": 0, "r2": math.nan, "gain": math.nan, "gain_se": math.nan}
    se_model, se_mean, pipe = [], [], None
    for i in range(min_train, len(y)):
        if pipe is None or (i - min_train) % refit_every == 0:
            pipe = Pipeline([("features", models_v2._preprocessor()), ("model", RidgeCV(alphas=RIDGE_ALPHAS))])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                pipe.fit(X.iloc[:i], y.iloc[:i])
            mean = float(y.iloc[:i].mean())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pred = float(pipe.predict(X.iloc[[i]])[0])
        se_model.append((y.iloc[i] - pred) ** 2)
        se_mean.append((y.iloc[i] - mean) ** 2)
    se_model, se_mean = np.asarray(se_model), np.asarray(se_mean)
    gains = se_mean - se_model
    n = len(gains)
    return {"n_test": n, "r2": 1 - se_model.sum() / se_mean.sum() if se_mean.sum() > 0 else math.nan,
            "gain": float(gains.mean()), "gain_se": float(gains.std(ddof=1) / math.sqrt(n)) if n > 1 else math.nan}


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def study(md, snapshots, min_train: int = 120, refit_every: int = 5) -> Dict[str, Any]:
    snapshots = list(snapshots)
    X, Y = collect(md, snapshots)
    assoc = associations(X, Y)
    wf = {metric: walk_forward(X, Y[metric], min_train, refit_every) for metric in Y.columns}
    return {"sessions": len(snapshots), "first": X.index[0] if len(X) else None,
            "last": X.index[-1] if len(X) else None, "associations": assoc, "walk_forward": wf,
            "min_train": min_train}


def format_report(result: Dict[str, Any], top: int = 5) -> str:
    assoc, wf = result["associations"], result["walk_forward"]
    tested = int(assoc["p"].notna().sum())
    lines = [f"{result['sessions']} session(s), {result['first']} .. {result['last']}; "
             f"{tested} feature x metric tests (robust: BH q < 0.05 and the same sign in both halves).",
             f"Walk-forward: ridge on all model inputs vs the running mean, from session "
             f"{result['min_train'] + 1} on; R2 > 0 and gain > 2 SE = predictable out of sample.", ""]
    kinds = {name: kind for name, _, kind in METRICS}
    for metric in assoc["metric"].unique():
        w = wf[metric]
        verdict = ("PREDICTABLE" if w["n_test"] and w["gain"] > 2 * w["gain_se"] and w["r2"] > 0
                   else "not predictable" if w["n_test"] else "too few sessions")
        lines.append(f"== {metric} ({kinds[metric]}): walk-forward R2 {w['r2']:+.3f}, gain "
                     f"{w['gain']:+.5f} ± {w['gain_se']:.5f} over {w['n_test']} sessions -> {verdict}")
        rows = assoc[assoc["metric"] == metric].dropna(subset=["rho"])
        rows = rows.reindex(rows["rho"].abs().sort_values(ascending=False).index).head(top)
        lines.append(f"   {'feature':34} {'n':>4} {'rho':>7} {'q':>8} {'early':>7} {'late':>7}")
        for _, r in rows.iterrows():
            lines.append(f"   {r['feature']:34} {int(r['n']):4d} {r['rho']:+7.3f} {r['q']:8.4f} "
                         f"{r['rho_early']:+7.3f} {r['rho_late']:+7.3f}{'  robust' if r['robust'] else ''}")
        lines.append("")
    robust = assoc[assoc["robust"]]
    by_kind = {k: int(robust["metric"].map(kinds).eq(k).sum()) for k in ("direction", "magnitude")}
    lines.append(f"Robust associations: {by_kind['direction']} with direction metrics, "
                 f"{by_kind['magnitude']} with magnitude metrics.")
    return "\n".join(lines)
