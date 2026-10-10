# research/ml_study_report.py
"""
The scoring stage of ml_study_v1 (research/ml_study_run.py): it first checks that every prediction file
is the one recorded (sha256) by its stage, then reads the outcomes and computes - per task, problem and
arm - the unhalved Brier score and log loss from the full vectors, reliability, dispersion, divergence
from the prior, paired per-session differences with moving-block bootstrap intervals, the audit's
diagnostics, the controls and the power analysis. Writes docs/reports/ml_study_v1/results.json,
scores_sessions.csv.gz and the report docs/reports/ml_study.md.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from contracts import nq_ml as ml
from research import ml_study as st
from research import ml_study_run as run

ARM_NAMES = {
    "A": "A frequencies (stored, frozen)", "B": "B analogues (stored, frozen)",
    "N": "N NQ-only logit (frozen refits)", "M": "M multi-instrument logit (frozen refits)",
    "P": "P pooled NQ+ES+RTY boosted (frozen refits)", "PRIOR": "A_s smoothed training frequencies",
    "CP": "CP conditional prior", "LR": "LR logistic (nested)", "GB": "GB boosted trees (nested)",
    "RES": "RES prior + residual (nested)", "BL": "BL blend with the prior", "TPF": "TPF TabPFN v2",
    "B_rth": "B_rth analogues RTH-20 (v3)", "B_rth_recent": "B_rth_recent analogues + recent path",
    "PRIOR_SYM": "the prior, symmetrised (no direction view)",
}


class Tampered(RuntimeError):
    pass


def verify() -> Dict[str, str]:
    """Every prediction file a stage recorded still has its recorded sha256; returns them."""
    m = run._manifest()
    checked = {}
    for stage, entry in (m.get("stages") or {}).items():
        for name, sha in (entry.get("files") or {}).items():
            if not isinstance(sha, str):
                continue
            fname = name.split(" ")[0]
            path = os.path.join(run.OUT if "(data/research)" in name else run.REPORT_DIR, fname)
            if not os.path.exists(path):
                raise Tampered(f"{fname} recorded by {stage} is missing")
            if run._sha(path) != sha:
                raise Tampered(f"{fname} changed after {stage} recorded it")
            checked[fname] = sha
    return checked


def _by_arm(frame: pd.DataFrame) -> Dict[str, Dict[int, np.ndarray]]:
    cols = [f"p_{c}" for c in st.CLASSES]
    out: Dict[str, Dict[int, np.ndarray]] = {}
    for arm, g in frame.groupby("arm"):
        out[arm] = dict(zip(g["row"].astype(int), g[cols].to_numpy(float)))
    return out


def with_symmetric_prior(preds: Dict[str, Dict[int, np.ndarray]]) -> Dict[str, Dict[int, np.ndarray]]:
    """Adds PRIOR_SYM: the stored prior with its bullish and bearish shares replaced by their mean - the same chance
    of a move past the band, no view on its direction. An analysis comparator added after the first scoring pass
    (docs/reports/ml_study.md, deviations): derived from stored predictions only, it changes none."""
    out = dict(preds)
    out["PRIOR_SYM"] = {r: np.array([(p[0] + p[1]) / 2, (p[0] + p[1]) / 2, p[2]]) for r, p in preds["PRIOR"].items()}
    return out


def summarise(problem: st.Problem, preds: Dict[str, Dict[int, np.ndarray]], rows: Sequence[int],
              arms: Sequence[str], refs: Sequence[str]) -> Dict[str, Any]:
    """The arms on ``rows`` (each arm must have every row): per arm the per-session mean Brier and log loss, then
    their means over sessions; reliability, dispersion and divergence from the prior; paired differences against
    each of ``refs``."""
    rows = np.asarray(sorted(rows), int)
    y = problem.y[rows]
    sess = [problem.sessions[problem.row_session[r]] for r in rows]
    prior = np.array([preds["PRIOR"][r] for r in rows]) if "PRIOR" in preds else None
    per: Dict[str, Dict[str, Dict[str, float]]] = {}
    out: Dict[str, Any] = {"rows": int(len(rows)), "sessions": int(len(set(sess))), "arms": {}}
    for arm in arms:
        P = np.array([preds[arm][r] for r in rows])
        b, ll = st.brier(P, y), st.logloss(P, y)
        per[arm] = {"brier": st.session_means(b, sess), "logloss": st.session_means(np.minimum(ll, 50.0), sess)}
        rel = st.reliability(P, y)
        a = {"brier": float(np.mean(list(per[arm]["brier"].values()))),
             "logloss": float(np.mean(list(per[arm]["logloss"].values()))),
             "logloss_infinite": int(np.isinf(ll).sum()), "ece": rel["ece_mean"],
             "ece_classes": {c: rel["classes"][c]["ece"] for c in st.CLASSES},
             "sd": {c: float(P[:, j].std()) for j, c in enumerate(st.CLASSES)},
             "mean": {c: float(P[:, j].mean()) for j, c in enumerate(st.CLASSES)}}
        if prior is not None:
            a["tv_prior"] = float(st.tv(P, prior).mean())
            a["kl_prior"] = float(st.kl(P, prior).mean())
            a["argmax_differs_prior"] = float((P.argmax(axis=1) != prior.argmax(axis=1)).mean())
        out["arms"][arm] = a
    # direction or size: the forecast with its bullish/bearish split symmetrised (only the neutral-or-not part
    # left), and the direction alone - p(bullish) / (p(bullish) + p(bearish)) on the rows that moved past the band
    moved = y != 2
    for arm in arms:
        P = np.array([preds[arm][r] for r in rows])
        S = P.copy()
        S[:, 0] = S[:, 1] = (P[:, 0] + P[:, 1]) / 2
        per[arm]["sym"] = st.session_means(st.brier(S, y), sess)
        q = P[moved, 1] / np.maximum(P[moved, 0] + P[moved, 1], 1e-12)
        per[arm]["dir"] = st.session_means(2 * (q - (y[moved] == 1)) ** 2, [x for x, m in zip(sess, moved) if m])
        out["arms"][arm]["sym_brier"] = float(np.mean(list(per[arm]["sym"].values())))
        out["arms"][arm]["dir_brier"] = float(np.mean(list(per[arm]["dir"].values())))
        t = st.paired(per[arm]["brier"], per[arm]["sym"])
        out["arms"][arm]["tilt_effect"] = {"mean": t["mean"], "interval": t["interval"]}
    for arm in arms:
        out["arms"][arm]["paired"] = {}
        for ref in refs:
            if ref == arm or ref not in per:
                continue
            res = st.paired(per[arm]["brier"], per[ref]["brier"])
            res_ll = st.paired(per[arm]["logloss"], per[ref]["logloss"])
            out["arms"][arm]["paired"][ref] = {
                "brier": res["mean"], "interval": res["interval"], "n": res["n"],
                "skill": 1 - out["arms"][arm]["brier"] / out["arms"][ref]["brier"],
                "logloss": res_ll["mean"], "logloss_interval": res_ll["interval"],
                "sym": {k: v for k, v in st.paired(per[arm]["sym"], per[ref]["sym"]).items()
                        if k in ("mean", "interval")},
                "dir": {k: v for k, v in st.paired(per[arm]["dir"], per[ref]["dir"]).items()
                        if k in ("mean", "interval", "n")}}
    out["per_session"] = per
    return out


# --------------------------------------------------------------------------
# The pre-open task
# --------------------------------------------------------------------------

def _week(conn, saved: Dict[str, Any]) -> Dict[str, Any]:
    """The five-session table: the recorded labels (2026-10-09's computed from the stored bars when none is
    recorded), every arm's full vector, Brier and log loss."""
    from forecaster import labels_prompt_v2 as labels
    pre = saved["pre"]
    snaps = {str(s["session_date"]): s for s in pre.data.snaps}
    rows, means = [], {}
    for d, r in saved["week"].items():
        lab = pre.data.y.get(d)
        source = "recorded outcome"
        if lab is None:
            out = labels.compute_outcome(snaps[d], labels.load_realised_bars(conn, snaps[d]))
            lab = out["labels"][ml.TARGET]["label"]
            source = "computed from the stored bars (no outcome recorded)"
        yi = np.array([st.CLASSES.index(lab)])
        row = {"session": d, "label": lab, "source": source, "trained_to": r["trained_to"], "arms": {}}
        for arm in ("A", "B", "N", "M", "P"):
            p = r.get(arm)
            if p is None:
                row["arms"][arm] = None
                continue
            P = np.array([p])
            row["arms"][arm] = {"p": [float(x) for x in p], "brier": float(st.brier(P, yi)[0]),
                                "logloss": float(st.logloss(P, yi)[0])}
            means.setdefault(arm, []).append(row["arms"][arm]["brier"])
        rows.append(row)
    mean = {a: float(np.mean(v)) for a, v in means.items()}
    # the earlier check's saved output (2026-10-09 21:00 UTC; its means were quoted rounded): the vectors must match
    with open(os.path.join(run.REPORT_DIR, "week_check_2026-10-09.json"), encoding="utf-8") as f:
        ref = json.load(f)
    diff = {a: max(float(np.max(np.abs(np.array(r["arms"][a]["p"]) - np.array(ref[r["session"]][a]))))
                   for r in rows if r["arms"].get(a)) for a in ("A", "B", "N", "M", "P")}
    labels_same = all(ref[r["session"]]["realised"] == r["label"] for r in rows)
    return {"rows": rows, "mean_brier": mean, "max_vector_diff": diff, "labels_same": labels_same,
            "identical": labels_same and max(diff.values()) < 1e-12, "reported": run.WEEK_REPORTED}


def _dev_reproduction(problem: st.Problem, preds, test_rows) -> Dict[str, Any]:
    """The development comparison (docs/reports/ml_development.json) recomputed from these predictions."""
    path = os.path.join(run.ROOT, "docs", "reports", "ml_development.json")
    with open(path, encoding="utf-8") as f:
        dev = json.load(f)
    keys = {"A": "A", "B": "B", "N": "nq_only/logit", "dev:nq_only/gbm": "nq_only/gbm", "M": "multi/logit",
            "dev:multi/gbm": "multi/gbm", "dev:pooled/logit": "pooled/logit", "P": "pooled/gbm"}
    rows = [r for r in test_rows if problem.y[r] >= 0 and all(r in preds[a] for a in keys)]
    y = problem.y[rows]
    out = {"sessions": len(rows), "arms": {}}
    for arm, key in keys.items():
        P = np.array([preds[arm][r] for r in rows])
        b = float(st.brier(P, y).mean())
        ll = float(st.logloss(P, y).mean())
        out["arms"][key] = {"brier": b, "reported_brier": dev["summary"][key]["brier"],
                            "logloss": ll, "reported_logloss": dev["summary"][key]["logloss"],
                            "abs_diff": abs(b - dev["summary"][key]["brier"])}
    out["identical"] = all(v["abs_diff"] < 1e-9 for v in out["arms"].values()) and \
        out["sessions"] == dev["common"]
    return out


def _features(saved) -> Dict[str, Any]:
    pre = saved["pre"]
    X = pre.data.X["multi"]
    out = {}
    for c in X.columns:
        x = X[c].to_numpy(float)
        ok = x[~np.isnan(x)]
        vals, counts = np.unique(ok, return_counts=True)
        out[c] = {"missing": float(np.isnan(x).mean()),
                  "modal_share": float(counts.max() / len(ok)) if len(ok) else None,
                  "mean": float(ok.mean()) if len(ok) else None, "sd": float(ok.std()) if len(ok) else None,
                  "p01": float(np.quantile(ok, 0.01)) if len(ok) else None,
                  "p99": float(np.quantile(ok, 0.99)) if len(ok) else None}
    return out


def _path(saved, problem: st.Problem, test_rows) -> Dict[str, Any]:
    """The regularisation path (post-hoc: scored here, never used to choose)."""
    out = {}
    rows = [r for r in test_rows if problem.y[r] >= 0]
    y = problem.y[rows]
    prior = np.array([st.laplace(problem.y[[i for i in range(len(problem.sessions))
                                            if i < r and problem.y[i] >= 0]]) for r in rows])
    for cfg, per_c in saved["path"].items():
        out[cfg] = []
        for c, v in per_c.items():
            P = np.array([v["preds"][problem.sessions[r]] for r in rows])
            out[cfg].append({"C": c, "test_brier": float(st.brier(P, y).mean()),
                             "tuning_brier": float(np.mean(v["tuning_brier"])),
                             "tv_from_frequencies": float(st.tv(P, prior).mean()),
                             "sd_bullish": float(P[:, 1].std()), "max_abs_coef": float(np.mean(v["coef_norm"]))})
    return out


def _folds(saved, problem: st.Problem) -> List[Dict[str, Any]]:
    pre = saved["pre"]
    out = []
    for f, log, rec in zip(saved["folds"], pre.frozen_log, saved["run"]["folds"]):
        ytr = problem.y[[i for i in f.train if problem.y[i] >= 0]]
        out.append({"train": rec["train"], "test": rec["test"],
                    "class_counts": {c: int((ytr == j).sum()) for j, c in enumerate(st.CLASSES)},
                    "frozen_chosen": log["chosen"], "pooled_rows": log.get("pooled_training_rows"),
                    "study_chosen": rec["best"]})
    return out


def fold_table(summary: Dict[str, Any], fold_sessions: List[List[str]], arms: Sequence[str]) -> List[Dict[str, Any]]:
    """Per outer fold: its test span and each arm's mean per-session Brier score over the fold's scored sessions."""
    per = summary["per_session"]
    out = []
    for days in fold_sessions:
        scored = [d for d in days if all(d in per[a]["brier"] for a in arms if a in per)]
        if not scored:
            continue
        out.append({"test": [scored[0], scored[-1]], "sessions": len(scored),
                    **{a: float(np.mean([per[a]["brier"][d] for d in scored])) for a in arms if a in per}})
    return out


def feature_choices(fold_records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """How often each tuned family's inner selection chose each feature set ('nq' only, 'multi')."""
    out: Dict[str, Dict[str, int]] = {}
    for f in fold_records:
        for fam, cfg in f["best"].items():
            if isinstance(cfg, dict) and "features" in cfg:
                out.setdefault(fam, {}).setdefault(cfg["features"], 0)
                out[fam][cfg["features"]] += 1
    return out


def preopen(conn) -> Dict[str, Any]:
    saved = run._load("preopen.pkl")
    problem: st.Problem = saved["pre"].problem
    frame = pd.read_csv(os.path.join(run.REPORT_DIR, "predictions_preopen.csv"))
    preds = _by_arm(frame)
    tp = os.path.join(run.OUT, "predictions_tabpfn_preopen.csv")
    if os.path.exists(tp):
        t = pd.read_csv(tp)
        preds["TPF"] = dict(zip(t["row"].astype(int), t[[f"p_{c}" for c in st.CLASSES]].to_numpy(float)))
    folds = saved["folds"]
    test_rows = [r for f in folds for r in f.test]
    labelled = [r for r in test_rows if problem.y[r] >= 0]
    preds = with_symmetric_prior(preds)
    arms = [a for a in ("A", "B", "N", "M", "P", "PRIOR", "PRIOR_SYM", "CP", "LR", "GB", "RES", "BL", "TPF")
            if a in preds]
    common = [r for r in labelled if all(r in preds[a] for a in arms)]
    res = {"summary": summarise(problem, preds, common, arms, ("A", "B", "PRIOR", "PRIOR_SYM")),
           "dev_reproduction": _dev_reproduction(problem, preds, test_rows),
           "week": _week(conn, saved), "features": _features(saved), "path": _path(saved, problem, test_rows),
           "folds": _folds(saved, problem), "availability": saved["availability"], "pooled": saved["pooled"],
           "production": saved["production"], "abstain_in_test": [d for d in saved["pre"].abstain
                                                                   if d >= problem.sessions[folds[0].test[0]]],
           "test_sessions": len(test_rows), "labelled_test": len(labelled), "common": len(common)}
    res["by_fold"] = fold_table(res["summary"], [[problem.sessions[i] for i in f.test] for f in folds],
                                ["A", "B", "N", "M", "P", "PRIOR_SYM", "LR", "GB", "TPF"])
    res["feature_choices"] = feature_choices(saved["run"]["folds"])
    return res


# --------------------------------------------------------------------------
# The RTH task
# --------------------------------------------------------------------------

def _crps_gauss(y, mu, sigma):
    from scipy.stats import norm
    w = (y - mu) / sigma
    return sigma * (w * (2 * norm.cdf(w) - 1) + 2 * norm.pdf(w) - 1 / math.sqrt(math.pi))


def _crps_members(xs: np.ndarray, y: float) -> float:
    """CRPS of the empirical distribution of ``xs`` (sorted) at y: E|X - y| - 0.5 E|X - X'|."""
    n = len(xs)
    k = np.arange(1, n + 1)
    spread = 2 * float((xs * (2 * k - n - 1)).sum()) / (n * n)
    return float(np.abs(xs - y).mean()) - 0.5 * spread


def _crps_folded(y: np.ndarray, mu: np.ndarray, sigma: np.ndarray, n: int = 2000) -> np.ndarray:
    """CRPS of |X|, X ~ N(mu, sigma^2), at y >= 0: the integral of F(x)^2 over [0, y] and of (1 - F(x))^2 beyond, F the
    folded normal's distribution function - numerically (trapezoid, the jump at y exact)."""
    from scipy.stats import norm
    out = np.empty(len(y))
    for i, (v, m, s_) in enumerate(zip(y, mu, sigma)):
        F = lambda x: norm.cdf((x - m) / s_) - norm.cdf((-x - m) / s_)      # noqa: E731
        lo = np.linspace(0.0, v, n)
        hi = np.linspace(v, max(v, abs(m) + 10 * s_), n)
        out[i] = np.trapezoid(F(lo) ** 2, lo) + np.trapezoid((1 - F(hi)) ** 2, hi)
    return out


def _distribution(rt, name: str) -> Dict[str, Any]:
    """Size and direction as distributions at 15 minutes, on identical rows: the same-clock empirical moves (CLOCK),
    the scale model at zero drift (LS0) and with the ridge location (LSmu), and the deployed fan_rw_v1 (FAN) - the
    signed move (CRPS of z) and its size alone (CRPS of |z|) scored separately, in z units."""
    from scipy.stats import norm
    p = rt["problems"][name]
    problem, rows, folds = p["problem"], p["rows"], p["folds"]
    dist = pd.read_csv(os.path.join(run.OUT, "distribution_rth_h15.csv.gz"))
    dist = dist[dist["problem"] == name].set_index("row")
    fpath = os.path.join(run.OUT, "fan_rth_h15.csv.gz")
    fanz = pd.read_csv(fpath) if os.path.exists(fpath) else pd.DataFrame(columns=["problem", "row", "fan_sigma_z"])
    fanz = fanz[fanz["problem"] == name].set_index("row")["fan_sigma_z"]
    z = rows["z"].to_numpy(float)
    ok = (problem.y >= 0) & np.isfinite(z)
    out_rows = []
    for f in folds:
        tr = np.flatnonzero(np.isin(problem.row_session, f.train) & ok)
        members = {c: np.sort(z[tr][problem.keys[tr] == c]) for c in np.unique(problem.keys[tr])}
        for r in np.flatnonzero(np.isin(problem.row_session, f.test) & ok):
            xs = members.get(problem.keys[r])
            if xs is None or len(xs) < 30 or r not in dist.index:
                continue
            mu, sg = float(dist.loc[r, "mu"]), float(dist.loc[r, "sigma"])
            sf = float(fanz.loc[r]) if r in fanz.index else np.nan
            q = {lv: (np.quantile(xs, (1 - lv) / 2), np.quantile(xs, (1 + lv) / 2)) for lv in (0.5, 0.9)}
            out_rows.append({
                "session": problem.sessions[problem.row_session[r]], "cutoff": int(problem.keys[r]), "z": z[r],
                "CLOCK_emp": _crps_members(xs, z[r]), "LS0": float(_crps_gauss(z[r], 0.0, sg)),
                "LSmu": float(_crps_gauss(z[r], mu, sg)), "mu": mu, "sigma": sg, "fan_sigma": sf,
                "FAN": float(_crps_gauss(z[r], 0.0, sf)) if np.isfinite(sf) else np.nan,
                "abs_CLOCK_emp": _crps_members(np.sort(np.abs(xs)), abs(z[r])),
                "cover50_emp": q[0.5][0] <= z[r] <= q[0.5][1], "cover90_emp": q[0.9][0] <= z[r] <= q[0.9][1],
                "width90_emp": q[0.9][1] - q[0.9][0],
                "cover50_ls": abs(z[r]) <= norm.ppf(0.75) * sg, "cover90_ls": abs(z[r]) <= norm.ppf(0.95) * sg,
                "width90_ls": 2 * norm.ppf(0.95) * sg,
                "cover90_fan": abs(z[r]) <= norm.ppf(0.95) * sf if np.isfinite(sf) else np.nan,
                "width90_fan": 2 * norm.ppf(0.95) * sf if np.isfinite(sf) else np.nan})
    d = pd.DataFrame(out_rows)
    res: Dict[str, Any] = {"rows_all": len(d), "sessions_all": int(d["session"].nunique())}
    # every comparison on the rows every arm has - the fan forecasts full, complete sessions only
    d = d[np.isfinite(d["FAN"])].reset_index(drop=True)
    d["abs_LS0"] = _crps_folded(np.abs(d["z"].to_numpy()), np.zeros(len(d)), d["sigma"].to_numpy())
    d["abs_LSmu"] = _crps_folded(np.abs(d["z"].to_numpy()), d["mu"].to_numpy(), d["sigma"].to_numpy())
    d["abs_FAN"] = _crps_folded(np.abs(d["z"].to_numpy()), np.zeros(len(d)), d["fan_sigma"].to_numpy())
    sess = d["session"].tolist()
    res.update(rows=len(d), sessions=int(d["session"].nunique()))
    arms = ("CLOCK_emp", "LS0", "LSmu", "FAN")
    for task, prefix in (("signed", ""), ("absolute", "abs_")):
        per = {a: st.session_means(d[prefix + a].to_numpy(float), sess) for a in arms}
        res[task] = {"crps": {a: float(np.mean(list(per[a].values()))) for a in arms},
                     "paired": {f"{a} - {b}": {k: v for k, v in st.paired(per[a], per[b]).items()
                                               if k in ("mean", "interval", "n")}
                                for a, b in (("LS0", "FAN"), ("CLOCK_emp", "FAN"), ("LS0", "CLOCK_emp"),
                                             ("LSmu", "LS0"))}}
    res["crps"] = res["signed"]["crps"]                      # (the earlier keys, kept for the summary bullets)
    res["paired"] = {"LS0 - CLOCK_emp": res["signed"]["paired"]["LS0 - CLOCK_emp"],
                     "LSmu - LS0": res["signed"]["paired"]["LSmu - LS0"]}
    res["coverage"] = {k: float(d[k].astype(float).mean()) for k in
                       ("cover50_emp", "cover90_emp", "cover50_ls", "cover90_ls", "cover90_fan")}
    res["width90"] = {"emp": float(d["width90_emp"].mean()), "ls": float(d["width90_ls"].mean()),
                      "fan": float(d["width90_fan"].mean())}
    nz = d[d["mu"].abs() > 1e-9]
    res["direction"] = {"sign_hit": float((np.sign(nz["mu"]) == np.sign(nz["z"])).mean()) if len(nz) else None,
                        "corr_mu_z": float(np.corrcoef(d["mu"], d["z"])[0, 1]) if d["mu"].std() > 0 else None,
                        "mu_sd": float(d["mu"].std()), "alphas": dist["alpha"].value_counts().to_dict()}
    res["scale"] = {"corr_log_sigma_abs_z": float(np.corrcoef(np.log(d["sigma"]), np.log(np.abs(d["z"]) + 1e-3))
                                                  [0, 1]),
                    "corr_log_fan_sigma_abs_z": float(np.corrcoef(np.log(d["fan_sigma"]),
                                                                  np.log(np.abs(d["z"]) + 1e-3))[0, 1])}
    return res


def rth() -> Dict[str, Any]:
    rt = run._load("rth.pkl")
    frame = pd.read_csv(os.path.join(run.OUT, "predictions_rth.csv.gz"))
    afile = os.path.join(run.OUT, "predictions_rth_analogues.csv.gz")
    analog = pd.read_csv(afile) if os.path.exists(afile) else pd.DataFrame()
    out: Dict[str, Any] = {"problems": {}, "label_reasons": rt["labels"]["reason"].value_counts().to_dict()}
    for name, p in rt["problems"].items():
        problem, rows = p["problem"], p["rows"]
        preds = _by_arm(frame[frame["problem"] == name])
        if not analog.empty:
            a = analog[analog["problem"] == name]
            key = {(s, c): i for i, (s, c) in enumerate(zip(rows["session"], rows["cutoff"]))}
            for arm, g in a.groupby("arm"):
                preds[arm] = {key[(s, c)]: np.array(v) for s, c, v in
                              zip(g["session"], g["cutoff"], g[[f"p_{x}" for x in st.CLASSES]].to_numpy(float))
                              if (s, c) in key}
        tname = {"rth/h15/cutoff": "rth_h15_cutoff", "rth/h15/delayed": "rth_h15_delayed"}.get(name)
        tp = os.path.join(run.OUT, f"predictions_tabpfn_{tname}.csv") if tname else None
        if tp and os.path.exists(tp):
            t = pd.read_csv(tp)
            preds["TPF"] = dict(zip(t["row"].astype(int), t[[f"p_{c}" for c in st.CLASSES]].to_numpy(float)))
        test_rows = [r for f in p["folds"] for r in np.flatnonzero(np.isin(problem.row_session, f.test))]
        labelled = [r for r in test_rows if problem.y[r] >= 0]
        preds = with_symmetric_prior(preds)
        arms = [a for a in ("PRIOR", "PRIOR_SYM", "CP", "LR", "GB", "RES", "BL", "TPF", "B_rth", "B_rth_recent")
                if a in preds]
        matched = [r for r in labelled if all(r in preds[a] for a in arms)]
        # the policy view: every labelled test row, an arm without a forecast falls back to the prior (counted)
        policy = {a: {r: preds[a].get(r, preds["PRIOR"][r]) for r in labelled} for a in arms}
        fallbacks = {a: int(sum(1 for r in labelled if r not in preds[a])) for a in arms}
        refs = ("PRIOR", "PRIOR_SYM", "B_rth") if "B_rth" in arms else ("PRIOR", "PRIOR_SYM")
        res = {"test_rows": len(test_rows), "labelled": len(labelled), "fallbacks": fallbacks,
               "matched": summarise(problem, preds, matched, arms, refs),
               "policy": summarise(problem, policy, labelled, arms, refs)}
        res["phases"] = {}
        for ph, (lo, hi) in st.RTH_PHASES.items():
            sub = [r for r in matched if lo <= problem.keys[r] <= hi]
            if sub:
                s = summarise(problem, preds, sub, arms, ("PRIOR", "PRIOR_SYM"))
                res["phases"][ph] = {a: {"brier": v["brier"], "vs_prior": v["paired"].get("PRIOR"),
                                         "vs_sym": v["paired"].get("PRIOR_SYM")} for a, v in s["arms"].items()}
        if name.startswith("rth/h15/"):
            res["distribution"] = _distribution(rt, name)
            res["by_fold"] = fold_table(res["matched"], [[problem.sessions[i] for i in f.test] for f in p["folds"]],
                                        ["PRIOR", "PRIOR_SYM", "LR", "GB", "TPF", "B_rth"])
        res["feature_choices"] = feature_choices(p["run"]["folds"])
        out["problems"][name] = res
    return out


# --------------------------------------------------------------------------
# Power, controls
# --------------------------------------------------------------------------

LEVEL = float(ml.promotion_rule()["interval_level"])            # 98.33 %: Bonferroni over three candidates


def power_table(diffs: Dict[str, List[float]]) -> Dict[str, Any]:
    out = {}
    for name, d in diffs.items():
        d = np.asarray(d, float)
        sd, deff = float(d.std(ddof=1)), st.design_effect(d)
        row = {"n": len(d), "mean": float(d.mean()), "sd": sd, "design_effect": deff,
               "registered_rule": {}, "material_rule": {}, "material_fixed_sequence": {}}
        for delta in (0.01, 0.02, 0.03, 0.05):
            row["registered_rule"][str(delta)] = st.sessions_needed(sd, delta, LEVEL, point=0.01, deff=deff)
            row["material_rule"][str(delta)] = st.sessions_needed(sd, delta, LEVEL, margin=0.01, deff=deff)
            # a fixed-sequence (hierarchical) design tests each candidate at the full 95 %
            row["material_fixed_sequence"][str(delta)] = st.sessions_needed(sd, delta, 0.95, margin=0.01, deff=deff)
        row["power_at_60"] = {str(delta): st.power(60, sd, delta, LEVEL, point=0.01, deff=deff)
                              for delta in (0.01, 0.02, 0.03, 0.05)}
        row["detectable_at_60"] = {"registered": st.detectable(60, sd, LEVEL, point=0.01, deff=deff),
                                   "material": st.detectable(60, sd, LEVEL, margin=0.01, deff=deff)}
        out[name] = row
    return out


def _diffs(summary: Dict[str, Any], a: str, b: str) -> List[float]:
    per = summary["per_session"]
    days = sorted(set(per[a]["brier"]) & set(per[b]["brier"]))
    return [per[a]["brier"][d] - per[b]["brier"][d] for d in days]


def controls_summary() -> Optional[Dict[str, Any]]:
    path = os.path.join(run.OUT, "controls.pkl")
    if not os.path.exists(path):
        return None
    c = run._load("controls.pkl")
    out: Dict[str, Any] = {"invariance": c["invariance"]}
    for task in ("preopen", "rth"):
        rows = c[task]
        sh = [r for r in rows if r["kind"] == "shuffled"]
        arms = sorted({a for r in sh for a in r["skill_vs_prior"]})
        out[f"{task}_shuffled"] = {
            a: {"runs": len(sh), "mean": float(np.mean([r["skill_vs_prior"][a]["mean"] for r in sh])),
                "below_zero_interval": sum(1 for r in sh if r["skill_vs_prior"][a]["interval"]
                                           and r["skill_vs_prior"][a]["interval"][1] < 0),
                "point_at_most_minus_001": sum(1 for r in sh if r["skill_vs_prior"][a]["mean"] <= -0.01),
                "mean_vs_sym": float(np.mean([r["skill_vs_prior"][a]["vs_sym"]["mean"] for r in sh]))
                if all("vs_sym" in r["skill_vs_prior"][a] for r in sh) else None,
                "below_zero_vs_sym": (sum(1 for r in sh if r["skill_vs_prior"][a]["vs_sym"]["interval"]
                                          and r["skill_vs_prior"][a]["vs_sym"]["interval"][1] < 0)
                                      if all("vs_sym" in r["skill_vs_prior"][a] for r in sh) else None)}
            for a in arms}
        syn = [r for r in rows if r["kind"] == "synthetic"]
        out[f"{task}_synthetic"] = {}
        for beta in sorted({r["beta"] for r in syn}):
            rs = [r for r in syn if r["beta"] == beta]
            out[f"{task}_synthetic"][str(beta)] = {
                "runs": len(rs), "oracle_gain": float(np.mean([r["oracle_gain"] for r in rs])),
                "arms": {a: {"mean": float(np.mean([r["skill_vs_prior"][a]["mean"] for r in rs])),
                             "detected": sum(1 for r in rs if r["skill_vs_prior"][a]["interval"]
                                             and r["skill_vs_prior"][a]["interval"][1] < 0)}
                         for a in rs[0]["skill_vs_prior"]}}
    return out


def score(conn) -> Dict[str, Any]:
    checked = verify()
    pre = preopen(conn)
    r = rth()
    ctl = controls_summary()
    s = pre["summary"]
    diffs = {f"{a} - A (pre-open)": _diffs(s, a, "A") for a in ("N", "M", "P", "LR", "BL") if a in s["per_session"]}
    diffs.update({f"{a} - B (pre-open)": _diffs(s, a, "B") for a in ("N", "M", "P") if a in s["per_session"]})
    prim = r["problems"]["rth/h15/cutoff"]["matched"]
    diffs.update({f"{a} - CLOCK (RTH 15 min, cutoff origin)": _diffs(prim, a, "PRIOR")
                  for a in ("LR", "GB", "B_rth") if a in prim["per_session"]})
    diffs.update({f"{a} - symmetric prior (RTH 15 min, cutoff origin)": _diffs(prim, a, "PRIOR_SYM")
                  for a in ("LR", "GB") if a in prim["per_session"]})
    results = {"version": st.VERSION, "protocol_hash": st.protocol_hash(), "verified_files": checked,
               "preopen": pre, "rth": r, "controls": ctl, "power": power_table(diffs)}
    _write_scores(pre, r)
    lean = json.loads(json.dumps(results, default=_jsonable))
    for task in (lean["preopen"]["summary"],):
        task.pop("per_session", None)
    for p in lean["rth"]["problems"].values():
        p["matched"].pop("per_session", None)
        p["policy"].pop("per_session", None)
    with open(os.path.join(run.REPORT_DIR, "results.json"), "w", encoding="utf-8") as f:
        json.dump(lean, f, indent=1)
    run._record("score", {"verified": sorted(checked)})
    from research import ml_study_markdown as md
    with open(run.REPORT, "w", encoding="utf-8") as f:
        f.write(md.render(results))
    return {"report": run.REPORT, "verified": len(checked)}


def _jsonable(x):
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return str(x)


def _write_scores(pre: Dict[str, Any], r: Dict[str, Any]) -> None:
    rows = []
    for arm, v in pre["summary"]["per_session"].items():
        for d, b in v["brier"].items():
            rows.append({"problem": "preopen", "arm": arm, "session": d, "brier": b, "logloss": v["logloss"][d]})
    for name, p in r["problems"].items():
        for arm, v in p["matched"]["per_session"].items():
            for d, b in v["brier"].items():
                rows.append({"problem": name, "arm": arm, "session": d, "brier": b, "logloss": v["logloss"][d]})
    pd.DataFrame(rows).to_csv(os.path.join(run.REPORT_DIR, "scores_sessions.csv.gz"), index=False,
                              float_format="%.8g", compression={"method": "gzip", "mtime": 0})
