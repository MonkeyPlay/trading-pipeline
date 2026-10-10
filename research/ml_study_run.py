# research/ml_study_run.py
"""
Runs ml_study_v1 (research/ml_study.py) in stages, each writing its results before the next reads them:

  predict    the pre-open and RTH datasets, every family's outer predictions (nested selection inside each
             outer training window), the frozen arms, the five-session reproduction and the regularisation
             path; the predictions are written with their sha256 BEFORE any outer outcome is read
  analogues  B_rth and B_rth_recent at every cutoff of the RTH test sessions
  tabpfn     not here: research/tabpfn_arm.py, in .venv-research, reads the exported inputs and writes its
             predictions beside the others (scripts/ml_study.py prints the command)
  controls   shuffled labels, a synthetic known signal, future-bar invariance
  score      checks the stored predictions' hashes, then reads the outcomes: scores, paired differences,
             reliability, dispersion, the audit's diagnostics, power - docs/reports/ml_study.md

Working files go to data/research/ml_study_v1/ (not committed); the manifest, trial records, predictions
and per-session scores to docs/reports/ml_study_v1/.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import pickle
import platform
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from contracts import nq_ml as ml
from research import ml_study as st
from research import ml_study_data as dd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "research", st.VERSION)
REPORT_DIR = os.path.join(ROOT, "docs", "reports", st.VERSION)
REPORT = os.path.join(ROOT, "docs", "reports", "ml_study.md")
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
# the five-session check's unhalved Brier means (docs/reports/audit_2026-10-09.md, scratchpad week_ml.py)
WEEK_REPORTED = {"A": 0.660, "B": 0.719, "N": 0.662, "M": 0.669, "P": 0.763}
FROZEN = {"N": "nq_only/logit", "M": "multi/logit", "P": "pooled/gbm"}
PATH_C = (0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0, 100.0)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest() -> Dict[str, Any]:
    path = os.path.join(REPORT_DIR, "manifest.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _record(stage: str, entry: Dict[str, Any]) -> None:
    """Records a stage's outputs in the manifest. A re-run keeps the earlier run's time and files beside it and says
    whether every file it wrote again is byte-identical (``reproduced``)."""
    os.makedirs(REPORT_DIR, exist_ok=True)
    m = _manifest()
    from forecaster.provenance import code_revision
    m.update({"version": st.VERSION, "protocol_hash": st.protocol_hash(), "protocol": st.PROTOCOL})
    old = (m.get("stages") or {}).get(stage)
    new = {"at": _now(), "code_revision": code_revision(), **entry}
    if old and old.get("files") and new.get("files"):
        same = {k: old["files"].get(k) == v for k, v in new["files"].items() if isinstance(v, str)}
        runs = old.get("earlier_runs", []) + [{"at": old["at"], "files": {k: v for k, v in old["files"].items()
                                                                        if isinstance(v, str)}}]
        new.update(earlier_runs=runs, reproduced=bool(same) and all(same.values()))
    m.setdefault("stages", {})[stage] = new
    with open(os.path.join(REPORT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1, sort_keys=True, default=str)


def _software() -> Dict[str, str]:
    import joblib
    import scipy
    import sklearn
    return {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
            "scikit-learn": sklearn.__version__, "scipy": scipy.__version__, "joblib": joblib.__version__}


def _save(name: str, obj: Any) -> None:
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "wb") as f:
        pickle.dump(obj, f)


def _load(name: str) -> Any:
    with open(os.path.join(OUT, name), "rb") as f:
        return pickle.load(f)


def _pred_frame(problem: st.Problem, preds: Dict[str, Dict[int, np.ndarray]], extra: Optional[pd.DataFrame] = None
                ) -> pd.DataFrame:
    """Long table: one row per arm and problem row - session (and cutoff), arm, the three probabilities."""
    out = []
    for arm, by_row in preds.items():
        for row, p in sorted(by_row.items()):
            r = {"problem": problem.name, "row": row, "session": problem.sessions[problem.row_session[row]],
                 "arm": arm}
            if extra is not None:
                r["cutoff"] = int(extra.loc[row, "cutoff"])
            r.update({f"p_{c}": float(p[j]) for j, c in enumerate(st.CLASSES)})
            out.append(r)
    return pd.DataFrame(out)


# --------------------------------------------------------------------------
# predict: the pre-open task
# --------------------------------------------------------------------------

def _week(pre: dd.Preopen) -> Dict[str, Any]:
    """The five-session check reproduced: per day N, M and P refitted on every labelled session before it (the
    earlier check's method, no embargo), beside the stored A and B runs - full vectors."""
    from forecaster import ml_features as mf
    from forecaster import ml_model as mm
    data = pre.data
    out = {}
    for d in WEEK:
        train = [x for x in data.days if x < d and data.y[x] is not None]
        row = {"trained_to": train[-1], "sessions": len(train),
               "A": pre.stored["A"].get(d), "B": pre.stored["B"].get(d)}
        for arm, version in (("N", ml.ML_NQ_VERSION), ("M", ml.ML_MULTI_VERSION)):
            cfg = ml.CONFIG_OF[version]
            model, params, _ = mm.tune(data.X[cfg].loc[train].to_numpy(), [data.y[x] for x in train],
                                       ml.FAMILY[version])
            row[arm] = (None if cfg == "multi" and mf.required_missing(data.fs, d) else
                        mm.probabilities(model, data.X[cfg].loc[[d]].to_numpy())[0])
            row[f"{arm}_params"] = params
        keep = [(s, x) for (s, x) in data.PX.index if x in set(train) and isinstance(data.py[(s, x)], str)]
        model, params, _ = mm.tune(data.PX.loc[keep].to_numpy(), [data.py[k] for k in keep],
                                   ml.FAMILY[ml.ML_POOLED_VERSION])
        row["P"] = mm.probabilities(model, data.X["pooled"].loc[[d]].to_numpy())[0]
        row["P_params"] = params
        out[d] = {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in row.items()}
    return out


def _regularisation_path(pre: dd.Preopen) -> Dict[str, Any]:
    """N's and M's logistic family refitted per development fold at every C of PATH_C (the frozen grid is 0.01..0.3):
    the frozen tuning split's validation Brier (training rows only) and the test sessions' predictions - scored
    later, as a post-hoc diagnostic of the shrinkage, never a selection."""
    from forecaster import ml_eval
    from forecaster import ml_model as mm
    data = pre.data
    out: Dict[str, Any] = {}
    for cfg in ("nq_only", "multi"):
        per_c: Dict[float, Dict[str, Any]] = {c: {"tuning_brier": [], "preds": {}, "coef_norm": []} for c in PATH_C}
        for train_end, test_start, test_end in ml_eval.folds(len(data.days)):
            train = [d for d in data.days[:train_end] if data.y[d] is not None]
            test = data.days[test_start:test_end]
            X, y = data.X[cfg].loc[train].to_numpy(), np.array([data.y[d] for d in train])
            cut = int(round(len(y) * (1 - ml.TUNING["validation_share"])))
            for c in PATH_C:
                fit = mm.pipeline("logit", {"C": c}).fit(X[:cut], y[:cut])
                per_c[c]["tuning_brier"].append(float(mm.brier(mm.probabilities(fit, X[cut:]), y[cut:]).mean()))
                full = mm.pipeline("logit", {"C": c}).fit(X, y)
                per_c[c]["coef_norm"].append(float(np.abs(full.named_steps["model"].coef_).max()))
                for d, p in zip(test, mm.probabilities(full, data.X[cfg].loc[test].to_numpy())):
                    per_c[c]["preds"][d] = p
        out[cfg] = per_c
    return out


def _availability(conn, pre: dd.Preopen) -> Dict[str, Any]:
    """Every pool session's multi-instrument features rebuilt as they stood at its replay deadline (the cutoff + 35
    minutes, contracts/nq_ml.replay_deadline): the instruments' statuses then, and whether the row equals the
    development row (built from the complete history, receipts ignored)."""
    from forecaster import ml_features as mf
    data = pre.data
    snaps = {str(s["session_date"]): s for s in data.snaps}
    status: Dict[str, Dict[str, int]] = {}
    same, differs, live_days = 0, [], []
    for d in data.days:
        deadline = ml.replay_deadline(mf._utc(snaps[d]["cutoff_at"]))
        fs = mf.build(conn, [d], {d: snaps[d]}, as_of=deadline, loaded=data.loaded)
        for sym, v in fs.instruments[d].items():
            status.setdefault(sym, {}).setdefault(v["status"] or "none", 0)
            status[sym][v["status"] or "none"] += 1
        a = mf.matrix(fs, [d], "multi").to_numpy(float)[0]
        b = data.X["multi"].loc[d].to_numpy(float)
        if np.array_equal(np.isnan(a), np.isnan(b)) and np.allclose(np.nan_to_num(a), np.nan_to_num(b), atol=1e-12):
            same += 1
        else:
            differs.append(d)
        if fs.instruments[d]["NQ"]["status"] == "observed":
            live_days.append(d)
    dev_status: Dict[str, Dict[str, int]] = {}
    for d in data.days:
        for sym, v in data.fs.instruments[d].items():
            dev_status.setdefault(sym, {}).setdefault(v["status"] or "none", 0)
            dev_status[sym][v["status"] or "none"] += 1
    ages = {sym: [v["age_min"] for d in data.days for s2, v in data.fs.instruments[d].items()
                  if s2 == sym and v["age_min"] is not None] for sym in data.fs.instruments[data.days[0]]}
    return {"at_deadline": status, "development": dev_status, "rows_equal_at_deadline": same,
            "rows_differ": differs, "live_nq_days": live_days,
            "age_minutes": {k: {"median": float(np.median(v)), "max": float(np.max(v))} for k, v in ages.items() if v}}


def _pooled_checks(pre: dd.Preopen) -> Dict[str, Any]:
    """The pooled candidate's split by session date: in every development fold the training rows of every
    instrument come from training dates only - none from the embargoed or test sessions; and how often the three
    instruments' labels agree on one date (the effective sample grows far less than threefold)."""
    from forecaster import ml_eval
    data = pre.data
    folds = []
    for train_end, test_start, test_end in ml_eval.folds(len(data.days)):
        train = {d for d in data.days[:train_end] if data.y[d] is not None}
        keep = [(s, d) for (s, d) in data.PX.index if d in train and isinstance(data.py[(s, d)], str)]
        dates = {d for _, d in keep}
        banned = set(data.days[train_end:test_end])
        folds.append({"train_rows": len(keep), "dates_outside_training": len(dates - train),
                      "rows_on_test_or_embargo_dates": sum(1 for _, d in keep if d in banned)})
    agree, n = {"ES": 0, "RTY": 0}, {"ES": 0, "RTY": 0}
    for d in data.days:
        a = data.py.get(("NQ", d))
        for s in ("ES", "RTY"):
            b = data.py.get((s, d))
            if isinstance(a, str) and isinstance(b, str):
                n[s] += 1
                agree[s] += int(a == b)
    return {"folds": folds, "label_agreement_with_nq": {s: agree[s] / n[s] for s in n if n[s]}, "pairs": n}


def _production_runs(conn) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT algorithm_version, mode, lifecycle_status, coalesce(failure_reason, ''), count(*) "
        "FROM journal.forecast_runs WHERE algorithm_version = ANY(%s) GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3;",
        (list(ml.ALGORITHMS) + [ml.fc.BASELINE_VERSION, ml.fc.PRIOR_VERSION],)).fetchall()
    deliveries = conn.execute(
        "SELECT coalesce(algorithm, 'none'), count(*) FROM journal.forecast_deliveries GROUP BY 1;").fetchall()
    return {"runs": [{"algorithm": r[0], "mode": r[1], "status": r[2], "reason": r[3][:120], "n": r[4]}
                     for r in rows], "deliveries": {r[0]: r[1] for r in deliveries}}


def predict_preopen(conn, jobs: int) -> Dict[str, Any]:
    t0 = time.time()
    pre = dd.preopen(conn, jobs)
    problem = pre.problem
    folds = st.outer_folds(len(problem.sessions))
    for f in folds:                       # every training session ends (09:45) before the first test session starts
        st.check_fold(f, problem.sessions, problem.sessions)
    run = st.run_problem(problem, folds, jobs=min(jobs, len(folds)))
    preds = run["preds"]
    for arm, key in FROZEN.items():
        preds[arm] = {i: pre.frozen[key][d] for i, d in enumerate(problem.sessions) if d in pre.frozen[key]}
    for arm in ("A", "B"):
        preds[arm] = {i: pre.stored[arm][d] for i, d in enumerate(problem.sessions)
                      if d in pre.stored[arm] and i >= folds[0].test[0]}
    for key in ("nq_only/gbm", "multi/gbm", "pooled/logit"):       # the development comparison's other fits
        preds[f"dev:{key}"] = {i: pre.frozen[key][d] for i, d in enumerate(problem.sessions) if d in pre.frozen[key]}
    week = _week(pre)
    path = _regularisation_path(pre)
    availability = _availability(conn, pre)
    pooled = _pooled_checks(pre)
    production = _production_runs(conn)
    frame = _pred_frame(problem, preds)
    os.makedirs(REPORT_DIR, exist_ok=True)
    pfile = os.path.join(REPORT_DIR, "predictions_preopen.csv")
    frame.to_csv(pfile, index=False, float_format="%.10g")
    with open(os.path.join(REPORT_DIR, "trials_preopen.json"), "w", encoding="utf-8") as f:
        json.dump(run["folds"], f, indent=1, default=str)
    _save("preopen.pkl", {"pre": pre, "run": run, "week": week, "path": path, "folds": folds,
                          "availability": availability, "pooled": pooled, "production": production})
    _export_tabpfn(problem, folds, "preopen", st.PREOPEN_FEATURES["nq"])
    entry = {"seconds": round(time.time() - t0, 1), "sessions": len(problem.sessions),
             "labelled": int((problem.y >= 0).sum()), "folds": len(folds),
             "test": [problem.sessions[folds[0].test[0]], problem.sessions[folds[-1].test[-1]]],
             "files": {"predictions_preopen.csv": _sha(pfile)}, "software": _software(),
             "trials": _count_trials(run), "frozen_folds": pre.frozen_log, "pooled_split": pooled["folds"]}
    _record("predict_preopen", entry)
    return entry


def _count_trials(run: Dict[str, Any]) -> Dict[str, Any]:
    """Configurations per family (each fold tries the whole grid) and the fits that took."""
    per: Dict[str, int] = {}
    for f in run["folds"]:
        for fam, trials in f["trials"].items():
            per[fam] = len(trials)
    return {"configurations": per, "folds": len(run["folds"]),
            "inner_fits": {fam: n * 3 * len(run["folds"]) for fam, n in per.items() if fam != "BL"}}


def _export_tabpfn(problem: st.Problem, folds: Sequence[st.Fold], name: str, cols: Sequence[str]) -> None:
    os.makedirs(OUT, exist_ok=True)
    np.savez(os.path.join(OUT, f"tabpfn_input_{name}.npz"), X=problem.X[list(cols)].to_numpy(float),
             y=problem.y, row_session=problem.row_session, columns=np.array(list(cols)),
             folds_train=np.array([np.array(f.train) for f in folds], dtype=object),
             folds_test=np.array([np.array(f.test) for f in folds], dtype=object), allow_pickle=True)


# --------------------------------------------------------------------------
# predict: the RTH task
# --------------------------------------------------------------------------

SCALE_FEATURES = ["rv_30", "rv_open", "rel_vol", "range_sofar", "elapsed", "ev_ahead", "ev_recent"]
RIDGE_ALPHAS = (1.0, 10.0, 100.0, 1000.0, 10000.0)


def _ols(X: np.ndarray, t: np.ndarray, alpha: float = 0.0, med=None, mu=None, sd=None):
    Xs, med, mu, sd = st._prep(X, med, mu, sd)
    A = np.column_stack([np.ones(len(Xs)), Xs])
    reg = alpha * np.eye(A.shape[1])
    reg[0, 0] = 0.0
    coef = np.linalg.solve(A.T @ A + reg, A.T @ t)
    return {"coef": coef, "med": med, "mu": mu, "sd": sd}


def _ols_predict(model, X: np.ndarray) -> np.ndarray:
    Xs, *_ = st._prep(X, model["med"], model["mu"], model["sd"])
    return np.column_stack([np.ones(len(Xs)), Xs]) @ model["coef"]


def _distribution(problem: st.Problem, rows: pd.DataFrame, folds: Sequence[st.Fold]) -> pd.DataFrame:
    """Per outer test row a location/scale forecast of z = move / (sigma_1m sqrt(h)): the scale from a regression
    of log(z^2 + 0.01) on SCALE_FEATURES, calibrated so the training rows' mean z^2 / s^2 is 1 (LS0: centred at 0);
    the location from a ridge regression of z on the compact NQ features, its penalty chosen on the inner folds by
    squared error (LSmu). Training rows only, per fold."""
    z = rows["z"].to_numpy(float)
    ok = (problem.y >= 0) & np.isfinite(z)
    Xs_ = rows[SCALE_FEATURES].to_numpy(float)
    Xl_ = rows[st.RTH_FEATURES["nq"]].to_numpy(float)
    out = []
    for f in folds:
        tr = np.flatnonzero(np.isin(problem.row_session, f.train) & ok)
        te = np.flatnonzero(np.isin(problem.row_session, f.test))
        sc = _ols(Xs_[tr], np.log(z[tr] ** 2 + 0.01))
        cal = float(np.mean(z[tr] ** 2 / np.exp(_ols_predict(sc, Xs_[tr]))))
        sigma = np.sqrt(cal * np.exp(_ols_predict(sc, Xs_[te])))
        scores = {}
        for a in RIDGE_ALPHAS:
            err = []
            for g in st.inner_folds(f.train):
                itr = np.flatnonzero(np.isin(problem.row_session, g.train) & ok)
                iva = np.flatnonzero(np.isin(problem.row_session, g.test) & ok)
                m = _ols(Xl_[itr], z[itr], a)
                err.extend(((_ols_predict(m, Xl_[iva]) - z[iva]) ** 2).tolist())
            scores[a] = float(np.mean(err))
        alpha = min(RIDGE_ALPHAS, key=lambda a: (round(scores[a], 12), a))
        loc = _ols_predict(_ols(Xl_[tr], z[tr], alpha), Xl_[te])
        for i, r in enumerate(te):
            out.append({"problem": problem.name, "row": int(r), "session": problem.sessions[problem.row_session[r]],
                        "cutoff": int(rows.loc[r, "cutoff"]), "sigma": float(sigma[i]), "mu": float(loc[i]),
                        "alpha": alpha, "scale_calibration": cal})
    return pd.DataFrame(out)


def predict_rth(conn, jobs: int) -> Dict[str, Any]:
    t0 = time.time()
    sessions = dd.rth_sessions(conn)
    features, labels = dd.rth_table(conn, sessions)
    out_preds, trials, problems, dist = [], {}, {}, []
    entry: Dict[str, Any] = {"sessions": len(sessions), "span": [sessions[0].day, sessions[-1].day],
                             "feature_rows": len(features), "label_rows": len(labels),
                             "label_reasons": labels["reason"].value_counts().to_dict(), "problems": {}}
    for h in st.RTH_HORIZONS:
        for origin in st.RTH_ORIGINS:
            problem, rows = dd.rth_problem(features, labels, h, origin)
            folds = st.outer_folds(len(problem.sessions))
            for f in folds:
                st.check_fold(f, problem.sessions, problem.sessions)
            run = st.run_problem(problem, folds, jobs=min(jobs, len(folds)))
            out_preds.append(_pred_frame(problem, run["preds"], rows))
            trials[problem.name] = run["folds"]
            problems[problem.name] = {"problem": problem, "rows": rows, "folds": folds, "run": run}
            entry["problems"][problem.name] = {"rows": len(rows), "labelled": int((problem.y >= 0).sum()),
                                               "folds": len(folds), "trials": _count_trials(run),
                                               "test": [problem.sessions[folds[0].test[0]],
                                                        problem.sessions[folds[-1].test[-1]]]}
            if h == 15:
                _export_tabpfn(problem, folds, f"rth_h15_{origin}", st.RTH_FEATURES["nq"])
                dist.append(_distribution(problem, rows, folds))
            print(f"{problem.name}: {len(folds)} folds, {time.time() - t0:.0f} s", flush=True)
    frame = pd.concat(out_preds, ignore_index=True)
    pfile = os.path.join(OUT, "predictions_rth.csv.gz")
    frame.to_csv(pfile, index=False, float_format="%.10g", compression={"method": "gzip", "mtime": 0})
    primary = frame[frame["problem"].str.startswith("rth/h15/")]
    pfile15 = os.path.join(REPORT_DIR, "predictions_rth_h15.csv.gz")
    primary.to_csv(pfile15, index=False, float_format="%.10g", compression={"method": "gzip", "mtime": 0})
    dfile = os.path.join(OUT, "distribution_rth_h15.csv.gz")
    pd.concat(dist, ignore_index=True).to_csv(dfile, index=False, float_format="%.10g",
                                              compression={"method": "gzip", "mtime": 0})
    with gzip.open(os.path.join(REPORT_DIR, "trials_rth.json.gz"), "wt", encoding="utf-8") as f:
        json.dump(trials, f, default=str)
    _save("rth.pkl", {"features": features, "labels": labels, "problems": problems,
                      "sessions": [s.day for s in sessions]})
    entry.update(seconds=round(time.time() - t0, 1), software=_software(),
                 files={"predictions_rth.csv.gz (data/research)": _sha(pfile),
                        "predictions_rth_h15.csv.gz": _sha(pfile15),
                        "distribution_rth_h15.csv.gz (data/research)": _sha(dfile)})
    _record("predict_rth", entry)
    return entry


# --------------------------------------------------------------------------
# analogues: B_rth and B_rth_recent at the RTH test sessions
# --------------------------------------------------------------------------

def _analogue_session(target, pool, cutoffs_windows):
    out = {}
    for m, windows in cutoffs_windows:
        out[m] = dd.analogue_forecast(target, pool, m, windows)
    return out


def analogues(conn, jobs: int) -> Dict[str, Any]:
    from joblib import Parallel, delayed
    t0 = time.time()
    rt = _load("rth.pkl")
    labels = rt["labels"]
    test_days = sorted({s for name, p in rt["problems"].items() for f in p["folds"]
                        for i in f.test for s in [p["problem"].sessions[i]]})
    ops = dd.openings(conn, max(test_days), os.path.join(OUT, "rth_cache"))
    work = []
    for day in test_days:
        target = ops.get(day)
        if target is None or target.context is None or not target.context.atr:
            continue
        pool = [o for d, o in ops.items() if d < day]
        lab = labels[labels["session"] == day]
        cw = []
        for m in st.RTH_CUTOFFS:
            sub = lab[lab["cutoff"] == m]
            if sub.empty or target.minutes < m:
                continue
            windows = [(int(r.S), int(r.h), float(r.band) / target.context.atr) for r in sub.itertuples()
                       if r.reason == "ok"]
            if windows:
                cw.append((m, windows))
        work.append((day, target, pool, cw))
    done = Parallel(n_jobs=jobs)(delayed(_analogue_session)(t, p, cw) for _, t, p, cw in work)
    rows = []
    for (day, _, _, _), res in zip(work, done):
        for m, r in res.items():
            for (S, h), w in r["windows"].items():
                origin = next(o for o, off in st.RTH_ORIGINS.items() if m + off == S)
                for arm in ("B_rth", "B_rth_recent"):
                    if w[arm] is not None:
                        rows.append({"problem": f"rth/h{h}/{origin}", "session": day, "cutoff": m, "arm": arm,
                                     "members": w[f"{arm}_n"], "pool": r["pool_size"],
                                     **{f"p_{c}": w[arm][j] for j, c in enumerate(st.CLASSES)}})
    frame = pd.DataFrame(rows)
    pfile = os.path.join(OUT, "predictions_rth_analogues.csv.gz")
    frame.to_csv(pfile, index=False, float_format="%.10g", compression={"method": "gzip", "mtime": 0})
    entry = {"seconds": round(time.time() - t0, 1), "sessions": len(work), "rows": len(frame),
             "files": {"predictions_rth_analogues.csv.gz (data/research)": _sha(pfile)}}
    _record("analogues", entry)
    return entry


# --------------------------------------------------------------------------
# controls
# --------------------------------------------------------------------------

def _frozen_on(pre: dd.Preopen, y_map: Dict[str, Optional[str]], py_map: Dict[Tuple[str, str], Optional[str]]
               ) -> Dict[str, Dict[str, np.ndarray]]:
    """N, M and P's walk-forward refits (forecaster/ml_eval._fold) under other labels."""
    from forecaster import ml_eval
    data = pre.data
    py = pd.Series([py_map[k] for k in data.PX.index], index=data.PX.index, dtype=object)
    out: Dict[str, Dict[str, np.ndarray]] = {}
    for f in ml_eval.folds(len(data.days)):
        preds, _ = ml_eval._fold(data.X, data.PX, y_map, py, set(pre.abstain), data.days, *f)
        for k, v in preds.items():
            out.setdefault(k, {}).update(v)
    return out


def _skill(problem: st.Problem, preds: Dict[str, Dict[int, np.ndarray]], arms: Sequence[str],
           ref: str = "PRIOR") -> Dict[str, Any]:
    """Each arm's mean per-session Brier difference from ``ref`` on the labelled test rows, with its interval; and
    from the symmetrised prior (the prior's bullish and bearish shares replaced by their mean: no direction view)."""
    out = {}
    rows = sorted(r for r in preds[ref] if problem.y[r] >= 0)
    sym = {r: np.array([(p[0] + p[1]) / 2, (p[0] + p[1]) / 2, p[2]]) for r, p in preds[ref].items()}
    for arm in arms:
        common = [r for r in rows if r in preds[arm]]
        if not common:
            continue
        y = problem.y[common]
        sess = [problem.sessions[problem.row_session[r]] for r in common]
        b_arm = st.session_means(st.brier(np.array([preds[arm][r] for r in common]), y), sess)
        res = st.paired(b_arm, st.session_means(st.brier(np.array([preds[ref][r] for r in common]), y), sess))
        res_s = st.paired(b_arm, st.session_means(st.brier(np.array([sym[r] for r in common]), y), sess))
        out[arm] = {"mean": res["mean"], "interval": res["interval"], "n": res["n"],
                    "vs_sym": {"mean": res_s["mean"], "interval": res_s["interval"]}}
    return out


def _control_preopen(args) -> Dict[str, Any]:
    kind, seed, beta = args
    pre = _load("preopen.pkl")["pre"]
    problem = pre.problem
    folds = st.outer_folds(len(problem.sessions))
    data = pre.data
    if kind == "shuffled":
        prob = st.shuffled(problem, seed)
        oracle = None
    else:
        prob, P = st.synthetic(problem, ("nq_ret_on", "nq_range_pos"), beta, seed)
        oracle = st.oracle_gain(prob, P)
    run = st.run_problem(prob, folds, jobs=1)
    preds = run["preds"]
    # the frozen arms under the same labels: NQ's by session; the pooled rows' other instruments by the same session
    # mapping (shuffled: the labels of the session the NQ label came from; synthetic: unchanged other instruments)
    lab = {d: (st.CLASSES[v] if v >= 0 else None) for d, v in zip(prob.sessions, prob.y)}
    if kind == "shuffled":
        perm = np.random.default_rng(seed).permutation(len(problem.sessions))
        src = {d: problem.sessions[perm[i]] for i, d in enumerate(problem.sessions)}
        py_map = {(s, d): (lab[d] if s == "NQ" else data.py.get((s, src[d]))) for (s, d) in data.PX.index}
    else:
        py_map = {(s, d): (lab[d] if s == "NQ" else data.py[(s, d)]) for (s, d) in data.PX.index}
    frozen = _frozen_on(pre, lab, py_map)
    for arm, key in FROZEN.items():
        preds[arm] = {i: frozen[key][d] for i, d in enumerate(prob.sessions) if d in frozen[key]}
    res = {"kind": kind, "seed": seed, "beta": beta, "oracle_gain": oracle,
           "skill_vs_prior": _skill(prob, preds, ["CP", "LR", "GB", "RES", "BL", "N", "M", "P"])}
    if oracle is not None:
        test = sorted(r for r in preds["PRIOR"] if prob.y[r] >= 0)
        res["oracle_vs_prior"] = float(np.mean(st.brier(P[test], prob.y[test])
                                               - st.brier(np.array([preds["PRIOR"][r] for r in test]), prob.y[test])))
    return res


def _control_rth(args) -> Dict[str, Any]:
    kind, seed, beta = args
    rt = _load("rth.pkl")
    p = rt["problems"]["rth/h15/cutoff"]
    problem, folds = p["problem"], p["folds"]
    if kind == "shuffled":
        prob, oracle = st.shuffled(problem, seed), None
    else:
        prob, P = st.synthetic(problem, ("ret_30", "range_pos"), beta, seed)
        oracle = st.oracle_gain(prob, P)
    run = st.run_problem(prob, folds, jobs=1)
    return {"kind": kind, "seed": seed, "beta": beta, "oracle_gain": oracle,
            "skill_vs_prior": _skill(prob, run["preds"], ["CP", "LR", "GB", "RES", "BL"])}


def _invariance(conn) -> Dict[str, Any]:
    """Future bars cannot change the past: (1) the pre-open features of every session before a boundary rebuilt
    from bars truncated at the boundary equal the full-history ones; (2) an RTH row's features at its cutoff
    are unchanged when every later bar of its session is altered; (3) the analogue ranking at a cutoff is
    unchanged when the target's later bars are altered."""
    from dataclasses import replace
    from forecaster import ml_features as mf
    pre = _load("preopen.pkl")["pre"]
    data = pre.data
    out: Dict[str, Any] = {}
    checks = []
    for boundary in (data.days[150], data.days[230]):
        loaded = {s: mf.InstrumentBars(s, {k: v for k, v in ib.days.items() if k[1] < boundary},
                                       {d: c for d, c in ib.active.items() if d < boundary})
                  for s, ib in data.loaded.items()}
        days = [d for d in data.days if d < boundary]
        fs = mf.build(conn, days, {str(s["session_date"]): s for s in data.snaps if str(s["session_date"]) < boundary},
                      loaded=loaded)
        a = mf.matrix(fs, days, "multi").to_numpy(float)
        b = data.X["multi"].loc[days].to_numpy(float)
        same = np.array_equal(np.isnan(a), np.isnan(b)) and np.allclose(np.nan_to_num(a), np.nan_to_num(b),
                                                                          rtol=0, atol=0)
        checks.append({"boundary": boundary, "sessions": len(days), "identical": bool(same),
                       "max_abs_diff": float(np.nanmax(np.abs(a - b))) if a.size else 0.0})
    out["preopen_truncation"] = checks
    rt = _load("rth.pkl")
    sessions = {s.day: s for s in dd.rth_sessions(conn)}
    events = dd._events(conn)
    rng = np.random.default_rng(st.SEED)
    rth_checks, n_bad = 0, 0
    for day in rng.choice(sorted(sessions), size=12, replace=False):
        sd = sessions[day]
        for c in st.RTH_CUTOFFS:
            if c >= sd.L:
                continue
            orig = replace(sd, buckets=dd.buckets_from_minutes(sd.bars["NQ"], sd.session.rth_open_at))
            before = dd.features_at(orig, c, events)
            bars = {}
            for sym, mn in sd.bars.items():
                cut = dd.PRE + c
                noise = 1 + rng.normal(0, 0.01, size=len(mn.c) - cut)
                bars[sym] = dd.Minutes(mn.cid, mn.o.copy(), mn.h.copy(), mn.l.copy(), mn.c.copy(), mn.v.copy())
                for arr in (bars[sym].o, bars[sym].h, bars[sym].l, bars[sym].c):
                    arr[cut:] = arr[cut:] * noise
                bars[sym].v[cut:] = bars[sym].v[cut:] * 3
            altered = replace(sd, bars=bars,
                              buckets=dd.buckets_from_minutes(bars["NQ"], sd.session.rth_open_at))
            after = dd.features_at(altered, c, events)
            rth_checks += 1
            diff = [k for k in before if not (before[k] == after[k] or (isinstance(before[k], float)
                                                                        and np.isnan(before[k])
                                                                        and np.isnan(after[k])))]
            if diff:
                n_bad += 1
    out["rth_features"] = {"rows": rth_checks, "changed": n_bad}
    # the analogue ranking: the target's bars after the cutoff altered
    ops = dd.openings(conn, rt["sessions"][-1], os.path.join(OUT, "rth_cache"))
    from matching import rth as mr
    rank_checks, rank_bad = 0, 0
    for day in rng.choice([d for d in sorted(ops) if d >= "2026-03-01" and ops[d].context], size=4, replace=False):
        t = ops[day]
        pool = [o for d, o in ops.items() if d < day]
        for m in (60, 180, 330):
            if t.minutes <= m:
                continue
            bars = list(t.bars)
            bars[m:] = [(b[0], b[1] * 1.01, b[2] * 1.01, b[3] * 1.01, b[4] * 1.01, b[5] * 2) for b in bars[m:]]
            a = mr.rank(replace(t, bars=t.bars[:m]), pool, m)
            b = mr.rank(replace(replace(t, bars=tuple(bars)), bars=tuple(bars)[:m]), pool, m)
            rank_checks += 1
            same = [(r["opening"].session_date, r["similarity"]) for r in a["ordered"]] == \
                   [(r["opening"].session_date, r["similarity"]) for r in b["ordered"]]
            rank_bad += int(not same)
    out["rth_ranks"] = {"rankings": rank_checks, "changed": rank_bad}
    return out


def controls(conn, jobs: int, shuffles: int = 20, synth_seeds: int = 10, rth_shuffles: int = 5,
             rth_synth: int = 3) -> Dict[str, Any]:
    from joblib import Parallel, delayed
    t0 = time.time()
    pre_problem = _load("preopen.pkl")["pre"].problem
    rth_problem = _load("rth.pkl")["problems"]["rth/h15/cutoff"]["problem"]
    betas_pre = [st.beta_for(pre_problem, ("nq_ret_on", "nq_range_pos"), g) for g in st.SYNTHETIC_GAINS]
    betas_rth = [st.beta_for(rth_problem, ("ret_30", "range_pos"), g) for g in st.SYNTHETIC_GAINS]
    tasks = ([("shuffled", s, None) for s in range(shuffles)] +
             [("synthetic", s, b) for b in betas_pre for s in range(synth_seeds)])
    pre_res = Parallel(n_jobs=jobs)(delayed(_control_preopen)(t) for t in tasks)
    tasks_rth = ([("shuffled", s, None) for s in range(rth_shuffles)] +
                 [("synthetic", s, b) for b in betas_rth for s in range(rth_synth)])
    rth_res = Parallel(n_jobs=jobs)(delayed(_control_rth)(t) for t in tasks_rth)
    inv = _invariance(conn)
    res = {"preopen": pre_res, "rth": rth_res, "invariance": inv}
    _save("controls.pkl", res)
    with open(os.path.join(REPORT_DIR, "controls.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=str)
    _record("controls", {"seconds": round(time.time() - t0, 1), "preopen_runs": len(pre_res),
                         "rth_runs": len(rth_res), "invariance": inv})
    return res
