# forecaster/ml_bundle_eval.py
"""
The seven-target bundles' development evaluation (contracts/nq_ml_bundle.EVALUATION),
ml_bundle_eval_v1: nested chronological folds by session date, every target scored first on its own,
the predictions frozen with their sha256 before an outcome is read.

    python scripts/nq_journal.py ml-bundle-eval predict     # fits and outer predictions, frozen
    python scripts/nq_journal.py ml-bundle-eval score       # checks the files, reads the outcomes, reports

  predict(data)    the development comparison's outer folds (an initial 120 sessions, blocks of 20, one
                   embargoed session) over the NQ pool's dates; in each, every variant is selected and
                   fitted on the fold's training dates only - a view of the dataset whose other dates'
                   labels are blanked, so a test label can never reach a fit - and predicts the test
                   sessions' NQ rows target by target (or says why not):
                     N, M, P         the bundles as they would be trained
                     <arm>_sym       the direction targets' symmetric comparator: q learned, r = 0.5
                     P_complete      P with every pooling strength forced to 0 (complete pooling)
                     P_nqonly        P's selected family and parameters refitted on NQ's rows only
  score(conn)      refuses changed prediction files, then reads every target's outcome and A's and B's
                   stored runs: per variant and target on paired dates the unhalved multiclass Brier
                   score, log loss, calibration (per-class reliability, ECE), label coverage, availability,
                   paired differences against A and B (moving-block bootstrap of per-session differences,
                   95 % and the Bonferroni level over the 21 arm-target claims), per-class diagnostics,
                   dispersion and disagreement, the symmetric comparators, the ablations (M - N, P -
                   P_nqonly, P - P_complete), and sample sizes from the development variability

Development data only: every session was inspected before. A claim met here is a candidate for a
separately reviewed prospective protocol (docs/reports/ml_bundle_forward_proposal.md), not a finding.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from fractions import Fraction
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from contracts import nq_forecast as fc
from contracts import nq_ml as ml
from contracts import nq_ml_bundle as mb
from contracts import nq_prompt_v2 as defs
from forecaster import ml_bundle as mbun
from forecaster import ml_split as sp

VERSION = "ml_bundle_eval_v1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "reports", VERSION)
REPORT = os.path.join(ROOT, "docs", "reports", f"{VERSION}.md")
ARMS = ("N", "M", "P")
VARIANTS = ("N", "M", "P", "N_sym", "M_sym", "P_sym", "P_complete", "P_nqonly")
UNC = {"block": 5, "resamples": 2000, "seed": 20261012}
CLAIMS = len(ARMS) * len(mb.TARGETS)                  # 21: the primary family
ALPHA = 0.05
BONFERRONI = 1 - ALPHA / CLAIMS
BINS = 10


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# predict
# --------------------------------------------------------------------------

def restricted(data, dates: Sequence[str]):
    """A view of ``data`` whose labels and moves outside ``dates`` are blanked: what a fit on those dates may read."""
    from dataclasses import replace
    keep = np.isin(data.dates, list(dates))
    labels = {t: pd.Series([v if k else None for v, k in zip(s, keep)], dtype=object) for t, s in data.labels.items()}
    moves = {t: pd.Series(np.where(keep, s.to_numpy(float), np.nan)) for t, s in data.moves.items()}
    return replace(data, labels=labels, moves=moves)


def outer_folds(days: Sequence[str]) -> List[sp.DateFold]:
    d = ml.DEV_EVALUATION
    return sp.outer_folds(days, d["initial_train_sessions"], d["test_block_sessions"], d["embargo_sessions"])


def _fit_variant(view, variant: str, train: Sequence[str], fitted: Dict[str, mbun.Bundle]) -> mbun.Bundle:
    arm = variant[0]
    if variant in ARMS:
        return mbun.fit_bundle(view, arm, train)
    if variant.endswith("_sym"):
        return mbun.fit_bundle(view, arm, train, targets=tuple(mb.DIRECTION),
                               configs=lambda t: mbun.configurations(t, arm, families=("decomp_sym",)),
                               calibration=False)
    if variant == "P_complete":
        return mbun.fit_bundle(view, "P", train, configs=lambda t: mbun.configurations(t, "P", gammas=(0.0,)))
    if variant == "P_nqonly":
        base = fitted["P"]
        heads = {}
        for t, h in base.heads.items():
            if h.status != "trained":
                heads[t] = mbun.Head(t, "P", "unavailable", reason=f"P's head unavailable: {h.reason}")
                continue
            params = {k: v for k, v in h.params.items() if k != "gamma"}
            heads[t] = mbun.fit_head(view, t, "P", train, fixed=(h.family, params), population=("NQ",),
                                     calibration=False)
        return mbun.Bundle("P_nqonly", "P", heads, base.training)
    raise ValueError(f"unknown variant {variant!r}")


def fold_job(data, fold: sp.DateFold, variants: Sequence[str] = VARIANTS) -> Tuple[List[Dict[str, Any]], Dict]:
    """One outer fold (in a worker process): every variant fitted on the training dates' view, its predictions for
    the test dates' NQ rows; ``(predictions, log)``."""
    sp.check(fold)
    t0 = time.time()
    train = list(fold.train)
    view = restricted(data, train)
    rows = [i for i, (d, s) in enumerate(zip(data.dates, data.instruments)) if s == "NQ" and d in set(fold.test)]
    X = data.rows.iloc[rows]
    own_missing = X.reindex(columns=mb.OWN).isna().all(axis=1).to_numpy()
    abstain = X.get("ctx_required_missing", pd.Series(0.0, index=X.index)).to_numpy(float) > 0
    fitted: Dict[str, mbun.Bundle] = {}
    out, log = [], {"fold": fold.describe(), "variants": {}}
    for v in variants:
        b = _fit_variant(view, v, train, fitted)
        fitted[v] = b
        log["variants"][v] = {t: {"status": h.status, "reason": h.reason, "family": h.family, "params": h.params,
                                  "calibration": h.calibration, "n": h.n, "rows": h.rows,
                                  "unseen": h.unseen} for t, h in b.heads.items()}
        for t, head in b.heads.items():
            elig = data.eligibility[t].iloc[rows].tolist()
            P = None
            if head.status == "trained":
                P = head.predict(X)
            for k, (i, d) in enumerate(zip(rows, X["date"])):
                why = ("NQ's own features are all missing" if own_missing[k] else
                       "a required context instrument unusable: M abstains" if v[0] == "M" and abstain[k] else
                       elig[k] if elig[k] is not None else
                       None if head.status == "trained" else f"head not trained: {head.reason}")
                rec = {"date": d, "variant": v, "target": t, "status": "unavailable" if why else "predicted",
                       "reason": why}
                if not why:
                    rec["p"] = [float(f"{x:.12g}") for x in P[k]]
                out.append(rec)
    log["seconds"] = round(time.time() - t0, 1)
    return out, log


def predict(data, out_dir: str = OUT, jobs: int = 1, variants: Sequence[str] = VARIANTS, progress=print
            ) -> Dict[str, Any]:
    """Every outer fold (in parallel worker processes when ``jobs`` > 1), the predictions written with their sha256
    before any outcome is read; returns the manifest."""
    from joblib import Parallel, delayed
    from forecaster.provenance import code_revision
    t0 = time.time()
    folds = outer_folds(data.days)
    if not folds:
        raise ValueError(f"{len(data.days)} sessions: no outer fold after the initial "
                         f"{ml.DEV_EVALUATION['initial_train_sessions']}")
    done = (Parallel(n_jobs=min(jobs, len(folds)))(delayed(fold_job)(data, f, variants) for f in folds)
            if jobs > 1 else [fold_job(data, f, variants) for f in folds])
    preds, logs = [], []
    for p, log in done:
        preds += p
        logs.append(log)
        progress(f"fold {log['fold']['test'][0]}..{log['fold']['test'][1]}: {log['seconds']} s")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "predictions.jsonl")
    preds.sort(key=lambda r: (r["date"], r["variant"], r["target"]))
    with open(path, "w", encoding="utf-8") as f:
        for r in preds:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    with open(os.path.join(out_dir, "folds.json"), "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=1, sort_keys=True, default=str)
    test = sorted({d for f in folds for d in f.test})
    manifest = {"version": VERSION, "stage": "predict", "at": _now(), "code_revision": code_revision(),
                "protocol": {"evaluation": mb.EVALUATION, "budget": mb.BUDGET, "pooling": mb.POOLING,
                             "calibration": mb.CALIBRATION, "smoothing": mb.SMOOTHING, "split": ml.SPLIT,
                             "variants": list(variants), "claims": CLAIMS, "bonferroni_level": BONFERRONI},
                "feature_version": mb.FEATURE_VERSION, "label_version": defs.LABEL_VERSION,
                "folds": [f.describe() for f in folds], "test_span": [test[0], test[-1]],
                "sessions": len(data.days), "rows": {s: int(np.sum(data.instruments == s))
                                                     for s in sorted(set(data.instruments))},
                "coverage": data.coverage(), "data_digest": mbun.data_digest(data, data.days),
                "files": {"predictions.jsonl": _sha(path), "folds.json": _sha(os.path.join(out_dir, "folds.json"))},
                "seconds": round(time.time() - t0, 1)}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True, default=str)
    return manifest


# --------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------

def stored_runs(conn, snaps: Sequence[Dict[str, Any]], algorithm: str) -> Dict[str, Dict[str, np.ndarray]]:
    """target -> day -> the class probabilities (canonical order) of the algorithm's first issued historical-replay run
    of that snapshot."""
    by_snap = {s["snapshot_id"]: str(s["session_date"]) for s in snaps}
    out: Dict[str, Dict[str, np.ndarray]] = {t: {} for t in mb.TARGETS}
    rows = conn.execute(
        "SELECT r.snapshot_id, p.target, p.distribution FROM journal.forecast_runs r JOIN journal.forecast_predictions "
        "p USING (run_id) WHERE r.algorithm_version = %s AND r.mode = 'historical_replay' AND r.lifecycle_status = "
        "'issued' AND p.distribution IS NOT NULL ORDER BY r.issued_at;", (algorithm,)).fetchall()
    for sid, target, dist in rows:
        day = by_snap.get(str(sid))
        if day is None or target not in out or day in out[target]:
            continue
        d = dist if isinstance(dist, dict) else json.loads(dist)
        out[target][day] = np.array([float(Fraction(d[c])) for c in mb.CLASSES[target]])
    return out


def _brier(p: np.ndarray, y: str, classes: Sequence[str]) -> float:
    return float(sum((p[j] - (1.0 if c == y else 0.0)) ** 2 for j, c in enumerate(classes)))


def _logloss(p: np.ndarray, y: str, classes: Sequence[str]) -> float:
    q = float(p[list(classes).index(y)])
    return math.inf if q <= 0 else -math.log(q)


def boot(diffs: Sequence[float], levels: Sequence[float] = (0.95, BONFERRONI)) -> Dict[str, Any]:
    """Moving-block bootstrap of the mean of date-ordered per-session differences (sessions the unit): the mean, its
    bootstrap standard error and percentile intervals at ``levels``; no interval with fewer than two blocks."""
    d = np.asarray(diffs, float)
    n, b = len(d), UNC["block"]
    out: Dict[str, Any] = {"n": n, "mean": float(d.mean()) if n else None, "se": None, "intervals": {}}
    if n < 2 * b:
        return out
    rng = np.random.default_rng(UNC["seed"])
    k = int(math.ceil(n / b))
    starts = rng.integers(0, n - b + 1, size=(UNC["resamples"], k))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(UNC["resamples"], -1)[:, :n]
    means = np.sort(d[idx].mean(axis=1))
    out["se"] = float(means.std(ddof=1))
    for lv in levels:
        lo = means[int(math.floor((1 - lv) / 2 * (len(means) - 1)))]
        hi = means[int(math.ceil((1 + lv) / 2 * (len(means) - 1)))]
        out["intervals"][f"{lv:.4f}"] = [float(lo), float(hi)]
    return out


def calibration(P: np.ndarray, y: Sequence[str], classes: Sequence[str]) -> Dict[str, Any]:
    out, eces = {}, []
    for j, c in enumerate(classes):
        p = P[:, j]
        o = np.array([1.0 if v == c else 0.0 for v in y])
        idx = np.clip(np.digitize(p, np.linspace(0, 1, BINS + 1)) - 1, 0, BINS - 1)
        bins, ece = [], 0.0
        for b in range(BINS):
            m = idx == b
            if m.any():
                bins.append({"bin": b, "n": int(m.sum()), "predicted": float(p[m].mean()), "observed": float(o[m].mean())})
                ece += m.sum() / len(p) * abs(p[m].mean() - o[m].mean())
        out[c] = {"n_realised": int(o.sum()), "mean_predicted": float(p.mean()), "observed": float(o.mean()),
                  "brier_component": float(((p - o) ** 2).mean()), "ece": float(ece), "bins": bins}
        eces.append(ece)
    return {"classes": out, "ece_mean": float(np.mean(eces))}


def sample_size(sigma: Optional[float], delta: float, margin: float = 0.0, k: int = CLAIMS,
                power: float = 0.8) -> Optional[int]:
    """Sessions for a two-sided Bonferroni (``k`` claims) test to have ``power`` when the true improvement is
    ``delta`` and the claim is that it exceeds ``margin`` (0: some improvement): n = ((z_a + z_b) sigma / (delta -
    margin))^2, sigma the per-session standard deviation inflated for serial dependence (bootstrap SE x sqrt(n))."""
    if sigma is None or delta <= margin:
        return None
    z = NormalDist().inv_cdf(1 - ALPHA / (2 * k)) + NormalDist().inv_cdf(power)
    return int(math.ceil((z * sigma / (delta - margin)) ** 2))


def _symmetric(p: np.ndarray, target: str) -> np.ndarray:
    """A direction distribution with its bullish and bearish shares replaced by their mean (no direction view)."""
    c = mb.CLASSES[target]
    q = p.copy()
    m = (p[c.index("bullish")] + p[c.index("bearish")]) / 2
    q[c.index("bullish")] = q[c.index("bearish")] = m
    return q


def score(conn, out_dir: str = OUT, report_path: Optional[str] = REPORT, progress=print) -> Dict[str, Any]:
    """Checks the prediction files against the manifest, then reads the outcomes (see the module docstring)."""
    from database import journal_store as store
    from features import calendar as cal
    from forecaster.ml_train import pool
    with open(os.path.join(out_dir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    for name, sha in manifest["files"].items():
        if _sha(os.path.join(out_dir, name)) != sha:
            raise RuntimeError(f"{name} changed after the predict stage recorded it: refusing to score")
    preds: Dict[str, Dict[str, Dict[str, np.ndarray]]] = {}
    unavailable: Dict[str, Dict[str, Dict[str, int]]] = {}
    with open(os.path.join(out_dir, "predictions.jsonl"), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r["status"] == "predicted":
                preds.setdefault(r["variant"], {}).setdefault(r["target"], {})[r["date"]] = np.array(r["p"])
            else:
                u = unavailable.setdefault(r["variant"], {}).setdefault(r["target"], {})
                key = (r["reason"] or "").split(":")[0]
                u[key] = u.get(key, 0) + 1
    snaps = pool(conn)
    history = store.outcome_history(conn, defs.LABEL_VERSION)      # the outcomes: read only now
    y: Dict[str, Dict[str, Optional[str]]] = {t: {} for t in mb.TARGETS}
    reasons: Dict[str, Dict[str, Optional[str]]] = {t: {} for t in mb.TARGETS}
    for s in snaps:
        revs = history.get(s["snapshot_id"]) or []
        labs = revs[-1]["labels"] if revs else {}
        for t in mb.TARGETS:
            lab = (labs.get(t) or {}).get("label")
            y[t][str(s["session_date"])] = lab if isinstance(lab, str) else None
            reasons[t][str(s["session_date"])] = None if isinstance(lab, str) else ((labs.get(t) or {}).get("reason")
                                                                                     or "no outcome")
    base = {"A": stored_runs(conn, snaps, fc.PRIOR_VERSION), "B": stored_runs(conn, snaps, fc.BASELINE_VERSION)}
    base["A_sym"] = {t: ({d: _symmetric(p, t) for d, p in base["A"][t].items()} if t in mb.DIRECTION else {})
                     for t in mb.TARGETS}
    test0, test1 = manifest["test_span"]
    scheduled = [s.session_date.isoformat() for s in cal.sessions_between(test0, test1) if s.is_open]
    res: Dict[str, Any] = {"version": VERSION, "at": _now(), "scheduled": len(scheduled), "test_span": [test0, test1],
                           "targets": {}}
    for t in mb.TARGETS:
        cls = mb.CLASSES[t]
        labelled = [d for d in scheduled if isinstance(y[t].get(d), str)]
        cov = {"scheduled": len(scheduled), "labelled": len(labelled),
               "without_label": _counts([reasons[t].get(d) or "no snapshot" for d in scheduled
                                         if not isinstance(y[t].get(d), str)])}
        arms_all = {**{v: preds.get(v, {}).get(t, {}) for v in VARIANTS}, **{b: base[b][t] for b in base}}
        entry: Dict[str, Any] = {"coverage": cov, "variants": {}, "pairs": {}, "claims": {}, "ablations": {},
                                 "dispersion": {}, "sample_size": {}}
        for v, p in arms_all.items():
            days = [d for d in labelled if d in p]
            if not days:
                entry["variants"][v] = {"n": 0, "available": sum(1 for d in scheduled if d in p),
                                        "unavailable": unavailable.get(v, {}).get(t, {})}
                continue
            P = np.array([p[d] for d in days])
            ys = [y[t][d] for d in days]
            ll = [_logloss(p[d], y[t][d], cls) for d in days]
            fin = [x for x in ll if math.isfinite(x)]
            entry["variants"][v] = {"n": len(days), "available": sum(1 for d in scheduled if d in p),
                                    "brier": float(np.mean([_brier(p[d], y[t][d], cls) for d in days])),
                                    "logloss": float(np.mean(fin)) if fin else None, "logloss_infinite": len(ll) - len(fin),
                                    "calibration": calibration(P, ys, cls),
                                    "unavailable": unavailable.get(v, {}).get(t, {})}
        pairs = [(v, b) for v in VARIANTS for b in ("A", "B")] + [("B", "A")]
        if t in mb.DIRECTION:
            pairs += [(a, f"{a}_sym") for a in ARMS] + [(a, "A_sym") for a in ARMS] + [("A_sym", "A")]
        for a, b in pairs:
            pa, pb = arms_all.get(a, {}), arms_all.get(b, {})
            common = [d for d in labelled if d in pa and d in pb and (b not in ("A", "B") or
                                                                       all(d in arms_all[x] for x in ("A", "B")))]
            diffs = [_brier(pa[d], y[t][d], cls) - _brier(pb[d], y[t][d], cls) for d in common]
            entry["pairs"][f"{a} - {b}"] = boot(diffs)
        for a in ARMS:                                           # the primary claims: below zero against A AND B
            ia = entry["pairs"][f"{a} - A"]["intervals"].get(f"{BONFERRONI:.4f}")
            ib = entry["pairs"][f"{a} - B"]["intervals"].get(f"{BONFERRONI:.4f}")
            entry["claims"][a] = {"met": bool(ia and ib and ia[1] < 0 and ib[1] < 0), "vs_A": ia, "vs_B": ib,
                                  "level": BONFERRONI}
            for b in ("A", "B"):
                pr = entry["pairs"][f"{a} - {b}"]
                sigma = None if pr["se"] is None else pr["se"] * math.sqrt(pr["n"])
                entry["sample_size"][f"{a} - {b}"] = {
                    "sigma_per_session": sigma,
                    "some_improvement": {str(dl): sample_size(sigma, dl) for dl in (0.005, 0.01, 0.02)},
                    "exceeds_0.01": {str(dl): sample_size(sigma, dl, 0.01) for dl in (0.02, 0.03)}}
        for a, b in (("M", "N"), ("P", "P_nqonly"), ("P", "P_complete")):
            pa, pb = arms_all.get(a, {}), arms_all.get(b, {})
            common = [d for d in labelled if d in pa and d in pb]
            diffs = [_brier(pa[d], y[t][d], cls) - _brier(pb[d], y[t][d], cls) for d in common]
            entry["ablations"][f"{a} - {b}"] = {**boot(diffs, (0.95,)), **_dispersion(pa, pb, common)}
        for a in ARMS:
            pa = arms_all.get(a, {})
            common = [d for d in scheduled if d in pa and d in arms_all["A"]]
            entry["dispersion"][a] = {"vs_A": _dispersion(pa, arms_all["A"], common),
                                      "max_probability_sd": float(np.std([pa[d].max() for d in pa])) if pa else None}
        for a, b in (("N", "M"), ("N", "P"), ("M", "P")):
            pa, pb = arms_all.get(a, {}), arms_all.get(b, {})
            entry["dispersion"][f"{a} vs {b}"] = _dispersion(pa, pb, [d for d in scheduled if d in pa and d in pb])
        res["targets"][t] = entry
        progress(f"{t}: scored")
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, sort_keys=True, default=str)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(markdown(res, manifest))
    return res


def _counts(xs) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for x in xs:
        k = str(x).split(":")[0]
        out[k] = out.get(k, 0) + 1
    return out


def _dispersion(pa: Dict[str, np.ndarray], pb: Dict[str, np.ndarray], days: Sequence[str]) -> Dict[str, Any]:
    """Mean total-variation distance between two arms' distributions and how often their most probable classes
    differ, on ``days``."""
    if not days:
        return {"n": 0, "tv": None, "argmax_disagreement": None}
    tv = [0.5 * float(np.abs(pa[d] - pb[d]).sum()) for d in days]
    dis = [int(np.argmax(pa[d]) != np.argmax(pb[d])) for d in days]
    return {"n": len(days), "tv": float(np.mean(tv)), "argmax_disagreement": float(np.mean(dis))}


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _f(x, nd=4) -> str:
    return "-" if x is None else f"{x:+.{nd}f}"


def _iv(iv) -> str:
    return "-" if not iv else f"[{iv[0]:+.4f}, {iv[1]:+.4f}]"


def markdown(res: Dict[str, Any], manifest: Dict[str, Any]) -> str:
    lv95 = "0.9500"
    lines = [f"# Seven-target ML bundles: development evaluation ({VERSION})", "",
             "**Development data, not a test.** Every session was inspected before. A claim met here is a candidate "
             "for a separately reviewed prospective protocol (ml_bundle_forward_proposal.md), never a promotion.", "",
             f"- **Design:** {len(manifest['folds'])} outer folds by session date, test span {res['test_span'][0]} to "
             f"{res['test_span'][1]} ({res['scheduled']} scheduled sessions); every variant selected and fitted on its "
             "fold's training dates only; predictions frozen with their sha256 before scoring "
             f"(predict stage {manifest['at']} at `{manifest['code_revision']}`).",
             f"- **Primary claims:** {CLAIMS} (3 arms x 7 targets). A claim is met when the arm's per-session Brier "
             f"difference has its {100 * BONFERRONI:.2f} % interval (Bonferroni) below zero against both A and B. "
             "Everything else is descriptive at 95 %.",
             "- **Scores:** unhalved multiclass Brier (0 to 2, lower is better), on the paired sessions with a "
             "classifiable label; coverage and availability are over every scheduled test session.", "",
             "## Results matrix (Brier difference against A / B, 95 % interval; claim at the Bonferroni level)", "",
             "| target | label coverage | " + " | ".join(f"{a} - A" for a in ARMS) + " | "
             + " | ".join(f"{a} - B" for a in ARMS) + " | claims met |",
             "|---|---:|" + "---|" * (2 * len(ARMS)) + "---|"]
    for t, e in res["targets"].items():
        cov = e["coverage"]
        cells = [f"{_f(e['pairs'][f'{a} - {b}']['mean'])} {_iv(e['pairs'][f'{a} - {b}']['intervals'].get(lv95))}"
                 for b in ("A", "B") for a in ARMS]
        met = [a for a in ARMS if e["claims"][a]["met"]]
        lines.append(f"| {t} | {cov['labelled']} / {cov['scheduled']} | " + " | ".join(cells) + f" | "
                     f"{', '.join(met) or 'none'} |")
    for t, e in res["targets"].items():
        lines += ["", f"## {t} ({mb.FIELD[t]})", "",
                  f"Label coverage {e['coverage']['labelled']} of {e['coverage']['scheduled']} scheduled sessions; "
                  "without a label: " + (", ".join(f"{k} {v}" for k, v in e["coverage"]["without_label"].items())
                                          or "none") + ".", "",
                  "| variant | n | available | Brier | log loss | ECE | - A | - B |", "|---|---:|---:|---:|---:|---:|---|---|"]
        for v, s in e["variants"].items():
            if not s.get("n"):
                lines.append(f"| {v} | 0 | {s.get('available', 0)} | - | - | - | - | - |")
                continue
            pa, pb = e["pairs"].get(f"{v} - A"), e["pairs"].get(f"{v} - B")
            lines.append(f"| {v} | {s['n']} | {s['available']} | {s['brier']:.4f} | "
                         f"{'-' if s['logloss'] is None else f'{s['logloss']:.4f}'} | "
                         f"{s['calibration']['ece_mean']:.3f} | "
                         + (f"{_f(pa['mean'])} {_iv(pa['intervals'].get(lv95))}" if pa else "-") + " | "
                         + (f"{_f(pb['mean'])} {_iv(pb['intervals'].get(lv95))}" if pb else "-") + " |")
        lines += ["", "Claims (" + f"{100 * BONFERRONI:.2f} %): " + "; ".join(
            f"{a} {'met' if c['met'] else 'not met'} (vs A {_iv(c['vs_A'])}, vs B {_iv(c['vs_B'])})"
            for a, c in e["claims"].items()) + "."]
        if t in mb.DIRECTION:
            lines += ["", "Direction beyond size (negative: the arm's direction view helps): " + "; ".join(
                f"{a} - {a}_sym {_f(e['pairs'][f'{a} - {a}_sym']['mean'])} "
                f"{_iv(e['pairs'][f'{a} - {a}_sym']['intervals'].get(lv95))}" for a in ARMS)
                + f". A's own tilt: A_sym - A {_f(e['pairs']['A_sym - A']['mean'])} "
                  f"{_iv(e['pairs']['A_sym - A']['intervals'].get(lv95))}."]
        lines += ["", "Ablations (95 %): " + "; ".join(
            f"{k} {_f(v['mean'])} {_iv(v['intervals'].get(lv95))} (TV {_f(v['tv'], 3)}, argmax differs "
            f"{'-' if v['argmax_disagreement'] is None else f'{100 * v['argmax_disagreement']:.0f} %'})"
            for k, v in e["ablations"].items()) + "."]
        rare = {v: {c: d for c, d in s["calibration"]["classes"].items() if d["n_realised"] < 15}
                for v, s in e["variants"].items() if s.get("n") and v in ARMS}
        if any(rare.values()):
            lines += ["", "Rare classes (fewer than 15 realised): " + "; ".join(
                f"{v} {c}: {d['n_realised']} realised, mean p {d['mean_predicted']:.3f}, observed {d['observed']:.3f}"
                for v, cs in rare.items() for c, d in cs.items()) + "."]
        ss = e["sample_size"]
        lines += ["", "Sessions needed (80 % power, Bonferroni over 21) for a true improvement of 0.01: " + "; ".join(
            f"{k} {ss[k]['some_improvement'].get('0.01') or '-'}" for k in ss) + "."]
    lines += ["", "## Reading", "",
              "- A claim met is development evidence only; the prospective protocol decides.",
              "- An arm can help one target and not another: results are per target first. No aggregate is reported.",
              "- Fallbacks are not ML predictions: a variant's unavailable sessions count against its availability, "
              "never as a prediction."]
    return "\n".join(lines) + "\n"
