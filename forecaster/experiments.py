# forecaster/experiments.py
"""
Registered forecast experiments (guideline revision 2, stage 4).

  experiment_manifest(...)   the immutable manifest (4A): session range, profile,
                             label / convention / snapshot / annotation / matcher
                             versions, the arms (4B: A the earlier-session prior, B the
                             rules, matcher and smoothed analogue baseline), the
                             official-run rule, outcome revisions, the primary target
                             and metric, companions, eligibility, the zero-probability
                             policy, calibration and the uncertainty method
  register_experiment(...)   stores it as a definition of kind 'experiment' - before
                             any result exists; a changed manifest needs a new name
  freeze_cases(...)          per scheduled session and arm, once: the official run the
                             rule chooses and the outcome revision it is scored
                             against, or why there is none (journal.experiment_cases)
  score_experiment(...)      the frozen cases scored (4D), stored with the code revision
                             (journal.experiment_results), and the report

Scores (4D), computed in code from the stored runs and outcomes:
  log loss     -ln p(realised class), natural log. No floor, no clipping: a realised
               class issued with probability 0 has infinite loss; such cases are
               counted, and the mean is reported over the finite cases beside them
  Brier        the unhalved multiclass sum, sum_c (p_c - [c realised])^2, in [0, 2]
  accuracy     the issued class equal to the realised one; an ambiguous prediction (an
               exact tie) has no class and is counted apart

A case counts for a target when its realised label is not null (an ambiguous first
move, an uncovered session type or a missing threshold stays outside the denominator,
counted by reason) and the arm issued a distribution. Arms are compared on the cases
both cover - paired session differences B - A with a moving-block bootstrap interval
(temporal dependence), and broken down by realised class, calendar month and the
session's daily-ATR tercile, so a gain concentrated in one class or period shows.
Directional accuracy says nothing about profitability: no execution, costs or
strategy is tested here.

Sessions already used to calibrate the structure rules or to inspect label
disagreements are development data (4C), whatever an experiment calls them; the
decisive test is prospective.
"""

from __future__ import annotations

import csv
import math
import os
import random
from collections import Counter, defaultdict
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from contracts import nq_forecast as fc
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from forecaster.provenance import code_revision

PRIMARY_TARGET = "direction_15m"
ARMS = {"A": fc.PRIOR_VERSION, "B": fc.BASELINE_VERSION}
QUESTIONS = {"A": "the earlier-session prior: does additional structure beat a simple baseline?",
             "B": "rules-only structure, the P1 matcher and the smoothed analogue forecast: does the implemented "
                  "numerical procedure add predictive value?"}
OFFICIAL_RUN_RULES = {
    "first": "the first issued run of the session, profile and arm by the database issue time",
    "latest": "the last issued run of the session, profile and arm when the cases are frozen",
    "first_timely": "the first issued run acknowledged by its deadline (live runs only)",
}
DEVELOPMENT_NOTE = ("development data (guideline 4C): the structure rules were calibrated and the label "
                    "disagreements inspected on these sessions, so the result identifies clear failures and a "
                    "modest candidate; it is not a test - that is prospective")
UNCERTAINTY = {"method": "moving-block bootstrap of the paired session differences (date order)",
               "block_sessions": 5, "resamples": 2000, "seed": 20261004, "interval": 0.95}
RELIABILITY_BINS = 10


# --------------------------------------------------------------------------
# Manifest and registration (4A)
# --------------------------------------------------------------------------

def experiment_manifest(name: str, start: str, end: str, profile: str = defs.DEFAULT_PROFILE,
                        purpose: str = "development", official_run: str = "first", mode: str = "historical_replay",
                        protocol: str = pre.RULES_PROTOCOL_VERSION,
                        arms: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The manifest of an experiment (see the module docstring); every choice is fixed here, before any score."""
    arms = arms or ARMS
    if official_run not in OFFICIAL_RUN_RULES:
        raise ValueError(f"official_run must be one of {sorted(OFFICIAL_RUN_RULES)}")
    if official_run == "first_timely" and mode != "live":
        raise ValueError("first_timely applies to live runs only")
    if purpose not in ("development", "test"):
        raise ValueError("purpose must be development or test")
    # the arms computed from the evidence (fc.ALGORITHMS) and the synthesis, which Claude issues on it
    unknown = [a for a in arms.values() if a not in fc.ALGORITHMS and a != fc.SYNTHESIS_VERSION]
    if unknown:
        raise ValueError(f"unknown forecast algorithm(s): {', '.join(unknown)}")
    targets = [t for _, t in fc.FORECAST_TARGETS]
    return {
        "name": name, "guideline": "Nasdaq 100 Forecast Implementation Guideline, revision 2, stage 4",
        "purpose": purpose,
        "purpose_note": DEVELOPMENT_NOTE if purpose == "development" else
        "test data: sessions not used to build, calibrate or inspect any arm",
        "sessions": {"from": start, "to": end, "calendar_version": cal.CALENDAR_VERSION,
                     "rule": "every scheduled session in [from, to]"},
        "profile": profile, "snapshot_version": defs.PROFILES[profile].snapshot_version,
        "convention_version": defs.CONVENTION_VERSION, "label_version": defs.LABEL_VERSION,
        "annotation_protocol": protocol, "matcher_version": pre.MATCHER_VERSION,
        "forecast_schema": fc.FORECAST_SCHEMA_VERSION, "mode": mode, "issue_policy": fc.ISSUE_POLICIES[mode],
        "arms": {k: {"algorithm": v, "question": QUESTIONS.get(k, "")} for k, v in arms.items()},
        "comparison": "each arm against arm A, paired on the cases both cover",
        "official_run": {"rule": official_run, "text": OFFICIAL_RUN_RULES[official_run]},
        "outcomes": "each case is scored against the latest outcome revision under the label version when the "
                    "cases are frozen; the revision is recorded per case and never changes after",
        "primary": {"target": PRIMARY_TARGET, "metric": "multiclass log loss (natural log), lower is better"},
        "companions": ["multiclass Brier sum (unhalved), lower is better",
                       "accuracy of the issued class (ambiguous predictions counted apart)",
                       "class-wise counts, mean probability of the realised class and accuracy"],
        "secondary_targets": [t for t in targets if t != PRIMARY_TARGET],
        "eligibility": "a case counts for a target when its realised label is not null and the arm issued a "
                       "distribution; unlabelled cases are counted by reason, never scored as a class; paired "
                       "comparisons use the cases both arms cover",
        "zero_probability": "no floor or clipping at scoring: a realised class issued with probability 0 has "
                            "infinite log loss; such cases are counted and the mean is taken over the finite ones "
                            "(Brier is always finite)",
        "calibration": "none: the arms' distributions are scored as issued",
        "uncertainty": dict(UNCERTAINTY),
        "breakdowns": ["realised class", "calendar month", "daily ATR tercile (volatility)"],
        "reliability": {"target": PRIMARY_TARGET, "bins": RELIABILITY_BINS},
        "profitability": "not tested - no execution, costs or trading strategy; directional accuracy says nothing "
                         "about profit",
    }


def register_experiment(conn, manifest: Dict[str, Any]) -> bool:
    """Registers the manifest (kind 'experiment'); True when new, False when the identical manifest is already
    registered; a different manifest under the same name raises journal_store.VersionConflict."""
    for version in [manifest["label_version"], manifest["snapshot_version"], manifest["convention_version"],
                    manifest["annotation_protocol"], manifest["matcher_version"], manifest["issue_policy"],
                    manifest["forecast_schema"]] + [a["algorithm"] for a in manifest["arms"].values()]:
        if store.get_version(conn, version) is None:
            raise ValueError(f"{version} is not registered: run the journal (catch-up) first")
    return store.register_version(conn, defs._record(manifest["name"], "experiment", manifest))


def load_manifest(conn, name: str) -> Dict[str, Any]:
    rec = store.get_version(conn, name)
    if rec is None or rec["kind"] != "experiment":
        raise ValueError(f"no registered experiment {name!r}")
    return rec["definition"]


# --------------------------------------------------------------------------
# Cases (the official-run rule, frozen once)
# --------------------------------------------------------------------------

def official_run(runs: Sequence[Dict[str, Any]], rule: str) -> Optional[Dict[str, Any]]:
    """The run a session's arm is scored by, from its issued runs (oldest issue first)."""
    if rule == "first_timely":
        from forecaster.forecast_service import timely
        runs = [r for r in runs if timely(r)]
    if not runs:
        return None
    return runs[-1] if rule == "latest" else runs[0]


def build_cases(conn, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per scheduled session and arm: the official run and the outcome revision it is scored against."""
    s, rule = manifest["sessions"], manifest["official_run"]["rule"]
    history = store.outcome_history(conn, manifest["label_version"])
    by_arm: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for arm, spec in manifest["arms"].items():
        days: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for run in store.issued_runs(conn, s["from"], s["to"], manifest["profile"], manifest["mode"],
                                     spec["algorithm"], manifest["label_version"]):
            days[run["session_date"]].append(run)
        by_arm[arm] = days
    cases = []
    for session in cal.sessions_between(s["from"], s["to"]):
        day = session.session_date.isoformat()
        for arm, spec in manifest["arms"].items():
            run = official_run(by_arm[arm].get(day, []), rule)
            case = {"session_date": day, "arm": arm, "run_id": None, "snapshot_id": None, "outcome_revision": None,
                    "status": "no_run", "detail": f"no issued {spec['algorithm']} run ({rule})"}
            if run is not None:
                revisions = history.get(run["snapshot_id"]) or []
                case.update(run_id=run["run_id"], snapshot_id=run["snapshot_id"])
                if revisions:
                    case.update(outcome_revision=int(revisions[-1]["outcome_revision"]), status="case", detail=None)
                else:
                    case.update(status="no_outcome", detail="no outcome recorded for the run's snapshot")
            cases.append(case)
    return cases


def freeze_cases(conn, name: str) -> Tuple[int, bool]:
    """Freezes the experiment's cases (once); ``(count, created)``."""
    return store.freeze_experiment_cases(conn, name, build_cases(conn, load_manifest(conn, name)))


# --------------------------------------------------------------------------
# Scores (pure)
# --------------------------------------------------------------------------

def log_loss(dist: Dict[str, Fraction], realised: str) -> float:
    p = dist[realised]
    return math.inf if p == 0 else -math.log(float(p))


def brier(dist: Dict[str, Fraction], realised: str) -> float:
    return float(sum((p - (1 if c == realised else 0)) ** 2 for c, p in dist.items()))


def case_score(prediction: Optional[Dict[str, Any]], realised: Dict[str, Any]) -> Dict[str, Any]:
    """One case and target: why it does not count, or its scores."""
    if realised.get("label") is None:
        return {"status": "no_label", "reason": realised.get("reason") or "unknown"}
    if prediction is None or prediction.get("distribution") is None:
        return {"status": "no_distribution", "reason": (prediction or {}).get("status", "no prediction")}
    dist = {c: Fraction(v) for c, v in prediction["distribution"].items()}
    label = realised["label"]
    return {"status": "scored", "realised": label, "p": float(dist[label]), "dist": {c: float(v) for c, v in dist.items()},
            "log_loss": log_loss(dist, label), "brier": brier(dist, label),
            "classed": prediction["status"] == "predicted", "ambiguous": prediction["status"] == "ambiguous_prediction",
            "correct": prediction["status"] == "predicted" and prediction["predicted_label"] == label}


def block_bootstrap(diffs: Sequence[float], block: int, resamples: int, seed: int,
                    interval: float) -> Optional[Tuple[float, float]]:
    """A moving-block bootstrap percentile interval of the mean of date-ordered paired differences; None with fewer
    than two blocks of data (every resample would be the whole sample: no interval, not a degenerate one)."""
    n = len(diffs)
    block = max(1, block)
    if n < 2 * block:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        sample: List[float] = []
        while len(sample) < n:
            start = rng.randrange(0, n - block + 1)
            sample.extend(diffs[start:start + block])
        means.append(sum(sample[:n]) / n)
    means.sort()
    lo = means[int(math.floor((1 - interval) / 2 * (resamples - 1)))]
    hi = means[int(math.ceil((1 + interval) / 2 * (resamples - 1)))]
    return lo, hi


def _mean(xs: Iterable[float]) -> Optional[float]:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def _r(x: Optional[float], nd: int = 6):
    return None if x is None else (x if math.isinf(x) else round(x, nd))


def arm_summary(scores: List[Dict[str, Any]], runs: List[Optional[Dict[str, Any]]],
                uses_analogues: bool = True) -> Dict[str, Any]:
    """One arm and target over its cases: coverage, scores and the evidence behind them (the analogue count and
    comparable weight only for an arm that uses the analogues)."""
    status = Counter(s["status"] for s in scores)
    no_label = Counter(s["reason"] for s in scores if s["status"] == "no_label")
    no_dist = Counter(s["reason"] for s in scores if s["status"] == "no_distribution")
    done = [s for s in scores if s["status"] == "scored"]
    finite = [s["log_loss"] for s in done if not math.isinf(s["log_loss"])]
    classed = [s for s in done if s["classed"]]
    members = [len(r["evidence"].get("members") or []) for r in runs if r and r.get("evidence")] \
        if uses_analogues else []
    weights = [float(m["comparable_weight"]) for r in runs if r and r.get("evidence")
               for m in r["evidence"].get("members") or []] if uses_analogues else []
    return {"cases": len(scores), "scored": len(done), "no_label": dict(no_label), "no_distribution": dict(no_dist),
            "log_loss_mean_finite": _r(_mean(finite)), "log_loss_finite": len(finite),
            "log_loss_infinite": len(done) - len(finite), "brier_mean": _r(_mean(s["brier"] for s in done)),
            "classed": len(classed), "ambiguous": sum(1 for s in done if s["ambiguous"]),
            "correct": sum(1 for s in classed if s["correct"]),
            "accuracy": _r(sum(1 for s in classed if s["correct"]) / len(classed)) if classed else None,
            "mean_analogues": _r(_mean(members), 3), "mean_comparable_weight": _r(_mean(weights), 3),
            "statuses": dict(status)}


def paired(base: Dict[str, Dict[str, Any]], other: Dict[str, Dict[str, Any]], days: Sequence[str],
           uncertainty: Dict[str, Any]) -> Dict[str, Any]:
    """``other`` - ``base`` on the sessions both scored, in date order: Brier, finite log loss, accuracy."""
    common = [d for d in days if base.get(d, {}).get("status") == "scored" and other.get(d, {}).get("status") == "scored"]
    boot = lambda xs: block_bootstrap(xs, uncertainty["block_sessions"], uncertainty["resamples"], uncertainty["seed"],
                                      uncertainty["interval"])
    brier_diffs = [other[d]["brier"] - base[d]["brier"] for d in common]
    both_finite = [d for d in common if not math.isinf(base[d]["log_loss"]) and not math.isinf(other[d]["log_loss"])]
    ll_diffs = [other[d]["log_loss"] - base[d]["log_loss"] for d in both_finite]
    classed = [d for d in common if base[d]["classed"] and other[d]["classed"]]
    out = {"common": len(common),
           "brier": {"base": _r(_mean(base[d]["brier"] for d in common)),
                     "other": _r(_mean(other[d]["brier"] for d in common)),
                     "diff": _r(_mean(brier_diffs)), "interval": [_r(x) for x in boot(brier_diffs) or ()] or None},
           "log_loss": {"both_finite": len(both_finite),
                        "infinite_base_only": sum(1 for d in common if math.isinf(base[d]["log_loss"])
                                                  and not math.isinf(other[d]["log_loss"])),
                        "infinite_other_only": sum(1 for d in common if math.isinf(other[d]["log_loss"])
                                                   and not math.isinf(base[d]["log_loss"])),
                        "infinite_both": sum(1 for d in common if math.isinf(base[d]["log_loss"])
                                             and math.isinf(other[d]["log_loss"])),
                        "base": _r(_mean(base[d]["log_loss"] for d in both_finite)),
                        "other": _r(_mean(other[d]["log_loss"] for d in both_finite)),
                        "diff": _r(_mean(ll_diffs)), "interval": [_r(x) for x in boot(ll_diffs) or ()] or None},
           "accuracy": {"both_classed": len(classed),
                        "base": _r(_mean(1.0 if base[d]["correct"] else 0.0 for d in classed)),
                        "other": _r(_mean(1.0 if other[d]["correct"] else 0.0 for d in classed))}}
    return out


def _groups(days: Sequence[str], key) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for d in days:
        out[key(d)].append(d)
    return dict(sorted(out.items()))


def breakdown(by_arm: Dict[str, Dict[str, Dict[str, Any]]], groups: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Per group: cases every arm scored, each arm's mean Brier, finite log loss and accuracy."""
    rows = []
    for name, days in groups.items():
        common = [d for d in days if all(by_arm[a].get(d, {}).get("status") == "scored" for a in by_arm)]
        row = {"group": name, "common": len(common)}
        for arm, scores in by_arm.items():
            finite = [scores[d]["log_loss"] for d in common if not math.isinf(scores[d]["log_loss"])]
            classed = [d for d in common if scores[d]["classed"]]
            row[arm] = {"brier": _r(_mean(scores[d]["brier"] for d in common)), "log_loss_finite": _r(_mean(finite)),
                        "infinite": len(common) - len(finite),
                        "accuracy": _r(_mean(1.0 if scores[d]["correct"] else 0.0 for d in classed))}
        rows.append(row)
    return rows


def reliability(scores: Dict[str, Dict[str, Any]], days: Sequence[str], classes: Sequence[str],
                bins: int = RELIABILITY_BINS) -> Dict[str, List[Dict[str, Any]]]:
    """Per class: the issued probability in equal bins against the observed frequency of the class."""
    out = {}
    for c in classes:
        cells = defaultdict(list)
        for d in days:
            s = scores.get(d)
            if s and s["status"] == "scored":
                p = s["dist"][c]
                cells[min(int(p * bins), bins - 1)].append((p, 1.0 if s["realised"] == c else 0.0))
        out[c] = [{"bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "n": len(v), "mean_p": _r(_mean(p for p, _ in v), 4),
                   "observed": _r(_mean(o for _, o in v), 4)} for b, v in sorted(cells.items())]
    return out


# --------------------------------------------------------------------------
# Scoring an experiment
# --------------------------------------------------------------------------

def score_experiment(conn, name: str) -> Dict[str, Any]:
    """Scores the frozen cases (freezing them first if needed) and returns the results; nothing is stored here."""
    manifest = load_manifest(conn, name)
    freeze_cases(conn, name)
    cases = store.experiment_cases(conn, name)
    history = store.outcome_history(conn, manifest["label_version"])
    arms = list(manifest["arms"])
    targets = [manifest["primary"]["target"]] + manifest["secondary_targets"]
    runs: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {a: {} for a in arms}
    outcome: Dict[Tuple[str, str], Dict[str, Any]] = {}
    status = {a: Counter() for a in arms}
    for c in cases:
        status[c["arm"]][c["status"]] += 1
        if c["status"] != "case":
            continue
        runs[c["arm"]][c["session_date"]] = store.get_forecast_run(conn, c["run_id"])
        labels = next((o["labels"] for o in history.get(c["snapshot_id"], [])
                       if int(o["outcome_revision"]) == c["outcome_revision"]), None)
        outcome[(c["arm"], c["session_date"])] = labels
    days = sorted({c["session_date"] for c in cases})
    same_set = sum(1 for d in days if len({(runs[a].get(d) or {}).get("analogue_set_id") for a in arms}) == 1
                   and all(runs[a].get(d) for a in arms))

    results: Dict[str, Any] = {"experiment": name, "manifest_hash": defs._record(name, "experiment", manifest)
                               ["definition_hash"], "sessions": len(days),
                               "cases": {a: dict(status[a]) for a in arms},
                               "pairs_on_the_same_analogue_set": same_set, "targets": {}}
    per_target_scores: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
    for target in targets:
        by_arm: Dict[str, Dict[str, Dict[str, Any]]] = {}
        entry: Dict[str, Any] = {"arms": {}, "paired": {}}
        for arm in arms:
            scores = {}
            for d, run in runs[arm].items():
                labels = outcome.get((arm, d))
                if labels is not None:
                    scores[d] = case_score(run["predictions"].get(target), labels[target])
            by_arm[arm] = scores
            entry["arms"][arm] = arm_summary(list(scores.values()), [runs[arm][d] for d in scores],
                                             manifest["arms"][arm]["algorithm"] == fc.BASELINE_VERSION)
        # the manifest's own pairs (arm minus base), else each arm against the first - as every earlier manifest
        for arm, base in manifest.get("pairs") or [(a, arms[0]) for a in arms[1:]]:
            entry["paired"][f"{arm}-{base}"] = paired(by_arm[base], by_arm[arm], days, manifest["uncertainty"])
        vocab = list(defs.TARGETS[target]["labels"])
        common = [d for d in days if all(by_arm[a].get(d, {}).get("status") == "scored" for a in arms)]
        entry["classes"] = []
        for cls in vocab:
            rows = [d for d in common if by_arm[arms[0]][d]["realised"] == cls]
            entry["classes"].append({"class": cls, "n": len(rows), **{
                arm: {"mean_p_realised": _r(_mean(by_arm[arm][d]["p"] for d in rows), 4),
                      "accuracy": _r(_mean(1.0 if by_arm[arm][d]["correct"] else 0.0
                                           for d in rows if by_arm[arm][d]["classed"]), 4)} for arm in arms}})
        results["targets"][target] = entry
        per_target_scores[target] = by_arm

    primary = manifest["primary"]["target"]
    scored = per_target_scores[primary]
    results["primary"] = {"target": primary, "metric": manifest["primary"]["metric"],
                          "paired": results["targets"][primary]["paired"]}
    results["monthly"] = breakdown(scored, _groups(days, lambda d: d[:7]))
    atr = {}
    for d in days:
        run = next((runs[a].get(d) for a in arms if runs[a].get(d)), None)
        a = ((run or {}).get("evidence") or {}).get("thresholds", {}).get("A")
        if a is not None:
            atr[d] = float(Fraction(str(a)))
    ordered = sorted(atr, key=lambda d: (atr[d], d))
    cut = [ordered[len(ordered) * k // 3] for k in (1, 2)] if len(ordered) >= 3 else []
    tercile = lambda d: ("no daily ATR" if d not in atr else "low" if not cut or atr[d] < atr[cut[0]]
                         else "middle" if atr[d] < atr[cut[1]] else "high")
    results["volatility"] = breakdown(scored, _groups(days, tercile))
    results["reliability"] = {arm: reliability(scored[arm], days, list(defs.TARGETS[primary]["labels"]))
                              for arm in arms}
    results["_case_scores"] = {t: {a: s for a, s in v.items()} for t, v in per_target_scores.items()}
    return results


def store_results(conn, name: str, results: Dict[str, Any]) -> str:
    """Stores a scoring (without the per-case detail, which the report's CSV keeps)."""
    body = {k: v for k, v in results.items() if not k.startswith("_")}
    return store.save_experiment_result(conn, name, _json_safe(body), code_revision())


def _json_safe(value):
    """Infinite floats as the string 'inf' (JSON has no infinity)."""
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _f(x, nd=4) -> str:
    if x is None:
        return "-"
    if isinstance(x, str) or (isinstance(x, float) and math.isinf(x)):
        return "inf"
    return f"{x:.{nd}f}"


def _ci(interval) -> str:
    return "-" if not interval else f"[{_f(interval[0])}, {_f(interval[1])}]"


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> List[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def write_report(manifest: Dict[str, Any], results: Dict[str, Any], directory: str, result_id: str) -> str:
    """docs/reports/experiment_<name>.md and .csv (per case and target); returns the markdown path."""
    os.makedirs(directory, exist_ok=True)
    name = manifest["name"]
    arms = list(manifest["arms"])
    lines = [f"# Experiment {name}", "",
             f"Scored by `forecaster/experiments.py` (result {result_id}, code {code_revision()}). The manifest was "
             f"registered before any score was computed; its hash is `{results['manifest_hash'][:16]}`.", "",
             f"- **Purpose:** {manifest['purpose']} - {manifest['purpose_note']}.",
             f"- **Sessions:** {manifest['sessions']['from']} to {manifest['sessions']['to']} ({results['sessions']} "
             f"scheduled), profile {manifest['profile']}, {manifest['mode'].replace('_', ' ')}.",
             f"- **Versions:** labels {manifest['label_version']}, snapshots {manifest['snapshot_version']}, "
             f"annotation {manifest['annotation_protocol']}, matcher {manifest['matcher_version']}.",
             "- **Arms:** " + "; ".join(f"{k} `{v['algorithm']}`" for k, v in manifest["arms"].items()) + ".",
             f"- **Official run:** {manifest['official_run']['text']}. Arms paired on the same analogue set: "
             f"{results['pairs_on_the_same_analogue_set']} sessions.",
             f"- **Primary:** {manifest['primary']['target']}, {manifest['primary']['metric']}. "
             f"{manifest['zero_probability'][0].upper()}{manifest['zero_probability'][1:]}.",
             f"- **Profitability:** {manifest['profitability']}.", "", "## Coverage", ""]
    lines += _table(["arm"] + ["case", "no_run", "no_outcome"],
                    [[a] + [results["cases"][a].get(k, 0) for k in ("case", "no_run", "no_outcome")] for a in arms])
    primary = results["primary"]
    lines += ["", f"## Primary: {primary['target']}", ""]
    for key, p in primary["paired"].items():
        ll, br, acc = p["log_loss"], p["brier"], p["accuracy"]
        lines += [f"Paired {key} on {p['common']} common sessions (negative favours {key.split('-')[0]}):", ""]
        lines += _table(["metric", arms[0], key.split("-")[0], "difference", f"{int(manifest['uncertainty']['interval'] * 100)}% interval", "n"], [
            ["log loss (both finite)", _f(ll["base"]), _f(ll["other"]), _f(ll["diff"]), _ci(ll["interval"]),
             ll["both_finite"]],
            ["Brier sum", _f(br["base"]), _f(br["other"]), _f(br["diff"]), _ci(br["interval"]), p["common"]],
            ["accuracy (both classed)", _f(acc["base"]), _f(acc["other"]), "-", "-", acc["both_classed"]]])
        lines += ["", f"Infinite log loss (a realised class issued with probability 0): {ll['infinite_base_only']} "
                      f"only in {arms[0]}, {ll['infinite_other_only']} only in {key.split('-')[0]}, "
                      f"{ll['infinite_both']} in both.", ""]
    lines += ["## Every target", ""]
    rows = []
    for target, entry in results["targets"].items():
        for arm in arms:
            s = entry["arms"][arm]
            rows.append([f"`{target}`", arm, s["scored"], _f(s["log_loss_mean_finite"]), s["log_loss_infinite"],
                         _f(s["brier_mean"]), _f(s["accuracy"]), s["ambiguous"],
                         ", ".join(f"{k} {v}" for k, v in sorted(s["no_label"].items())) or "-",
                         ", ".join(f"{k} {v}" for k, v in sorted(s["no_distribution"].items())) or "-",
                         _f(s["mean_analogues"], 2), _f(s["mean_comparable_weight"], 1)])
    lines += _table(["target", "arm", "scored", "log loss (finite)", "infinite", "Brier", "accuracy", "ambiguous",
                     "no realised label", "no distribution", "analogues", "comparable weight"], rows)
    lines += ["", "Paired differences, every target (no interval: fewer than two bootstrap blocks of pairs):", ""]
    rows = []
    for target, entry in results["targets"].items():
        for key, p in entry["paired"].items():
            rows.append([f"`{target}`", key, p["common"], _f(p["brier"]["diff"]), _ci(p["brier"]["interval"]),
                         _f(p["log_loss"]["diff"]), _ci(p["log_loss"]["interval"]), p["log_loss"]["both_finite"]])
    lines += _table(["target", "pair", "common", "Brier diff", "interval", "log loss diff", "interval",
                     "both finite"], rows)
    lines += ["", f"## Where the differences sit ({primary['target']})", "", "By realised class:", ""]
    lines += _table(["class", "n"] + [f"{a} mean p(realised)" for a in arms] + [f"{a} accuracy" for a in arms],
                    [[c["class"], c["n"]] + [_f(c[a]["mean_p_realised"]) for a in arms]
                     + [_f(c[a]["accuracy"]) for a in arms] for c in results["targets"][primary["target"]]["classes"]])
    for title, key in (("By calendar month", "monthly"), ("By daily-ATR tercile (volatility)", "volatility")):
        lines += ["", f"{title}:", ""]
        lines += _table(["group", "common"] + [f"{a} Brier" for a in arms] + [f"{a} log loss (finite)" for a in arms]
                        + [f"{a} accuracy" for a in arms],
                        [[r["group"], r["common"]] + [_f(r[a]["brier"]) for a in arms]
                         + [_f(r[a]["log_loss_finite"]) for a in arms] + [_f(r[a]["accuracy"]) for a in arms]
                         for r in results[key]])
    lines += ["", f"## Reliability ({primary['target']})", "",
              "The issued probability of each class in equal bins against how often the class was realised. Five "
              "analogues and a few hundred sessions do not establish calibration (4D).", ""]
    for arm in arms:
        for cls, rows in results["reliability"][arm].items():
            lines += [f"{arm}, {cls}: " + "; ".join(f"{r['bin']} n={r['n']} p={_f(r['mean_p'], 2)} "
                                                    f"obs={_f(r['observed'], 2)}" for r in rows)]
    path = os.path.join(directory, f"experiment_{name}.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(directory, f"experiment_{name}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["target", "arm", "session_date", "status", "realised", "p_realised", "log_loss", "brier",
                    "classed", "correct", "reason"])
        for target, by_arm in results["_case_scores"].items():
            for arm, scores in by_arm.items():
                for d, s in sorted(scores.items()):
                    w.writerow([target, arm, d, s["status"], s.get("realised", ""), _f(s.get("p"), 6) if "p" in s
                                else "", _f(s.get("log_loss"), 6) if "log_loss" in s else "",
                                _f(s.get("brier"), 6) if "brier" in s else "", s.get("classed", ""),
                                s.get("correct", ""), s.get("reason", "")])
    return path
