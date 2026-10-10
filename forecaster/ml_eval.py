# forecaster/ml_eval.py
"""
The development comparison of the NQ direction model (contracts/nq_ml.DEV_EVALUATION and
POOLED): historical frequencies A, deterministic analogues B, the NQ-only and the
multi-instrument models - each with the logistic and the boosted estimator - and the
pooled candidate, on identical NQ targets and opportunities.

    python scripts/nq_journal.py ml-dev-eval      # docs/reports/ml_development.md

  data       the research_0929 pool's sessions (their historical snapshots), direction_15m
             as labelled (the latest outcome revision), the features as of each cutoff
             (forecaster/ml_features.py)
  folds      chronological walk-forward by session date (forecaster/ml_split.py): train on
             every earlier session (expanding), skip the embargo session(s), test the next
             block - preprocessing, tuning and the fit inside each training window only; the
             tuning's inner split is by session date too, with the embargo, and the pooled
             candidate is tuned on NQ's validation rows (contracts/nq_ml.SPLIT; split=
             'legacy_rows' reproduces the v1 row-position cut, docs/reports/ml_pooled_split_v1.md);
             the folds are independent, so they run in parallel worker processes (``jobs``; the
             results are the same as one by one)
  A, B       their stored historical-replay runs of the same snapshots (first issued)
  scores     per session the unhalved multiclass Brier score (primary) and log loss;
             paired differences on the sessions all compared arms forecast and a label
             exists, with a moving-block bootstrap interval; calibration (reliability bins,
             expected calibration error); availability over every scheduled test session;
             inference latency
  pooled     NQ, ES and RTY rows with their own features, labels and identity; split by
             session date; scored on NQ's test rows against the NQ-trained fit

Development data only: every session here was inspected before. A result is a
candidate for the forward evaluation (contracts/nq_ml.FORWARD), not a finding.
"""

from __future__ import annotations

import math
import os
import time
from datetime import timedelta
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from contracts import nq_forecast as fc
from contracts import nq_ml as ml
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from forecaster import ml_features as mf
from forecaster import ml_model as mm
from forecaster import ml_split as sp
from forecaster.experiments import block_bootstrap

ARMS_A_B = {"A": fc.PRIOR_VERSION, "B": fc.BASELINE_VERSION}
UNC = {"block": 5, "resamples": 2000, "seed": 20261009, "interval": 0.95}
BINS = 10


def stored_arm(conn, snaps: Sequence[Dict[str, Any]], algorithm: str) -> Dict[str, np.ndarray]:
    """day -> the class probabilities of the algorithm's first issued historical-replay run of that snapshot."""
    import json
    by_snap = {s["snapshot_id"]: str(s["session_date"]) for s in snaps}
    out = {}
    rows = conn.execute(
        "SELECT r.snapshot_id, p.distribution FROM journal.forecast_runs r JOIN journal.forecast_predictions p "
        "USING (run_id) WHERE r.algorithm_version = %s AND r.mode = 'historical_replay' AND r.lifecycle_status = "
        "'issued' AND p.target = %s AND p.distribution IS NOT NULL ORDER BY r.issued_at;",
        (algorithm, ml.TARGET)).fetchall()
    for sid, dist in rows:
        day = by_snap.get(str(sid))
        if day is None or day in out:
            continue
        d = dist if isinstance(dist, dict) else json.loads(dist)
        out[day] = np.array([float(Fraction(d[c])) for c in ml.CLASSES])
    return out


def date_folds(days: Sequence[str]) -> List["sp.DateFold"]:
    """The development comparison's outer folds over the pool's session dates (forecaster/ml_split.outer_folds)."""
    d = ml.DEV_EVALUATION
    return sp.outer_folds(days, d["initial_train_sessions"], d["test_block_sessions"], d["embargo_sessions"])


def folds(n: int) -> List[Tuple[int, int, int]]:
    """``(train_end, test_start, test_end)`` index triples over ``n`` date-ordered sessions - the date folds as
    positions: train on [0, train_end), test [test_start, test_end)."""
    days = [f"{i:08d}" for i in range(n)]                  # n distinct, sortable dates stand for the sessions
    return [(len(f.train), len(f.train) + len(f.embargo), len(f.train) + len(f.embargo) + len(f.test))
            for f in date_folds(days)]


def _scores(p: np.ndarray, y: str) -> Tuple[float, float]:
    onehot = np.array([1.0 if c == y else 0.0 for c in ml.CLASSES])
    brier = float(((p - onehot) ** 2).sum())
    py = float(p[ml.CLASSES.index(y)])
    return brier, (math.inf if py <= 0 else -math.log(py))


def paired(a: Dict[str, Tuple[float, float]], b: Dict[str, Tuple[float, float]], days: Sequence[str]) -> Dict[str, Any]:
    """``a`` minus ``b`` on ``days`` (date order): mean Brier and log-loss differences with bootstrap intervals."""
    db = [a[d][0] - b[d][0] for d in days]
    fin = [d for d in days if math.isfinite(a[d][1]) and math.isfinite(b[d][1])]
    dl = [a[d][1] - b[d][1] for d in fin]
    boot = lambda xs: block_bootstrap(xs, UNC["block"], UNC["resamples"], UNC["seed"], UNC["interval"])  # noqa
    return {"n": len(days), "brier_diff": float(np.mean(db)) if db else None, "brier_interval": boot(db),
            "logloss_diff": float(np.mean(dl)) if dl else None, "logloss_interval": boot(dl),
            "logloss_n": len(fin)}


def calibration(preds: Dict[str, np.ndarray], y: Dict[str, str], days: Sequence[str]) -> Dict[str, Any]:
    """Per class, the reliability bins (mean predicted probability against the observed frequency) and the expected
    calibration error over the classes."""
    out, eces = {}, []
    for j, c in enumerate(ml.CLASSES):
        p = np.array([preds[d][j] for d in days])
        o = np.array([1.0 if y[d] == c else 0.0 for d in days])
        edges = np.linspace(0, 1, BINS + 1)
        idx = np.clip(np.digitize(p, edges) - 1, 0, BINS - 1)
        bins, ece = [], 0.0
        for b in range(BINS):
            m = idx == b
            if m.any():
                bins.append({"bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}", "n": int(m.sum()),
                             "predicted": float(p[m].mean()), "observed": float(o[m].mean())})
                ece += m.sum() / len(p) * abs(p[m].mean() - o[m].mean())
        out[c] = {"bins": bins, "ece": float(ece)}
        eces.append(ece)
    return {"classes": out, "ece_mean": float(np.mean(eces)) if eces else None}


ML_ARMS = [(cfg, fam) for cfg in ("nq_only", "multi") for fam in ("logit", "gbm")]


def _fold(X, PX, y, py_, abstain, days, train_end: int, test_start: int, test_end: int, split: str = "dates"):
    """One walk-forward fold - in a worker process: the four NQ-trained fits and the two pooled ones, tuned and fitted
    on the training sessions only, and their probabilities for the test sessions; ``(predictions, log entry)``.
    ``split``: the tuner's inner split - 'dates' (contracts/nq_ml.SPLIT) or 'legacy_rows' (the v1 reproduction)."""
    fold = sp.DateFold(tuple(days[:train_end]), tuple(days[train_end:test_start]), tuple(days[test_start:test_end]))
    sp.check(fold)
    train_days = [d for d in fold.train if y[d] is not None]
    test_days = list(fold.test)
    entry = {"train": [train_days[0], train_days[-1], len(train_days)], "test": [test_days[0], test_days[-1]],
             "chosen": {}, "split": split}
    preds: Dict[str, Dict[str, np.ndarray]] = {}
    for cfg, fam in ML_ARMS:
        model, params, _ = mm.tune(X[cfg].loc[train_days].to_numpy(), [y[d] for d in train_days], fam,
                                   dates=train_days, split=split)
        avail = [d for d in test_days if cfg == "nq_only" or d not in abstain]
        preds[f"{cfg}/{fam}"] = ({} if not avail else
                                 dict(zip(avail, mm.probabilities(model, X[cfg].loc[avail].to_numpy()))))
        entry["chosen"][f"{cfg}/{fam}"] = params
    # pooled: every instrument's rows of the training dates (labels present), NQ's test rows; tuned on NQ's rows of
    # the inner validation dates (the date split) - every instrument's rows of a date on one side
    train_set = set(train_days)
    keep = [(s, d) for (s, d) in PX.index if d in train_set and isinstance(py_[(s, d)], str)]
    for fam in ("logit", "gbm"):
        model, params, _ = mm.tune(PX.loc[keep].to_numpy(), [py_[k] for k in keep], fam, dates=[d for _, d in keep],
                                   instruments=[s for s, _ in keep], split=split)
        preds[f"pooled/{fam}"] = dict(zip(test_days, mm.probabilities(model, X["pooled"].loc[test_days].to_numpy())))
        entry["chosen"][f"pooled/{fam}"] = params
    entry["pooled_training_rows"] = len(keep)
    if split == "dates":
        inner = sp.inner_split([d for _, d in keep], ml.TUNING["validation_share"], ml.SPLIT["embargo_sessions"])
        entry["inner"] = inner.describe()
    return preds, entry


def run(conn, profile: str = ml.PROFILE, progress=print, data=None, jobs: Optional[int] = None,
        split: str = "dates") -> Dict[str, Any]:
    """The development comparison (see the module docstring); returns the results. ``jobs``: worker processes for
    the folds (default one per fold, at most one per CPU; 1 runs them here, one by one). ``split``: the tuner's inner
    split ('legacy_rows' reproduces docs/reports/ml_development.md of 2026-10-09)."""
    from joblib import Parallel, delayed
    from forecaster.ml_train import dataset
    t0 = time.time()
    data = data or dataset(conn, profile)
    snaps, days, y, fs, X, PX, py_ = data.snaps, data.days, data.y, data.fs, data.X, data.PX, data.py
    progress(f"features of {len(days)} sessions in {time.time() - t0:.1f} s")
    stored = {arm: stored_arm(conn, snaps, alg) for arm, alg in ARMS_A_B.items()}
    preds: Dict[str, Dict[str, np.ndarray]] = {a: {} for a in ("A", "B")}
    for cfg, fam in ML_ARMS:
        preds[f"{cfg}/{fam}"] = {}
    for fam in ("logit", "gbm"):
        preds[f"pooled/{fam}"] = {}
    abstain = {d for d in days if mf.required_missing(fs, d)}       # the multi-instrument model abstains there
    plan = folds(len(days))
    jobs = min(len(plan), os.cpu_count() or 1) if jobs is None else jobs
    t1 = time.time()
    done = Parallel(n_jobs=jobs)(delayed(_fold)(X, PX, y, py_, abstain, days, *f, split=split) for f in plan)
    fold_log = []
    for fold_preds, entry in done:                  # in fold order, whatever order the workers finished in
        for arm, got in fold_preds.items():
            preds[arm].update(got)
        fold_log.append(entry)
        progress(f"fold {len(fold_log)}: trained to {entry['train'][1]} ({entry['train'][2]} sessions), tested "
                 f"{entry['test'][0]} to {entry['test'][1]}")
    progress(f"{len(plan)} folds in {time.time() - t1:.1f} s ({jobs} worker process(es))")
    test_days = days[folds(len(days))[0][1]:]
    for arm in ("A", "B"):
        preds[arm] = {d: stored[arm][d] for d in test_days if d in stored[arm]}
    arms = list(preds)
    scored = {a: {d: _scores(preds[a][d], y[d]) for d in test_days if d in preds[a] and y[d] is not None}
              for a in arms}
    common = [d for d in test_days if y[d] is not None and all(d in preds[a] for a in arms)]
    # availability over every scheduled session of the test span (a session without a snapshot counts against all)
    scheduled = [s.session_date.isoformat() for s in cal.sessions_between(test_days[0], test_days[-1]) if s.is_open]
    summary = {}
    for a in arms:
        rows = [scored[a][d] for d in common]
        fin = [r[1] for r in rows if math.isfinite(r[1])]
        summary[a] = {"brier": float(np.mean([r[0] for r in rows])), "logloss": float(np.mean(fin)) if fin else None,
                      "logloss_infinite": len(rows) - len(fin),
                      "available": sum(1 for d in scheduled if d in preds[a]), "scheduled": len(scheduled),
                      "calibration": calibration(preds[a], y, common)}
    pairs = {}
    for a, b in [("nq_only/logit", "A"), ("nq_only/gbm", "A"), ("multi/logit", "A"), ("multi/gbm", "A"),
                 ("nq_only/logit", "B"), ("nq_only/gbm", "B"), ("multi/logit", "B"), ("multi/gbm", "B"),
                 ("multi/logit", "nq_only/logit"), ("multi/gbm", "nq_only/gbm"), ("B", "A"),
                 ("pooled/logit", "nq_only/logit"), ("pooled/gbm", "nq_only/gbm"), ("pooled/logit", "A"),
                 ("pooled/gbm", "A"), ("pooled/logit", "B"), ("pooled/gbm", "B")]:
        pairs[f"{a} - {b}"] = paired(scored[a], scored[b], common)
    class_counts = {c: sum(1 for d in common if y[d] == c) for c in ml.CLASSES}
    return {"profile": profile, "split": split, "sessions": len(days),
            "labelled": sum(1 for d in days if y[d] is not None),
            "test_span": [test_days[0], test_days[-1]], "test_sessions": len(test_days), "common": len(common),
            "class_counts": class_counts, "folds": fold_log, "summary": summary, "pairs": pairs,
            "pooled_rows": {s: int(sum(1 for (x, d) in PX.index if x == s and isinstance(py_[(x, d)], str)))
                            for s in ml.POOLED_INSTRUMENTS},
            "missing_share": {c: float(X["multi"][c].isna().mean()) for c in X["multi"].columns}}


def latency(conn, versions: Sequence[str], days: Sequence[str], root: Optional[str] = None) -> Dict[str, Any]:
    """Seconds from nothing loaded to probabilities, per issue: the session's features as of its cutoff from the
    database, the artifact loaded and checked against its manifest, the prediction - the ML forecasts' measured
    inference latency (whatever the in-sample rule would say of the day)."""
    from forecaster.ml_train import pool
    snaps = {str(s["session_date"]): s for s in pool(conn)}
    times = {v: [] for v in versions}
    for d in days:
        for v in versions:
            t0 = time.perf_counter()
            art = mm.load(v, root)
            fs = mf.build(conn, [d], {d: snaps[d]})
            mm.probabilities(art["model"], mf.matrix(fs, [d], ml.CONFIG_OF[v]).to_numpy())
            times[v].append(time.perf_counter() - t0)
    return {v: {"n": len(t), "median_s": float(np.median(t)), "max_s": float(np.max(t))} for v, t in times.items()}


def _f(x, nd=4):
    return "-" if x is None else f"{x:+.{nd}f}" if isinstance(x, float) and x < 0 else f"{x:.{nd}f}"


def _ci(iv):
    return "-" if not iv else f"[{iv[0]:+.4f}, {iv[1]:+.4f}]"


def report(res: Dict[str, Any], chosen: Dict[str, str], lat: Optional[Dict[str, Any]] = None,
           inventory_note: str = "") -> str:
    """The development comparison as markdown."""
    s = res["summary"]
    lines = [
        "# NQ direction model: development comparison", "",
        f"**Development data, not a test.** Every one of these sessions has been inspected before (hist_dev_v1, "
        f"p1_pool_tuning_v1, the fan experiments). An improvement here would be a candidate for the forward "
        f"evaluation ({ml.FORWARD['name']}), not a result.", "",
        f"- **Target:** {ml.TARGET}, exactly as labelled (bullish above T, bearish below -T, else the neutral band).",
        f"- **Sessions:** {res['sessions']} in the research_0929 pool ({res['labelled']} with a label); walk-forward "
        f"test span {res['test_span'][0]} to {res['test_span'][1]} ({res['test_sessions']} sessions, "
        f"{len(res['folds'])} folds: train on every earlier session, one-session embargo, test the next "
        f"{ml.DEV_EVALUATION['test_block_sessions']}).",
        f"- **Compared on** the {res['common']} test sessions where every arm has a forecast and the session a label "
        f"(classes: " + ", ".join(f"{k} {v}" for k, v in res["class_counts"].items()) + ").",
        "- **Primary score:** the unhalved multiclass Brier score (0 to 2, lower is better); intervals are 95 % "
        "moving-block bootstrap intervals (blocks of 5 sessions) of the paired per-session differences.",
        "- **Tuning split:** " + ("the session-date inner split with the one-session embargo, the pooled candidate "
                                  "scored on NQ's validation rows (contracts/nq_ml.SPLIT)."
                                  if res.get("split", "dates") == "dates" else
                                  "the v1 row-position cut (legacy reproduction: the pooled candidate's validation "
                                  "rows were RTY's, on dates also in training - docs/reports/ml_pooled_split_v1.md)."),
        "",
        "## Scores on the common sessions", "",
        "| arm | Brier | log loss | calibration error (mean over classes) | available / scheduled |",
        "|---|---:|---:|---:|---:|"]
    names = {"A": "A frequencies", "B": "B analogues", "nq_only/logit": "NQ-only, logistic",
             "nq_only/gbm": "NQ-only, boosted", "multi/logit": "multi-instrument, logistic",
             "multi/gbm": "multi-instrument, boosted", "pooled/logit": "pooled (NQ+ES+RTY), logistic",
             "pooled/gbm": "pooled (NQ+ES+RTY), boosted"}
    for a, v in s.items():
        lines.append(f"| {names.get(a, a)} | {v['brier']:.4f} | {_f(v['logloss'])} | "
                     f"{_f(v['calibration']['ece_mean'], 3)} | {v['available']} / {v['scheduled']} |")
    lines += ["", "## Paired differences (negative favours the first arm)", "",
              "| comparison | sessions | Brier difference | 95 % interval | log-loss difference | 95 % interval |",
              "|---|---:|---:|---|---:|---|"]
    for k, v in res["pairs"].items():
        a, b = k.split(" - ")
        lines.append(f"| {names.get(a, a)} - {names.get(b, b)} | {v['n']} | {_f(v['brier_diff'])} | "
                     f"{_ci(v['brier_interval'])} | {_f(v['logloss_diff'])} | {_ci(v['logloss_interval'])} |")
    lines += ["", "## Reading", ""]
    for k in ("multi/logit - nq_only/logit", "multi/gbm - nq_only/gbm"):
        v = res["pairs"][k]
        iv = v["brier_interval"]
        verdict = ("lower Brier with the whole interval below zero" if iv and iv[1] < 0 else
                   "higher Brier with the whole interval above zero" if iv and iv[0] > 0 else
                   "no difference established (the interval spans zero)")
        lines.append(f"- **Do the other instruments help ({k.split(' - ')[0].split('/')[1]})?** {verdict}: "
                     f"{_f(v['brier_diff'])} {_ci(iv)}.")
    for k in ("pooled/logit - nq_only/logit", "pooled/gbm - nq_only/gbm"):
        v = res["pairs"][k]
        iv = v["brier_interval"]
        verdict = ("better on NQ, interval below zero" if iv and iv[1] < 0 else
                   "worse on NQ, interval above zero" if iv and iv[0] > 0 else "no difference established")
        lines.append(f"- **Pooled training ({k.split(' - ')[0].split('/')[1]}):** {verdict}: {_f(v['brier_diff'])} "
                     f"{_ci(iv)} - development data: a candidate (arm P, experimental) that only the forward "
                     f"evaluation can promote.")
    for k in ("multi/logit - B", "nq_only/logit - B", "multi/gbm - B", "nq_only/gbm - B", "pooled/logit - B",
              "pooled/gbm - B"):
        v = res["pairs"][k]
        iv = v["brier_interval"]
        lines.append(f"- **{names[k.split(' - ')[0]]} against B:** {_f(v['brier_diff'])} {_ci(iv)}.")
    v = res["pairs"]["B - A"]
    lines.append(f"- **B against A:** {_f(v['brier_diff'])} {_ci(v['brier_interval'])} - B stays in force because it "
                 "is the existing baseline, not because this shows it better than A; A remains the benchmark any "
                 "promotion must beat (contracts/nq_ml.promotion_rule).")
    lines += ["", f"Pooled training rows: " + ", ".join(f"{k} {v}" for k, v in res["pooled_rows"].items())
              + " - three instruments on one day share its news, so the effective sample grows far less than "
                "threefold.", "",
              "## Chosen for the artifacts", "",
              "The family with the lower development Brier score per configuration (a development choice; the "
              "forward evaluation tests it):", ""]
    for cfg, fam in chosen.items():
        lines.append(f"- **{cfg}:** {fam}")
    if lat:
        lines += ["", "## Inference latency", "",
                  "From nothing loaded to probabilities - the session's features as of the cutoff from the database, "
                  "the artifact loaded and checked, the prediction:", ""]
        for v, t in lat.items():
            lines.append(f"- **{v}:** median {t['median_s']:.2f} s, slowest {t['max_s']:.2f} s ({t['n']} issues)")
    lines += ["", "## Missing inputs (share of pool sessions, multi-instrument features)", ""]
    miss = {k: v for k, v in res["missing_share"].items() if v > 0}
    lines += [f"- {k}: {v:.1%}" for k, v in sorted(miss.items(), key=lambda kv: -kv[1])] or ["- none"]
    lines += ["", "## Calibration (reliability, common sessions)", ""]
    for a, v in s.items():
        cal_ = v["calibration"]["classes"]
        lines.append(f"- **{names.get(a, a)}:** " + "; ".join(
            f"{c} ECE {cal_[c]['ece']:.3f}" for c in ml.CLASSES))
    if inventory_note:
        lines += ["", inventory_note]
    lines += ["", "Folds: " + "; ".join(f"{f['train'][1]} → {f['test'][0]}..{f['test'][1]}" for f in res["folds"])]
    return "\n".join(lines) + "\n"
