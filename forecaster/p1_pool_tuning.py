# forecaster/p1_pool_tuning.py
"""
p1_pool_tuning_v1 (contracts/p1_pool_tuning.py): arm B's analogue probabilities with
their pool size K and smoothing k chosen walk-forward on earlier sessions only, against
arm A's frequencies and arm B as registered (K = 5, k = 5). Development data.

  ordered_analogues(target, records)   every earlier comparable session, most similar
                                       first (nq_match_p1_v2's rubric, not cut at five)
  distribution(labels, prior, k)       (count + k x prior) / (n + k)
  run(conn)                            the scores, the tuned choices and the paired
                                       differences - written by scripts/p1_pool_tuning.py
"""

from __future__ import annotations

import math
from collections import Counter
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from contracts import p1_pool_tuning as spec
from database import journal_store as store
from forecaster import journal
from matching import structural as ms


def ordered_analogues(target: ms.Record, records: Sequence[ms.Record]) -> List[ms.Record]:
    """Every earlier session ``target`` can be compared with, most similar first - the matcher's eligibility, floor
    and tie order, without the cut at five."""
    eligible = [r for r in records if r.session_date < target.session_date and r.symbol == target.symbol
                and (r.snapshot_version, r.protocol_version) == (target.snapshot_version, target.protocol_version)
                and r.integrity_status == "ok"]
    once = Counter(r.session_date for r in eligible)
    scored = []
    for r in eligible:
        if once[r.session_date] != 1:
            continue
        sim, comp = ms.score(ms.compare(target, r))
        if comp >= pre.MIN_COMPARABLE:
            scored.append((sim, comp, r))
    scored.sort(key=lambda c: (-c[0], -c[1], -int(c[2].session_date.replace("-", "")), c[2].snapshot_id))
    return [r for _, _, r in scored]


def distribution(labels: Sequence[Optional[str]], prior: Dict[str, float], k: float,
                 classes: Sequence[str]) -> Dict[str, float]:
    """(count + k x prior) / (n + k) over the labelled ``labels``; the prior alone when there is none."""
    have = [x for x in labels if x is not None]
    n = len(have)
    counts = Counter(have)
    if n + k == 0:
        return dict(prior)
    return {c: (counts[c] + k * prior.get(c, 0.0)) / (n + k) for c in classes}


def brier(dist: Dict[str, float], realised: str, classes: Sequence[str]) -> float:
    return sum((dist.get(c, 0.0) - (1.0 if c == realised else 0.0)) ** 2 for c in classes)


def log_loss(dist: Dict[str, float], realised: str) -> float:
    p = dist.get(realised, 0.0)
    return math.inf if p <= 0 else -math.log(p)


def _bootstrap(values: Sequence[float]) -> Dict[str, Any]:
    from forecaster.rth_eval import block_bootstrap
    b = spec.BOOTSTRAP
    return block_bootstrap(values, b["block"], b["resamples"], b["seed"], b["level"])


def run(conn, profile: str = defs.DEFAULT_PROFILE) -> Dict[str, Any]:
    version = defs.PROFILES[profile].snapshot_version
    snaps = store.list_snapshots(conn, "2000-01-01", "2100-01-01", version)
    records = journal.annotated_records(conn, snaps, pre.RULES_PROTOCOL_VERSION)
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    labels = {sid: (revs[-1]["labels"] if revs else None) for sid, revs in history.items()}

    def label(snapshot_id: str, target: str) -> Optional[str]:
        return ((labels.get(snapshot_id) or {}).get(target) or {}).get("label")
    sessions = sorted(records, key=lambda r: r.session_date)
    all_snaps = sorted(snaps, key=lambda s: str(s["session_date"]))
    cells = [(K, k) for K in spec.POOL_SIZES for k in spec.PSEUDO_COUNTS]
    out: Dict[str, Any] = {"version": spec.VERSION, "definition_hash": spec.definition_hash(), "sessions": len(sessions),
                           "targets": {}}
    ordered_cache: Dict[str, List[ms.Record]] = {}
    for target_name in pre.OUTCOME_TARGETS:
        classes = list(defs.TARGETS[target_name]["labels"])
        rows = []                                    # per scored session: realised class and every arm's distribution
        for r in sessions:
            y = label(r.snapshot_id, target_name)
            earlier = [label(str(s["snapshot_id"]), target_name) for s in all_snaps
                       if str(s["session_date"]) < r.session_date]
            earlier = [x for x in earlier if x is not None]
            if y is None or not earlier:
                continue
            prior = {c: earlier.count(c) / len(earlier) for c in classes}
            if r.snapshot_id not in ordered_cache:
                ordered_cache[r.snapshot_id] = ordered_analogues(r, records)
            ordered = ordered_cache[r.snapshot_id]
            dists = {"A": prior}
            for K, k in cells:
                dists[(K, k)] = distribution([label(a.snapshot_id, target_name) for a in ordered[:K]], prior, k,
                                             classes)
            rows.append({"session_date": r.session_date, "y": y, "dists": dists})
        # walk-forward: each session's cell chosen on the earlier rows only
        chosen, tuned_rows = Counter(), []
        for i, row in enumerate(rows):
            if i < spec.MIN_TRAINING:
                continue
            train = rows[:i]
            best = min(cells, key=lambda c: (sum(brier(t["dists"][c], t["y"], classes) for t in train) / len(train),
                                             c[0], c[1]))
            chosen[f"K={best[0]}, k={best[1]}"] += 1
            tuned_rows.append({**row, "tuned": row["dists"][best]})
        scored = {"A": [], "B": [], "tuned": []}
        logs = {"A": [], "B": [], "tuned": []}
        for row in tuned_rows:
            for arm, dist in (("A", row["dists"]["A"]), ("B", row["dists"][(5, 5)]), ("tuned", row["tuned"])):
                scored[arm].append(brier(dist, row["y"], classes))
                logs[arm].append(log_loss(dist, row["y"]))
        diffs = lambda a, b: [x - y for x, y in zip(scored[a], scored[b])]
        out["targets"][target_name] = {
            "rows": len(rows), "tuned_sessions": len(tuned_rows),
            "first_tuned": tuned_rows[0]["session_date"] if tuned_rows else None,
            "brier": {arm: (sum(v) / len(v) if v else None) for arm, v in scored.items()},
            "log_loss_finite": {arm: (sum(x for x in v if math.isfinite(x)) / max(1, sum(math.isfinite(x) for x in v))
                                      if v else None) for arm, v in logs.items()},
            "infinite_log_loss": {arm: sum(not math.isfinite(x) for x in v) for arm, v in logs.items()},
            "tuned_minus_A": _bootstrap(diffs("tuned", "A")),
            "tuned_minus_B": _bootstrap(diffs("tuned", "B")),
            "B_minus_A": _bootstrap(diffs("B", "A")),
            "chosen": dict(chosen.most_common()),
        }
    primary = out["targets"][spec.PRIMARY_TARGET]["tuned_minus_A"]
    out["decision"] = ("candidate for a forward test: tuned analogues beat the frequencies on development data"
                       if primary["high"] is not None and primary["high"] < 0 else
                       "no sufficiently reliable improvement over the frequencies was established on development data")
    return out


def report(res: Dict[str, Any]) -> str:
    lines = [f"# {res['version']}", "",
             f"Definition `{res['definition_hash']}` (contracts/p1_pool_tuning.py, "
             f"docs/p1_pool_tuning_v1_definition.json), fixed and committed before this run. **Development data** "
             f"({res['sessions']} sessions): a candidate at most, never a result.", "",
             f"**Decision (primary, {spec.PRIMARY_TARGET}, tuned minus A):** {res['decision']}.", "",
             "Mean multiclass Brier (lower is better) over the walk-forward-tuned sessions; differences are paired per "
             "session with a 95 % block-bootstrap interval.", "",
             "| target | sessions | A (frequencies) | B (K=5, k=5) | tuned | tuned - A | tuned - B | B - A | infinite log loss A / B / tuned | cells chosen (most often) |",
             "|---|---:|---:|---:|---:|---|---|---|---|---|"]
    iv = lambda d: "-" if d["mean"] is None else f"{d['mean']:+.4f} [{d['low']:+.4f}, {d['high']:+.4f}]"
    for t, r in res["targets"].items():
        b = r["brier"]
        f = lambda x: "-" if x is None else f"{x:.4f}"
        inf = r["infinite_log_loss"]
        top = ", ".join(f"{c} ({n})" for c, n in list(r["chosen"].items())[:2])
        lines.append(f"| `{t}` | {r['tuned_sessions']} | {f(b['A'])} | {f(b['B'])} | {f(b['tuned'])} | "
                     f"{iv(r['tuned_minus_A'])} | {iv(r['tuned_minus_B'])} | {iv(r['B_minus_A'])} | "
                     f"{inf['A']} / {inf['B']} / {inf['tuned']} | {top} |")
    return "\n".join(lines) + "\n"
