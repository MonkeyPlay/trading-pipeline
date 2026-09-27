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

  volatility    for the magnitude metrics, the same walk-forward on a fixed set of
                 pre-open volatility inputs (VOL_FEATURES, chosen on market grounds -
                 volatility clusters - not from the correlations) with the metric's
                 log as the target (ranges are right-skewed, so squared error on raw
                 values is dominated by a few large days); and the candidate label
                 "above the median of the previous ``median_window`` sessions" (known
                 before the session, so point-in-time) fitted by logistic regression
                 against its running base rate - log-loss gain +/- SE, the test the
                 forecast models must pass.

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

# The pre-open volatility inputs - the forecast model's own set (its logistic_vol candidate).
VOL_FEATURES = models_v2.VOL_FEATURES
LOG_FLOOR = 0.01   # added before the log of an absolute return, which can be 0


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

def _vol_pipeline(estimator):
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                     ("scale", StandardScaler()), ("model", estimator)])


def _gain_summary(gains: np.ndarray) -> Tuple[float, float]:
    n = len(gains)
    return float(gains.mean()), (float(gains.std(ddof=1) / math.sqrt(n)) if n > 1 else math.nan)


def walk_forward(X: pd.DataFrame, y: pd.Series, min_train: int = 120, refit_every: int = 5,
                 features: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Ridge vs the running mean, each session predicted from earlier sessions
    only: on all model inputs (the model's preprocessing), or on ``features``
    (numeric) alone. Returns n_test, r2 (1 - SSE_model / SSE_mean) and
    gain / gain_se (mean per-session squared-error reduction; > 0 is better).
    """
    import warnings

    from sklearn.linear_model import RidgeCV
    from sklearn.pipeline import Pipeline

    if features is not None:
        X = X[features]
    keep = y.notna().to_numpy()
    X, y = X[keep].reset_index(drop=True), y[keep].reset_index(drop=True)
    if len(y) <= min_train:
        return {"n_test": 0, "r2": math.nan, "gain": math.nan, "gain_se": math.nan}
    se_model, se_mean, pipe = [], [], None
    for i in range(min_train, len(y)):
        if pipe is None or (i - min_train) % refit_every == 0:
            pipe = (_vol_pipeline(RidgeCV(alphas=RIDGE_ALPHAS)) if features is not None else
                    Pipeline([("features", models_v2._preprocessor()), ("model", RidgeCV(alphas=RIDGE_ALPHAS))]))
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
    gain, gain_se = _gain_summary(se_mean - se_model)
    return {"n_test": len(se_model), "r2": 1 - se_model.sum() / se_mean.sum() if se_mean.sum() > 0 else math.nan,
            "gain": gain, "gain_se": gain_se}


def above_trailing_median(y: pd.Series, window: int) -> pd.Series:
    """1.0 when a session's value exceeds the median of the previous ``window``
    sessions with a value, 0.0 when not, NaN before that - known before the
    session, so a point-in-time label."""
    vals = y.dropna()
    med = vals.shift(1).rolling(window, min_periods=window).median()
    lab = (vals > med).astype(float).where(med.notna())
    return lab.reindex(y.index)


def walk_forward_binary(X: pd.DataFrame, y: pd.Series, features: List[str], median_window: int = 40,
                        min_train: int = 120, refit_every: int = 5, min_labels: int = 60,
                        C: float = 0.1) -> Dict[str, Any]:
    """
    The label above_trailing_median(y) predicted by an L2 logistic regression on
    ``features``, from session ``min_train`` on and with at least ``min_labels``
    earlier labels, against the Laplace-smoothed running base rate. Returns
    n_test, base_rate, accuracy / base_accuracy and the mean per-session log-loss
    gain (> 0: better than the base rate) with its standard error.
    """
    import warnings

    from sklearn.linear_model import LogisticRegression

    lab = above_trailing_median(y, median_window).to_numpy()
    Xf = X[features].reset_index(drop=True)
    gains, hits, base_hits, pipe, p0 = [], 0, 0, None, None
    for i in range(min_train, len(lab)):
        if np.isnan(lab[i]):
            continue
        train = np.flatnonzero(~np.isnan(lab[:i]))
        if len(train) < min_labels:
            continue
        y_tr = lab[train]
        if pipe is None or len(gains) % refit_every == 0:
            p0 = (y_tr.sum() + 1) / (len(y_tr) + 2)
            pipe = None
            if 0 < y_tr.sum() < len(y_tr):
                pipe = _vol_pipeline(LogisticRegression(C=C, max_iter=2000))
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    pipe.fit(Xf.iloc[train], y_tr)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p1 = float(pipe.predict_proba(Xf.iloc[[i]])[0, 1]) if pipe is not None else p0
        p1 = min(max(p1, 1e-6), 1 - 1e-6)
        yi = lab[i]
        ll_model = -math.log(p1 if yi else 1 - p1)
        ll_base = -math.log(p0 if yi else 1 - p0)
        gains.append(ll_base - ll_model)
        hits += int((p1 > 0.5) == bool(yi))
        base_hits += int((p0 > 0.5) == bool(yi))
    n = len(gains)
    if not n:
        return {"n_test": 0, "base_rate": math.nan, "accuracy": math.nan, "base_accuracy": math.nan,
                "gain": math.nan, "gain_se": math.nan}
    gain, gain_se = _gain_summary(np.asarray(gains))
    valid = lab[~np.isnan(lab)]
    return {"n_test": n, "base_rate": float(valid.mean()), "accuracy": hits / n, "base_accuracy": base_hits / n,
            "gain": gain, "gain_se": gain_se}


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def study(md, snapshots, min_train: int = 120, refit_every: int = 5, median_window: int = 40) -> Dict[str, Any]:
    snapshots = list(snapshots)
    X, Y = collect(md, snapshots)
    assoc = associations(X, Y)
    wf = {metric: walk_forward(X, Y[metric], min_train, refit_every) for metric in Y.columns}
    vol = {}
    for metric, _, kind in METRICS:
        if kind != "magnitude" or metric.startswith("efficiency"):
            continue
        logy = np.log(Y[metric].clip(lower=0) + (LOG_FLOOR if metric.startswith("|") else 0.0))
        logy = logy.replace(-np.inf, np.nan)
        vol[metric] = {
            "log_ridge": walk_forward(X, logy, min_train, refit_every, features=VOL_FEATURES),
            "above_median": walk_forward_binary(X, Y[metric], VOL_FEATURES, median_window, min_train, refit_every),
        }
    return {"sessions": len(snapshots), "first": X.index[0] if len(X) else None,
            "last": X.index[-1] if len(X) else None, "associations": assoc, "walk_forward": wf,
            "volatility": vol, "min_train": min_train, "median_window": median_window}


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
    vol = result.get("volatility") or {}
    if vol:
        lines += ["", f"Volatility inputs only ({', '.join(VOL_FEATURES)}):",
                  f"  log ridge: the metric's log, ridge vs the running mean; above median: the label 'above "
                  f"the median of the previous {result['median_window']} sessions', logistic vs the running base "
                  f"rate (log-loss gain per session). Predictable = gain > 2 SE.",
                  f"   {'metric':22} {'log ridge R2':>12} {'gain ± SE':>22}   {'above median: n':>15} "
                  f"{'acc':>6} {'base':>6} {'log-loss gain ± SE':>22}"]
        for metric, v in vol.items():
            r, b = v["log_ridge"], v["above_median"]
            ok_r = r["n_test"] and r["gain"] > 2 * r["gain_se"]
            ok_b = b["n_test"] and b["gain"] > 2 * b["gain_se"]
            lines.append(f"   {metric:22} {r['r2']:+12.3f} {r['gain']:+10.5f} ± {r['gain_se']:.5f}{' *' if ok_r else '  '}"
                         f" {b['n_test']:15d} {b['accuracy']:6.3f} {b['base_accuracy']:6.3f} "
                         f"{b['gain']:+10.4f} ± {b['gain_se']:.4f}{' *' if ok_b else ''}")
        lines.append("   * predictable out of sample (gain > 2 SE)")
    return "\n".join(lines)
