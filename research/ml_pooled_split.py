# research/ml_pooled_split.py
"""
ml_pooled_split_v1 - the correction of the pooled model's inner tuning split, rerun on the
development data under its own identifier. ml_study_v1 (docs/reports/ml_study.md) and its files,
hashes and the registered v1 artifacts are left exactly as they are; this study reads them and
reports before and after.

The defect (forecaster/ml_split.py): forecaster/ml_train.dataset builds the pooled rows instrument
first, date second; training_rows() kept that order and dropped the dates; ml_model.tune cut the rows
at 75 % - so the pooled candidate's inner validation rows were RTY's, on session dates whose NQ and ES
rows (and later dates) were in inner training. The outer walk-forward folds always excluded their test
dates: this is not outer-test leakage and nothing assumes the correction improves P. It made the inner
validation unfit for chronological NQ model selection and weakened the attribution of any pooling
benefit. The same tuner chose N's and M's parameters (one row per session, in date order, so their cut
was chronological - but without the session embargo).

  predict(conn)   the development comparison's outer folds and the five-session check, each refitted
                  twice through the production fold code (forecaster/ml_eval._fold): split='legacy_rows'
                  (before - must reproduce ml_study_v1's frozen N, M and P predictions) and split='dates'
                  (after: session-date inner split, the one-session embargo, the pooled candidate tuned
                  on NQ validation rows); the legacy inner split's composition per fold on the
                  production rows; the shuffled-label and planted-signal controls of the frozen arms
                  under both splits. Writes the predictions with their sha256 before any outcome is read
  score(conn)     refuses changed prediction files, then reads the outcomes: per arm before and after,
                  paired differences (after - before, against A and B, P - N) with the moving-block
                  bootstrap, the five-session check, the controls; writes results.json and
                  docs/reports/ml_pooled_split_v1.md

Development data only, like ml_study_v1: a candidate, never a result.

    python scripts/ml_pooled_split.py predict
    python scripts/ml_pooled_split.py score
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from contracts import nq_ml as ml

VERSION = "ml_pooled_split_v1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(ROOT, "docs", "reports", VERSION)
REPORT_MD = os.path.join(ROOT, "docs", "reports", f"{VERSION}.md")
V1_DIR = os.path.join(ROOT, "docs", "reports", "ml_study_v1")
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
FROZEN = {"N": "nq_only/logit", "M": "multi/logit", "P": "pooled/gbm"}
ARMS = ("N", "M", "P", "pooled/logit", "nq_only/gbm", "multi/gbm")
SPLITS = ("legacy_rows", "dates")
UNC = {"block": 5, "resamples": 2000, "seed": 20261011, "interval": 0.95}
CONTROL = {"shuffles": 10, "synthetic_seeds": 5, "gain": 0.04, "columns": ("nq_ret_on", "nq_range_pos")}
TOL = 1e-9


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def v1_hashes() -> Dict[str, str]:
    """sha256 of every ml_study_v1 file: recorded before and after, so the study can show it left them unchanged."""
    return {name: _sha(os.path.join(V1_DIR, name)) for name in sorted(os.listdir(V1_DIR))}


# --------------------------------------------------------------------------
# Refits (pure given the dataset): the outer folds and the five-session check, under one split
# --------------------------------------------------------------------------

def outer_refits(data, abstain: Sequence[str], split: str, jobs: int = 1, y=None, py=None
                 ) -> Tuple[Dict[str, Dict[str, np.ndarray]], List[Dict[str, Any]]]:
    """Every development fold through forecaster/ml_eval._fold under ``split``: ``(arm -> day -> probabilities,
    fold log)``. ``y`` / ``py`` replace the labels (the controls)."""
    from joblib import Parallel, delayed
    from forecaster import ml_eval
    y = data.y if y is None else y
    py = data.py if py is None else py
    plan = ml_eval.folds(len(data.days))
    work = [delayed(ml_eval._fold)(data.X, data.PX, y, py, set(abstain), data.days, *f, split=split) for f in plan]
    done = Parallel(n_jobs=jobs)(work) if jobs > 1 else [w[0](*w[1], **w[2]) for w in work]
    preds: Dict[str, Dict[str, np.ndarray]] = {}
    log = []
    for p, entry in done:
        for arm, got in p.items():
            preds.setdefault(arm, {}).update(got)
        log.append(entry)
    return preds, log


def week_refits(data, split: str) -> Dict[str, Any]:
    """The five-session check: per day N, M and P refitted on every labelled session before it (the earlier check's
    method, no outer embargo), under ``split``."""
    from forecaster import ml_features as mf
    from forecaster import ml_model as mm
    out = {}
    for d in WEEK:
        if d not in data.days:
            continue
        train = [x for x in data.days if x < d and data.y[x] is not None]
        row: Dict[str, Any] = {"trained_to": train[-1], "sessions": len(train)}
        for arm, version in (("N", ml.ML_NQ_VERSION), ("M", ml.ML_MULTI_VERSION)):
            cfg = ml.CONFIG_OF[version]
            model, params, _ = mm.tune(data.X[cfg].loc[train].to_numpy(), [data.y[x] for x in train],
                                       ml.FAMILY[version], dates=train, split=split)
            row[arm] = (None if cfg == "multi" and mf.required_missing(data.fs, d) else
                        mm.probabilities(model, data.X[cfg].loc[[d]].to_numpy())[0].tolist())
            row[f"{arm}_params"] = params
        keep = [(s, x) for (s, x) in data.PX.index if x in set(train) and isinstance(data.py[(s, x)], str)]
        model, params, _ = mm.tune(data.PX.loc[keep].to_numpy(), [data.py[k] for k in keep],
                                   ml.FAMILY[ml.ML_POOLED_VERSION], dates=[x for _, x in keep],
                                   instruments=[s for s, _ in keep], split=split)
        row["P"] = mm.probabilities(model, data.X["pooled"].loc[[d]].to_numpy())[0].tolist()
        row["P_params"] = params
        row["P_rows"] = len(keep)
        out[d] = row
    return out


def inner_diagnostics(data) -> List[Dict[str, Any]]:
    """Per development fold, what the legacy row cut did to the pooled candidate's training rows (in the order
    training_rows produced them) and what the corrected split uses instead."""
    from forecaster import ml_eval
    from forecaster import ml_split as sp
    out = []
    for train_end, test_start, test_end in ml_eval.folds(len(data.days)):
        train = [d for d in data.days[:train_end] if data.y[d] is not None]
        keep = set(train)
        idx = [(s, d) for (s, d) in data.PX.index if d in keep and isinstance(data.py[(s, d)], str)]
        legacy = sp.legacy_diagnostics([d for _, d in idx], [s for s, _ in idx], ml.TUNING["validation_share"])
        fixed = sp.inner_split([d for _, d in idx], ml.TUNING["validation_share"], ml.SPLIT["embargo_sessions"])
        nq_val = sum(1 for s, d in idx if s == "NQ" and d in set(fixed.test))
        out.append({"train": [train[0], train[-1], len(train)], "legacy": legacy,
                    "corrected": {**fixed.describe(), "objective_rows_nq": nq_val,
                                  "validation_rows_all": sum(1 for _, d in idx if d in set(fixed.test))}})
    return out


def _control_labels(data, kind: str, seed: int, beta: Optional[float]):
    """The control's labels: ``shuffled`` moves every session's labels (all instruments together) to another
    session; ``synthetic`` draws NQ's labels from a planted signal (research/ml_study.synthetic), the other
    instruments' unchanged."""
    import pandas as pd
    from research import ml_study as st
    days = data.days
    cls = {c: i for i, c in enumerate(st.CLASSES)}
    if kind == "shuffled":
        perm = np.random.default_rng(seed).permutation(len(days))
        src = {d: days[perm[i]] for i, d in enumerate(days)}
        y = {d: data.y[src[d]] for d in days}
        py = pd.Series([data.py.get((s, src[d])) for (s, d) in data.PX.index], index=data.PX.index, dtype=object)
        return y, py, None
    yy = np.array([cls[data.y[d]] if isinstance(data.y[d], str) else -1 for d in days])
    problem = st.Problem(name="control", sessions=list(days), row_session=np.arange(len(days)),
                         keys=np.zeros(len(days), int), phase=np.zeros(len(days), int), X=data.X["multi"].copy(),
                         y=yy, feature_sets=st.PREOPEN_FEATURES, cp_grid=st.CP_GRID_PREOPEN, cp_context={})
    prob, oracle = st.synthetic(problem, CONTROL["columns"], beta, seed)
    y = {d: (st.CLASSES[v] if v >= 0 else None) for d, v in zip(days, prob.y)}
    py = pd.Series([(y[d] if s == "NQ" else data.py[(s, d)]) for (s, d) in data.PX.index], index=data.PX.index,
                   dtype=object)
    return y, py, st.oracle_gain(prob, oracle)


def controls(data, abstain: Sequence[str], jobs: int = 1) -> List[Dict[str, Any]]:
    """The frozen arms' shuffled-label and planted-signal controls under both splits: their predictions and labels
    (scored in ``score``)."""
    from research import ml_study as st
    yy = np.array([st.CLASSES.index(data.y[d]) if isinstance(data.y[d], str) else -1 for d in data.days])
    problem = st.Problem(name="control", sessions=list(data.days), row_session=np.arange(len(data.days)),
                         keys=np.zeros(len(data.days), int), phase=np.zeros(len(data.days), int),
                         X=data.X["multi"].copy(), y=yy, feature_sets=st.PREOPEN_FEATURES, cp_grid=st.CP_GRID_PREOPEN,
                         cp_context={})
    beta = st.beta_for(problem, CONTROL["columns"], CONTROL["gain"])
    tasks = ([("shuffled", s, None) for s in range(CONTROL["shuffles"])]
             + [("synthetic", s, beta) for s in range(CONTROL["synthetic_seeds"])])
    out = []
    for kind, seed, b in tasks:
        y, py, oracle = _control_labels(data, kind, seed, b)
        entry = {"kind": kind, "seed": seed, "beta": b, "oracle_gain": oracle, "labels": y, "splits": {}}
        for split in SPLITS:
            preds, _ = outer_refits(data, abstain, split, jobs, y=y, py=py)
            entry["splits"][split] = {arm: {d: p.tolist() for d, p in preds[FROZEN[arm]].items()} for arm in FROZEN}
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def _write_predictions(path: str, preds: Dict[str, Dict[str, np.ndarray]]) -> None:
    """One row per arm and session: the three probabilities in contracts/nq_ml.CLASSES order (deterministic text)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["session", "arm"] + [f"p_{c}" for c in ml.CLASSES])
    for arm in sorted(preds):
        for d in sorted(preds[arm]):
            w.writerow([d, arm] + [f"{float(x):.12g}" for x in preds[arm][d]])
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())


def _read_predictions(path: str) -> Dict[str, Dict[str, np.ndarray]]:
    out: Dict[str, Dict[str, np.ndarray]] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.setdefault(row["arm"], {})[row["session"]] = np.array([float(row[f"p_{c}"]) for c in ml.CLASSES])
    return out


def v1_frozen() -> Dict[str, Dict[str, np.ndarray]]:
    """ml_study_v1's stored frozen-refit predictions (N, M, P and the dev: rows) by arm and session."""
    out: Dict[str, Dict[str, np.ndarray]] = {}
    with open(os.path.join(V1_DIR, "predictions_preopen.csv"), encoding="utf-8") as f:
        for row in csv.DictReader(f):
            arm = row["arm"]
            key = FROZEN.get(arm) or (arm[4:] if arm.startswith("dev:") else None)
            if key is not None:
                out.setdefault(key, {})[row["session"]] = np.array([float(row[f"p_{c}"]) for c in ml.CLASSES])
    return out


def reproduction(before: Dict[str, Dict[str, np.ndarray]]) -> Dict[str, Any]:
    """Whether the legacy refits reproduce ml_study_v1's stored predictions: per arm the sessions compared and the
    largest absolute difference."""
    stored = v1_frozen()
    out = {}
    for arm, days in stored.items():
        mine = before.get(arm, {})
        common = sorted(set(days) & set(mine))
        diff = max((float(np.max(np.abs(days[d] - mine[d]))) for d in common), default=None)
        out[arm] = {"sessions": len(common), "stored": len(days), "max_abs_diff": diff,
                    "reproduced": bool(common) and len(common) == len(days) and diff is not None and diff <= 1e-6}
    return out


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------

def predict(conn, jobs: int = 1, with_controls: bool = True, progress=print) -> Dict[str, Any]:
    """The refits under both splits, written with their sha256 before any outcome is read (see the module
    docstring)."""
    from forecaster import ml_features as mf
    from forecaster.ml_train import dataset
    from forecaster.provenance import code_revision
    t0 = time.time()
    os.makedirs(REPORT_DIR, exist_ok=True)
    before_v1 = v1_hashes()
    data = dataset(conn)
    abstain = sorted(d for d in data.days if mf.required_missing(data.fs, d))
    progress(f"{len(data.days)} sessions, {len(data.PX)} pooled rows ({time.time() - t0:.0f} s)")
    files, logs, weeks = {}, {}, {}
    for split in SPLITS:
        preds, log = outer_refits(data, abstain, split, jobs)
        name = f"predictions_{split}.csv"
        _write_predictions(os.path.join(REPORT_DIR, name), preds)
        files[name] = _sha(os.path.join(REPORT_DIR, name))
        logs[split] = log
        weeks[split] = week_refits(data, split)
        progress(f"{split}: {len(log)} folds ({time.time() - t0:.0f} s)")
    with open(os.path.join(REPORT_DIR, "week_refits.json"), "w", encoding="utf-8") as f:
        json.dump(weeks, f, indent=1, sort_keys=True)
    files["week_refits.json"] = _sha(os.path.join(REPORT_DIR, "week_refits.json"))
    diag = {"inner": inner_diagnostics(data), "folds": logs}
    with open(os.path.join(REPORT_DIR, "diagnostics.json"), "w", encoding="utf-8") as f:
        json.dump(diag, f, indent=1, sort_keys=True, default=str)
    files["diagnostics.json"] = _sha(os.path.join(REPORT_DIR, "diagnostics.json"))
    if with_controls:
        ctl = controls(data, abstain, jobs)
        with open(os.path.join(REPORT_DIR, "controls.json"), "w", encoding="utf-8") as f:
            json.dump([{k: v for k, v in c.items() if k != "labels"} for c in ctl], f, indent=1, sort_keys=True)
        files["controls.json"] = _sha(os.path.join(REPORT_DIR, "controls.json"))
        with open(os.path.join(REPORT_DIR, "control_labels.json"), "w", encoding="utf-8") as f:
            json.dump([{"kind": c["kind"], "seed": c["seed"], "labels": c["labels"]} for c in ctl], f, indent=1,
                      sort_keys=True)
        files["control_labels.json"] = _sha(os.path.join(REPORT_DIR, "control_labels.json"))
        progress(f"controls: {len(ctl)} runs x {len(SPLITS)} splits ({time.time() - t0:.0f} s)")
    manifest = {"version": VERSION, "stage": "predict", "at": _now(), "code_revision": code_revision(),
                "sessions": len(data.days), "pooled_rows": len(data.PX), "splits": list(SPLITS),
                "split_rule": ml.SPLIT, "files": files, "ml_study_v1_files": before_v1,
                "control": CONTROL, "seconds": round(time.time() - t0, 1)}
    if v1_hashes() != before_v1:
        raise RuntimeError("an ml_study_v1 file changed while this study ran")
    with open(os.path.join(REPORT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    return manifest


def _brier(p: np.ndarray, y: str) -> float:
    return float(sum((p[j] - (1.0 if c == y else 0.0)) ** 2 for j, c in enumerate(ml.CLASSES)))


def _logloss(p: np.ndarray, y: str) -> float:
    q = float(p[ml.CLASSES.index(y)])
    return math.inf if q <= 0 else -math.log(q)


def _paired(a: Dict[str, float], b: Dict[str, float], days: Sequence[str]) -> Dict[str, Any]:
    from forecaster.experiments import block_bootstrap
    d = [a[x] - b[x] for x in days]
    iv = block_bootstrap(d, UNC["block"], UNC["resamples"], UNC["seed"], UNC["interval"]) if d else None
    return {"n": len(d), "mean": float(np.mean(d)) if d else None, "interval": iv}


def _skill(preds: Dict[str, np.ndarray], labels: Dict[str, Optional[str]], train_prior) -> Dict[str, Any]:
    """Mean per-session Brier difference from the expanding training-frequency prior (negative: skill)."""
    days = sorted(d for d in preds if isinstance(labels.get(d), str))
    a = {d: _brier(preds[d], labels[d]) for d in days}
    b = {d: _brier(train_prior[d], labels[d]) for d in days}
    return _paired(a, b, days)


def _expanding_prior(days: Sequence[str], labels: Dict[str, Optional[str]], test: Sequence[str]) -> Dict[str, np.ndarray]:
    """For each test session, the Laplace-smoothed class frequencies of the labelled sessions before it less the
    one-session embargo (the folds' training windows)."""
    out = {}
    pos = {d: i for i, d in enumerate(days)}
    for d in test:
        prev = [labels[x] for x in days[:max(0, pos[d] - ml.SPLIT["embargo_sessions"])] if isinstance(labels[x], str)]
        out[d] = np.array([(prev.count(c) + 1) / (len(prev) + len(ml.CLASSES)) for c in ml.CLASSES])
    return out


def score(conn, progress=print) -> Dict[str, Any]:
    """Checks the prediction files against the manifest, then reads the outcomes (see the module docstring)."""
    from forecaster import ml_eval
    from forecaster.ml_train import labels, pool
    with open(os.path.join(REPORT_DIR, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    for name, sha in manifest["files"].items():
        if _sha(os.path.join(REPORT_DIR, name)) != sha:
            raise RuntimeError(f"{name} changed after the predict stage recorded it: refusing to score")
    if v1_hashes() != manifest["ml_study_v1_files"]:
        raise RuntimeError("an ml_study_v1 file differs from the one recorded at the predict stage")
    snaps = pool(conn)
    y = labels(conn, snaps)                                   # the outcomes: read only now
    stored = {arm: ml_eval.stored_arm(conn, snaps, alg) for arm, alg in ml_eval.ARMS_A_B.items()}
    preds = {split: _read_predictions(os.path.join(REPORT_DIR, f"predictions_{split}.csv")) for split in SPLITS}
    before, after = preds["legacy_rows"], preds["dates"]
    keyed = {f"{a}@before": before[FROZEN.get(a, a)] for a in ARMS}
    keyed.update({f"{a}@after": after[FROZEN.get(a, a)] for a in ARMS})
    keyed.update(A=stored["A"], B=stored["B"])
    test = sorted(set().union(*[set(v) for v in keyed.values() if v]) & set(before[FROZEN["N"]]))
    common = [d for d in test if isinstance(y.get(d), str) and all(d in keyed[a] for a in keyed
                                                                    if not a.startswith("M"))]
    common_m = [d for d in common if all(d in keyed[a] for a in keyed if a.startswith("M"))]
    br = {a: {d: _brier(p[d], y[d]) for d in p if isinstance(y.get(d), str)} for a, p in keyed.items()}
    ll = {a: {d: _logloss(p[d], y[d]) for d in p if isinstance(y.get(d), str)} for a, p in keyed.items()}
    summary = {}
    for a in keyed:
        days = common_m if a.startswith("M") else common
        fin = [ll[a][d] for d in days if math.isfinite(ll[a][d])]
        summary[a] = {"n": len(days), "brier": float(np.mean([br[a][d] for d in days])) if days else None,
                      "logloss": float(np.mean(fin)) if fin else None}
    pairs = {}
    for arm in ARMS:
        days = common_m if arm.startswith("M") or arm.startswith("multi") else common
        pairs[f"{arm}: after - before"] = _paired(br[f"{arm}@after"], br[f"{arm}@before"], days)
        for base in ("A", "B"):
            pairs[f"{arm}@after - {base}"] = _paired(br[f"{arm}@after"], br[base], days)
            pairs[f"{arm}@before - {base}"] = _paired(br[f"{arm}@before"], br[base], days)
    for when in ("before", "after"):
        pairs[f"P@{when} - N@{when}"] = _paired(br[f"P@{when}"], br[f"N@{when}"], common)
        pairs[f"pooled/logit@{when} - N@{when}"] = _paired(br[f"pooled/logit@{when}"], br[f"N@{when}"], common)
    with open(os.path.join(REPORT_DIR, "week_refits.json"), encoding="utf-8") as f:
        weeks = json.load(f)
    week = {}
    for split, rows in weeks.items():
        week[split] = {arm: {d: (None if r.get(arm) is None or not isinstance(y.get(d), str)
                                 else _brier(np.array(r[arm]), y[d])) for d, r in rows.items()}
                       for arm in ("N", "M", "P")}
        week[split]["params"] = {d: {a: r.get(f"{a}_params") for a in ("N", "M", "P")} for d, r in rows.items()}
    week_days = sorted(set().union(*[set(rows) for rows in weeks.values()]))
    week["A"] = {d: (_brier(stored["A"][d], y[d]) if d in stored["A"] and isinstance(y.get(d), str) else None)
                 for d in week_days}
    week["B"] = {d: (_brier(stored["B"][d], y[d]) if d in stored["B"] and isinstance(y.get(d), str) else None)
                 for d in week_days}
    ctl_out = []
    if "controls.json" in manifest["files"]:
        with open(os.path.join(REPORT_DIR, "controls.json"), encoding="utf-8") as f:
            ctl = json.load(f)
        with open(os.path.join(REPORT_DIR, "control_labels.json"), encoding="utf-8") as f:
            ctl_labels = json.load(f)
        days = [str(s["session_date"]) for s in snaps]
        for c, lab in zip(ctl, ctl_labels):
            labels_c = lab["labels"]
            entry = {"kind": c["kind"], "seed": c["seed"], "oracle_gain": c.get("oracle_gain"), "skill": {}}
            for split, arms in c["splits"].items():
                for arm, by_day in arms.items():
                    p = {d: np.array(v) for d, v in by_day.items()}
                    prior = _expanding_prior(days, labels_c, sorted(p))
                    entry["skill"][f"{arm}@{split}"] = _skill(p, labels_c, prior)
            ctl_out.append(entry)
    with open(os.path.join(REPORT_DIR, "diagnostics.json"), encoding="utf-8") as f:
        diag = json.load(f)
    res = {"version": VERSION, "at": _now(), "common": len(common), "common_with_m": len(common_m),
           "test_span": [test[0], test[-1]] if test else None, "summary": summary, "pairs": pairs, "week": week,
           "controls": ctl_out, "reproduction": reproduction(before), "inner": diag["inner"],
           "chosen": {split: [f.get("chosen") for f in diag["folds"][split]] for split in SPLITS}}
    with open(os.path.join(REPORT_DIR, "results.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, sort_keys=True, default=str)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(markdown(res, manifest))
    progress(f"scored {len(common)} common sessions; report {REPORT_MD}")
    return res


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _f(x, nd=4) -> str:
    return "-" if x is None else f"{x:+.{nd}f}"


def _iv(iv) -> str:
    return "-" if not iv else f"[{iv[0]:+.4f}, {iv[1]:+.4f}]"


def markdown(res: Dict[str, Any], manifest: Dict[str, Any]) -> str:
    s, p = res["summary"], res["pairs"]
    names = {"N": "N NQ-only logit", "M": "M multi-instrument logit", "P": "P pooled boosted",
             "pooled/logit": "pooled logit", "nq_only/gbm": "NQ-only boosted", "multi/gbm": "multi boosted"}
    lines = [
        f"# The pooled tuning split, corrected ({VERSION})", "",
        "**Development data, not a test.** Every session was inspected before. This study corrects a defect in the "
        "inner tuning split and reruns the affected development results under its own identifier; "
        "ml_study_v1's files and hashes and the registered v1 artifacts are unchanged "
        f"({len(manifest['ml_study_v1_files'])} ml_study_v1 files hashed before and after).", "",
        "## The defect", "",
        "forecaster/ml_train.dataset built the pooled rows instrument first, date second; training_rows kept that "
        "order and dropped the dates; ml_model.tune took the first 75 % of the rows for training and the last 25 % "
        "for validation. So the pooled candidate's inner validation rows were RTY's, on dates whose NQ and ES rows "
        "- and later dates - were in inner training. The outer walk-forward folds always excluded their test dates: "
        "this is not outer-test leakage, and the correction was not assumed to improve P. It made the inner "
        "validation unfit for chronological NQ model selection and weakened the attribution of any pooling benefit.",
        "", "| fold (trained to) | legacy validation rows by instrument | validation dates also in training | "
            "training reached past validation start | corrected: NQ objective rows |", "|---|---|---:|---|---:|"]
    for f in res["inner"]:
        lg = f["legacy"]
        lines.append(f"| {f['train'][1]} | " + ", ".join(f"{k} {v}" for k, v in lg["validation_instruments"].items())
                     + f" | {lg['validation_dates_also_in_training']} of {lg['validation_dates']} | "
                       f"{'yes' if lg['training_after_validation_start'] else 'no'} | "
                       f"{f['corrected']['objective_rows_nq']} |")
    lines += ["", "The correction (forecaster/ml_split.py): every inner and outer split is made on session dates - "
              "all instruments of a date on one side - with the one-session embargo between inner training and "
              "validation, and the pooled candidate is scored on NQ's validation rows only. Preprocessing stays "
              "inside each fit's pipeline.", "",
              "## Does 'before' reproduce ml_study_v1?", "",
              "The legacy refits against ml_study_v1's stored frozen predictions (predictions_preopen.csv), "
              "session by session:", ""]
    for arm, r in res["reproduction"].items():
        lines.append(f"- {arm}: {r['sessions']} of {r['stored']} sessions, largest difference "
                     f"{'-' if r['max_abs_diff'] is None else f'{r['max_abs_diff']:.2e}'} - "
                     f"{'reproduced' if r['reproduced'] else 'NOT reproduced'}")
    lines += ["", f"## Scores ({res['common']} common sessions; M on {res['common_with_m']})", "",
              "| arm | before | after | after - before | 95 % interval | after - A | after - B |",
              "|---|---:|---:|---:|---|---|---|"]
    for arm in ARMS:
        a, b = s[f"{arm}@before"], s[f"{arm}@after"]
        d = p[f"{arm}: after - before"]
        lines.append(f"| {names[arm]} | {a['brier']:.4f} | {b['brier']:.4f} | {_f(d['mean'])} | {_iv(d['interval'])} | "
                     f"{_f(p[f'{arm}@after - A']['mean'])} {_iv(p[f'{arm}@after - A']['interval'])} | "
                     f"{_f(p[f'{arm}@after - B']['mean'])} {_iv(p[f'{arm}@after - B']['interval'])} |")
    lines += [f"| A frequencies | {s['A']['brier']:.4f} | | | | | |",
              f"| B analogues | {s['B']['brier']:.4f} | | | | | |", "",
              "**Pooling effect (P - N):** before " + f"{_f(p['P@before - N@before']['mean'])} "
              f"{_iv(p['P@before - N@before']['interval'])}, after {_f(p['P@after - N@after']['mean'])} "
              f"{_iv(p['P@after - N@after']['interval'])}.", "",
              f"## The five-session check ({', '.join(res['week'].get('A', {}).keys()) or '-'})", "",
              "| session | A | B | N before | N after | M before | M after | P before | P after |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    w = res["week"]
    for d in w.get("A", {}):
        cell = lambda v: "-" if v is None else f"{v:.3f}"  # noqa: E731
        lines.append(f"| {d} | {cell(w['A'].get(d))} | {cell(w['B'].get(d))} | "
                     + " | ".join(cell(w[sp][arm].get(d)) for arm in ("N", "M", "P") for sp in SPLITS) + " |")
    if res["controls"]:
        lines += ["", "## Controls of the frozen arms (skill = Brier minus the expanding training prior)", "",
                  "| control | seed | N before | N after | M before | M after | P before | P after |",
                  "|---|---:|---|---|---|---|---|---|"]
        for c in res["controls"]:
            lines.append(f"| {c['kind']} | {c['seed']} | " + " | ".join(
                _f(c["skill"].get(f"{arm}@{sp}", {}).get("mean")) for arm in ("N", "M", "P") for sp in SPLITS) + " |")
    lines += ["", "## What this does and does not change", "",
              "- The shrinkage diagnosis of ml_study_v1 (N and M's tuning prefers the strongest penalty and the pipeline "
              "moves with planted signals) concerned N and M, whose rows are one per session in date order: it stands "
              "as a diagnosis of those arms.",
              "- The pooled candidate's inner selection was a real coding defect. ml_study_v1's statement that the "
              "models look like A 'not because of a bug' is too broad for P; P's development numbers there are the "
              "'before' column above.",
              "- No candidate is promoted by this study. p1_ml_forward_v2 and its registered artifacts are unchanged.",
              "", f"Predict stage {manifest['at']} at `{manifest['code_revision']}`; scored {res['at']}."]
    return "\n".join(lines) + "\n"
