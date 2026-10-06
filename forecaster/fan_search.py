# forecaster/fan_search.py
"""
The multiple-comparison review of the intermarket experiment's development search
(docs/fan_experiment.md): many candidates met the same check sessions, so the best of them
looks better than it is. Two tests on aligned per-session losses (the checks' stored runs,
forecaster/fan_harness.py), each against one benchmark:

  spa(d)     Hansen's test for superior predictive ability (2005): is ANY candidate better
             than the benchmark? The studentised maximum of the candidates' mean loss
             differences against its bootstrap distribution under the null, recentred three
             ways - lower, consistent (the test's recommended p-value) and upper (White's
             reality check, the most conservative)
  stepm(d)   Romano and Wolf's stepwise test (2005): WHICH candidates are better than the
             benchmark, with the probability of naming any wrongly held at alpha

d[k, t] = benchmark loss - candidate k's loss in session t (positive: the candidate better).
Both resample whole sessions in moving blocks (the experiment's 5-session blocks, 2000
resamples, seed 7), the same index draws for every candidate so their correlation is kept.

Neither replaces the untouched holdout: they measure the search's selection among the runs
supplied - not the trials that cannot be rebuilt, nor choices made by looking at screens.

  aligned(runs, key, benchmark)   the loss differences from stored runs
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from contracts import fan as F


def block_indices(n: int, block: int, resamples: int, seed: int) -> np.ndarray:
    """(resamples, n) session indices: moving blocks of ``block`` consecutive sessions, concatenated and cut to n."""
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - block + 1, size=(resamples, math.ceil(n / block)))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(resamples, -1)
    return idx[:, :n]


def _boot_means(d: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """(resamples, k): each candidate's mean difference in every resample."""
    return d[:, idx].mean(axis=2).T


def spa(d: np.ndarray, block: int = F.BOOTSTRAP["block_sessions"], resamples: int = F.BOOTSTRAP["resamples"],
        seed: int = F.BOOTSTRAP["seed"]) -> Dict[str, Any]:
    """Hansen's SPA on ``d`` (candidates x sessions): the statistic and the lower / consistent / upper p-values."""
    k, n = d.shape
    idx = block_indices(n, block, resamples, seed)
    mean = d.mean(axis=1)
    boot = _boot_means(d, idx)
    omega = np.sqrt(n) * boot.std(axis=0)
    ok = omega > 0
    if not ok.any():
        raise ValueError("every candidate's difference is constant: nothing to test")
    t = np.sqrt(n) * mean[ok] / omega[ok]
    stat = max(0.0, float(t.max()))
    threshold = np.sqrt(omega[ok] ** 2 / n * 2 * np.log(np.log(n)))
    recentre = {"lower": np.maximum(mean[ok], 0.0),
                "consistent": np.where(mean[ok] >= -threshold, mean[ok], 0.0),
                "upper": mean[ok]}
    p = {}
    for name, g in recentre.items():
        z = np.sqrt(n) * (boot[:, ok] - g[None, :]) / omega[ok][None, :]
        p[name] = float(np.mean(np.maximum(z.max(axis=1), 0.0) >= stat))
    return {"statistic": stat, "p": p, "candidates": int(ok.sum()), "sessions": n,
            "t": [float(x) if o else None for x, o in zip(np.sqrt(n) * mean / np.where(ok, omega, 1.0), ok)]}


def stepm(d: np.ndarray, alpha: float = 0.05, block: int = F.BOOTSTRAP["block_sessions"],
          resamples: int = F.BOOTSTRAP["resamples"], seed: int = F.BOOTSTRAP["seed"]) -> Dict[str, Any]:
    """Romano and Wolf's studentised StepM on ``d``: the candidates better than the benchmark (family-wise error
    ``alpha``), with each step's critical value."""
    k, n = d.shape
    idx = block_indices(n, block, resamples, seed)
    mean = d.mean(axis=1)
    boot = _boot_means(d, idx)
    omega = np.sqrt(n) * boot.std(axis=0)
    t = np.where(omega > 0, np.sqrt(n) * mean / np.where(omega > 0, omega, 1.0), -np.inf)
    z = np.sqrt(n) * (boot - mean[None, :]) / np.where(omega > 0, omega, 1.0)[None, :]
    active = [i for i in range(k) if omega[i] > 0]
    rejected: List[int] = []
    steps = []
    while active:
        crit = float(np.quantile(z[:, active].max(axis=1), 1 - alpha))
        new = [i for i in active if t[i] > crit]
        steps.append({"critical": crit, "rejected": new})
        if not new:
            break
        rejected += new
        active = [i for i in active if i not in new]
    return {"better": rejected, "steps": steps, "t": [float(x) for x in t], "alpha": alpha}


def aligned(runs: Dict[str, Dict[str, Any]], key: str, benchmark: Optional[str] = None
            ) -> Tuple[List[str], np.ndarray, List[str]]:
    """``(names, d, sessions)`` from stored checks runs (name -> run_checks result with 'per_session'): d[k, t] =
    benchmark loss - run k's loss at ``key`` in session t, on the sessions every run scored. The benchmark is the
    baseline the runs were scored against (None) or one of the runs by name. Raises ValueError when two runs scored
    different baselines."""
    days = sorted(set.intersection(*[set(r["per_session"]) for r in runs.values()]))
    base = None
    for name, r in runs.items():
        b = np.array([r["per_session"][d][key][0] for d in days])
        if base is None:
            base = b
        elif not np.allclose(b, base, rtol=1e-9, atol=0):
            raise ValueError(f"{name} was scored against another baseline")
    bench = base if benchmark is None else np.array([runs[benchmark]["per_session"][d][key][1] for d in days])
    names = [n for n in runs if n != benchmark]
    d = np.array([bench - np.array([runs[n]["per_session"][x][key][1] for x in days]) for n in names])
    return names, d, days


# --------------------------------------------------------------------------
# The review of the stored runs
# --------------------------------------------------------------------------

EXCLUDED = {"identity"}                  # the baseline itself: its differences are zero by construction
# Development trials that left no rebuildable definition (docs/fan_experiment.md lists their results).
NOT_REBUILT = (
    "ablation G: iv_rv from the previous session's VXN close (a prototype column)",
    "HAR: NQ's 1- and 22-day realised variance beside rv5d (prototype columns)",
    "semivariance: NQ's falling- and rising-price variance over 15 / 60 / 240 minutes (prototype columns)",
    "HAR and semivariance together (prototype columns)",
    "a calibration and shrinkage layer a x m^b chosen on the last 30 training sessions (review round 1)",
    "E's shape refitted on its training rows' own residuals (review round 1, in sample)",
    "E with training origins at minute 1-4 of the five (the same model: a robustness check)",
    "a linear Poisson model on VXN's level and NQ's 5-day realised variance only (review round 1)",
    "never scored, screened by rank correlation only: premarket volume since 04:00 (QQQ, SPY, SMH) and NQ's realised "
    "variance against ES's and SMH's (15, 60, 240 minutes) - the screen that picked iv_rv",
)
INVALIDATED = (
    "review round 1's constant-scale benchmark: it chose its scale with the normal's quantiles and was scored with "
    "v2's real shape (replaced by crps_scale)",
    "the first HAR / semivariance run: a selector took every new column into every variant (rerun correctly; not "
    "rebuilt, above)",
)


def _cross_market(run: Dict[str, Any], target: str) -> bool:
    return any(not (n.startswith("base.") or n.startswith(f"{target}.")) for n in (run.get("features") or []))


def review(runs: Dict[str, Dict[str, Any]], key: str) -> Dict[str, Any]:
    """SPA and StepM over ``runs`` against v2 (every run), and against gbm_own (the runs that read another
    instrument) when it is among them."""
    out: Dict[str, Any] = {}
    names, d, days = aligned(runs, key)
    base = np.array([next(iter(runs.values()))["per_session"][x][key][0] for x in days])
    s, w = spa(d), stepm(d)
    out["v2"] = {"benchmark": "fan_rw_v2", "sessions": len(days), "spa": s,
                 "better": [names[i] for i in w["better"]], "steps": w["steps"],
                 "runs": [{"name": n, "diff_bps": float(-d[i].mean()), "share": float(-d[i].mean() / base.mean()),
                           "t": w["t"][i]} for i, n in enumerate(names)]}
    target = next(iter(runs.values()))["target"]
    cross = {n: r for n, r in runs.items() if _cross_market(r, target)}
    if "gbm_own" in runs and cross:
        sub = {"gbm_own": runs["gbm_own"], **cross}
        names2, d2, days2 = aligned(sub, key, benchmark="gbm_own")
        s2, w2 = spa(d2), stepm(d2)
        own = np.array([runs["gbm_own"]["per_session"][x][key][1] for x in days2])
        out["own"] = {"benchmark": "gbm_own", "sessions": len(days2), "spa": s2,
                      "better": [names2[i] for i in w2["better"]], "steps": w2["steps"],
                      "runs": [{"name": n, "diff_bps": float(-d2[i].mean()), "share": float(-d2[i].mean() / own.mean()),
                                "t": w2["t"][i]} for i, n in enumerate(names2)]}
    else:
        out["own"] = None
    return out


def write_report(res: Dict[str, Any], runs: Dict[str, Dict[str, Any]], experiment: str, target: str, key: str,
                 report_dir: str) -> str:
    """docs/reports/fan_search_<experiment>_<target>.md: the review, the runs it covers and the ones it cannot."""
    import os
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"fan_search_{experiment}_{target}.md")
    v = res["v2"]
    L = [f"# The development search under review: {experiment}, {target}, {key[1:]} minutes", "",
         f"{len(runs)} stored checks runs, each on the checks' {v['sessions']} sessions; the loss is a session's mean "
         "CRPS. **SPA** (Hansen 2005) asks whether *any* run is better than the benchmark once the search is taken "
         "into account - the consistent p-value is the test's, the upper one White's reality check; **StepM** "
         "(Romano and Wolf 2005) names the runs that are, holding the chance of naming any wrongly at 5 %. Both "
         "resample whole sessions in 5-session blocks (2000 resamples). Development only: neither replaces the "
         "sealed holdout, and neither sees the trials below that left no rebuildable definition, nor the choices "
         "made by looking at screens.", ""]
    for part in ("v2", "own"):
        r = res.get(part)
        if r is None:
            continue
        title = ("Against fan_rw_v2 - is any run better than the baseline?" if part == "v2" else
                 "Against gbm_own - does any run that reads another market beat NQ's own features?")
        L += [f"## {title}", "",
              f"SPA statistic {r['spa']['statistic']:.2f}; p lower {r['spa']['p']['lower']:.3f}, **consistent "
              f"{r['spa']['p']['consistent']:.3f}**, upper {r['spa']['p']['upper']:.3f}. StepM names "
              + (", ".join(f"`{n}`" for n in r["better"]) or "none") + f" ({len(r['steps'])} step(s); first critical "
              f"value {r['steps'][0]['critical']:.2f}).", "",
              "| Run | Where it came from | Features | Difference (bps) | Share | t | StepM |", "|---|---|---|---|---|---|---|"]
        for row in sorted(r["runs"], key=lambda x: x["diff_bps"]):
            run = runs[row["name"]]
            spec = (run.get("provenance") or {}).get("spec") or {}
            src = spec.get("note") or ("reference" if spec.get("kind") == "reference" else "candidate")
            feats = run.get("features")
            fdesc = "-" if feats is None else f"{len(feats)} (`{run.get('features_hash')}`)"
            L.append(f"| `{row['name']}` | {src} | {fdesc} | {row['diff_bps']:+.5f} | {100 * row['share']:+.2f} % | "
                     f"{row['t']:+.2f} | {'better' if row['name'] in r['better'] else '-'} |")
        L.append("")
    L += ["## Trials not in the test", "", "Left no rebuildable definition (their results are in "
          "docs/fan_experiment.md):", ""] + [f"- {x}" for x in NOT_REBUILT]
    L += ["", "Invalidated:", ""] + [f"- {x}" for x in INVALIDATED] + [""]
    with open(path, "w") as fh:
        fh.write("\n".join(L))
    return path
