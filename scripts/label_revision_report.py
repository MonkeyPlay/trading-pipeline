#!/usr/bin/env python3
"""
Disagreement report for a label revision: the recorded ``nq_prompt_v2_1_impl3``
outcomes against the current label version (impl5) on the current snapshot
version (strict completeness, nq_conv_v5), session by session and target by
target, and the stored rule-based structure annotation against the current rules
protocol - before anything new is recorded.

    python scripts/label_revision_report.py            # writes docs/reports/label_disagreement_impl3_vs_impl5.{md,csv}

Reads only (the session is set read-only): the stored snapshots, outcomes and
annotations, and the bars. The new snapshots are built in memory and never saved.

Every difference is walked through one change at a time, each step changing one
thing, so the step where a label moves names its cause:

  stored       impl3 as recorded, on the stored snapshot (OLD_SNAPSHOTS)
  replay       impl3 rerun with its own code (git commit LEGACY_COMMIT) on today's bars
               - differs from the stored label only if the bars were revised since
  impl3 / new  impl3 on the snapshot built today under the current convention
               - the snapshot step: strict completeness, the frozen inputs
  impl5 / new  the current labels on that snapshot - the rule step: FL-v3 and its
               candidate list, OS-v2, LO-v2

The structure annotation goes the same way: stored (OLD_RULES) -> current rules
on the stored snapshot (the rule step) -> current rules on the new snapshot.
Exit status 1 when a difference has no named cause.
"""

import argparse
import csv
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from database.connection import get_db_connection
from features.nq_evidence import SnapshotError, build_snapshot
from forecaster import labels_prompt_v2 as labels
from forecaster import structure_rules as sr

LEGACY_COMMIT = "91ea3a5"
OLD_LABELS, OLD_SNAPSHOTS, OLD_RULES = "nq_prompt_v2_1_impl3", "nq_evidence_v3_r0929", "nq_structure_rules_v2"
PROFILE = "research_0929"
OUT = os.path.join(_PROJECT_ROOT, "docs", "reports", "label_disagreement_impl3_vs_impl5")
NEW_CANDIDATES = ("premarket_high", "premarket_low", "long_ma")      # impl4's additions to impl3's seven
LEVEL_INPUTS = ("on_high", "on_low", "prev_rth_high", "prev_rth_low", "prev_rth_close", "overnight_open")
THRESHOLD_OF = {t: d.get("threshold") for t, d in defs.TARGETS.items()}


# --------------------------------------------------------------------------
# impl3, from git history
# --------------------------------------------------------------------------

def load_impl3():
    """forecaster/labels_prompt_v2.py with its contract as they were at LEGACY_COMMIT."""
    def show(path):
        return subprocess.check_output(["git", "show", f"{LEGACY_COMMIT}:{path}"], cwd=_PROJECT_ROOT, text=True)
    root = tempfile.mkdtemp(prefix="legacy_impl3_")
    pkg = os.path.join(root, "legacy_impl3")
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    with open(os.path.join(pkg, "contract.py"), "w") as f:
        f.write(show("contracts/nq_prompt_v2.py"))
    with open(os.path.join(pkg, "labels.py"), "w") as f:
        f.write(show("forecaster/labels_prompt_v2.py").replace("from contracts import nq_prompt_v2 as defs",
                                                                "from legacy_impl3 import contract as defs"))
    sys.path.insert(0, root)
    module = importlib.import_module("legacy_impl3.labels")
    assert module.defs.LABEL_VERSION == OLD_LABELS, module.defs.LABEL_VERSION
    return module


# --------------------------------------------------------------------------
# Values and causes
# --------------------------------------------------------------------------

def shown(v) -> str:
    """A label, or its reason in parentheses."""
    return v["label"] if v["label"] is not None else f"({v['reason']})"


def _num(meas, key):
    v = meas.get(key)
    return "Unavailable" if v is None else str(v)


def snapshot_cause(target, old_out, new_out, old_payload, new_payload) -> str:
    """What the new snapshot changed that the target reads."""
    om, nm = old_out["measurements"], new_out["measurements"]
    diffs = []
    for key in ("T", "B", "A"):
        if om.get(key) != nm.get(key):
            status = {"A": new_payload["atr"]["daily"], "B": new_payload["atr"]["daily"],
                      "T": new_payload["atr"]["two_minute"]}[key]
            why = f" ({status.get('status')}: {status.get('detail', '')})".replace(": )", ")") \
                if nm.get(key) is None else ""
            diffs.append(f"{key} {_num(om, key)} -> {_num(nm, key)}{why}")
    old_refs, new_refs = old_payload.get("references") or {}, new_payload.get("references") or {}
    for name in LEVEL_INPUTS:
        a, b = old_refs.get(name) or {}, new_refs.get(name) or {}
        if (a.get("value"), a.get("status")) != (b.get("value"), b.get("status")):
            diffs.append(f"{name} {a.get('value') if a.get('status') == 'valid' else a.get('status')} -> "
                         f"{b.get('value') if b.get('status') == 'valid' else b.get('status')}")
    if om.get("vwap") != nm.get("vwap"):
        diffs.append(f"vwap {_num(om, 'vwap')} -> {_num(nm, 'vwap')}")
    return "snapshot: " + "; ".join(diffs) if diffs else ""


def _reached(detail) -> list:
    m = re.search(r"open reached: (.*)$", detail or "")
    return [x.strip() for x in m.group(1).split(",")] if m else []


def lo_cause(a, b) -> str:
    if b["label"] in ("break_acceptance", "break_reclaim_acceptance", "break_without_acceptance") and \
            (a["label"] in ("test_rejection", "break_reclaim_acceptance", "break_without_acceptance", "not_tested")
             or a["reason"] == "no_rejection"):
        return "LO-v2: a wick beyond the level is a breach"
    if a["label"] in ("break_acceptance", "break_reclaim_acceptance", "break_without_acceptance") and \
            b["label"] in ("break_acceptance", "break_reclaim_acceptance", "break_without_acceptance"):
        return "LO-v2: a wick beyond the level is a breach"
    return ""


def rule_cause(target, a, b, old_out, new_out) -> str:
    """Why impl3 and impl5 differ on the same snapshot ('' when no rule change explains it)."""
    if target == "first_level_tested":
        if b["label"] in NEW_CANDIDATES:
            return f"FL candidates: {defs.display(target, b['label'])} (not an impl3 candidate) reached first"
        if a["reason"] == "coincident_levels" and b["label"] is not None:
            return "FL: coincident levels named by precedence (impl3: unavailable)"
        if b["reason"] == "ambiguous_intrabar" and a["label"] is not None:
            new = [n for n in _reached(b["detail"]) if n in NEW_CANDIDATES]
            if new:
                return f"FL candidates: {', '.join(new)} on the other side of the open in the same bar - ambiguous"
        if b["reason"] == "missing_reference" and any(n in (b["detail"] or "") for n in NEW_CANDIDATES):
            return "FL candidates: a new candidate is unavailable - " + b["detail"].split(": ", 1)[-1]
        if a["reason"] == "none_tested" and b["label"] is None:
            return ""
        return ""
    if target == "first_level_outcome":
        fa, fb = old_out["labels"]["first_level_tested"], new_out["labels"]["first_level_tested"]
        if (fa["label"], fa["reason"]) != (fb["label"], fb["reason"]):
            return "follows the first level tested"
        return lo_cause(a, b)
    if target in defs.LEVEL_OUTCOME_REFERENCES:
        return lo_cause(a, b)
    if target == "opening_type_15m":
        if b["label"] in ("sweep_low_rebound", "sweep_high_reverse") and a["label"] != b["label"]:
            return "OS-v2: a level the opening gap crossed was swept by a later RTH breach and reclaim"
        if a["reason"] == "missing_bars" and "09:29" in (a["detail"] or ""):
            return "OS-v2: the 09:29 bar is no longer needed"
    return ""


def unavailable(p) -> str:
    """What a snapshot leaves unavailable (premarket undefined before nq_conv_v4 is not listed)."""
    notes = [f"{k} {p['atr'][k2]['status']}" for k, k2 in (("daily ATR", "daily"), ("2m ATR", "two_minute"))
             if p["atr"][k2]["status"] != "valid"]
    notes += [f"{n} {r['status']}" for n, r in p["references"].items()
              if r["status"] not in ("valid", "not_observed", "not_defined")]
    cov = p["bars"]["coverage"]
    if not cov.get("complete", float(cov["ratio"]) == 1):
        notes.append(f"overnight {cov.get('minutes', '?')} of {cov.get('expected_minutes', '?')} minutes")
    return "; ".join(notes)


def field_value(f) -> str:
    return str(f["value"]) if f["value"] is not None else f"({f['reason'] or f['status']})"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=Config.DATABASE_URL)
    ap.add_argument("--limit", type=int, help="first N sessions only (a quick look)")
    args = ap.parse_args(argv)

    conn = get_db_connection(args.db)
    conn.execute("SET default_transaction_read_only = on")
    impl3 = load_impl3()
    snaps = store.list_snapshots(conn, "2000-01-01", "2100-01-01", OLD_SNAPSHOTS)[:args.limit]
    rows, structure_rows, snapshot_notes = [], [], []
    checks = Counter()
    for i, old in enumerate(snaps, 1):
        day = str(old["session_date"])
        stored = store.latest_outcome(conn, old["snapshot_id"], OLD_LABELS)
        if stored is None:
            checks["no stored outcome"] += 1
            continue
        try:
            fresh = build_snapshot(conn, day, PROFILE)
        except SnapshotError as e:
            checks["no new snapshot"] += 1
            snapshot_notes.append((day, f"no snapshot: {e}"))
            continue
        new = {"snapshot_id": "in-memory", "session_date": day, "contract_id": fresh.contract_id,
               "payload": json.loads(defs.canonical_json(fresh.payload))}
        bars_old, bars_new = labels.load_realised_bars(conn, old), labels.load_realised_bars(conn, new)
        replay = impl3.compute_outcome(old, bars_old)
        impl3_new = impl3.compute_outcome(new, bars_new)
        impl5_new = labels.compute_outcome(new, bars_new)

        for target in defs.TARGETS:
            s0 = stored["labels"].get(target)
            s1, s2, s3 = replay["labels"].get(target), impl3_new["labels"].get(target), impl5_new["labels"][target]
            checks["session-targets"] += 1
            if s0 is None or s1 is None or s2 is None:               # a target impl3 does not have
                checks["targets new in impl5"] += 1
                continue
            if shown(s0) == shown(s1):
                checks["replay reproduces"] += 1
            if shown(s0) == shown(s3):
                continue
            causes = []
            if shown(s0) != shown(s1):
                causes.append("bars revised since the outcome was recorded")
            if shown(s1) != shown(s2):
                causes.append(snapshot_cause(target, replay, impl3_new, old["payload"], new["payload"])
                              or "snapshot: unexplained")
            if shown(s2) != shown(s3):
                causes.append(rule_cause(target, s2, s3, impl3_new, impl5_new) or "rules: unexplained")
            if not causes:
                causes.append("unexplained: intermediate steps agree")
            detail = s3["detail"] or ""
            if target == "first_level_tested" and impl5_new["measurements"].get("first_level_estimate"):
                detail = (f"estimate {impl5_new['measurements']['first_level_estimate']} at "
                          f"{impl5_new['measurements']['first_level_estimate_price']}; " + detail)
            rows.append({"session_date": day, "kind": "label", "target": target, "stored": shown(s0),
                         "replay": shown(s1), "old_rules_new_snapshot": shown(s2), "new": shown(s3),
                         "cause": " | ".join(causes), "detail": detail})

        old_ann = store.latest_annotation(conn, old["snapshot_id"], OLD_RULES)
        if old_ann is not None and old_ann["integrity_status"] == "ok":
            rules_old_snap, rules_new_snap = sr.annotate(old), sr.annotate(new)
            for name in pre.FIELDS:
                if name not in old_ann["fields"]:
                    continue
                v0, v1, v2 = (field_value(old_ann["fields"][name]), field_value(rules_old_snap["fields"][name]),
                              field_value(rules_new_snap["fields"][name]))
                checks["session-fields"] += 1
                if v0 == v2:
                    continue
                causes = []
                if v0 != v1:
                    causes.append("rules v4: the window is incomplete" if rules_old_snap["fields"][name]["value"]
                                  is None else "rules: unexplained")
                if v1 != v2:
                    causes.append("snapshot: " + (rules_new_snap["fields"][name]["reason"] or "its inputs differ"))
                structure_rows.append({"session_date": day, "kind": "structure", "target": name, "stored": v0,
                                       "replay": "", "old_rules_new_snapshot": v1, "new": v2,
                                       "cause": " | ".join(causes),
                                       "detail": rules_new_snap["fields"][name]["basis"] or ""})
        if unavailable(new["payload"]):
            snapshot_notes.append((day, unavailable(old["payload"]) or "-", unavailable(new["payload"])))
        if i % 25 == 0:
            print(f"{i} of {len(snaps)} sessions", file=sys.stderr)

    all_rows = rows + structure_rows
    unexplained = [r for r in all_rows if "unexplained" in r["cause"]]
    write_csv(OUT + ".csv", all_rows)
    write_markdown(OUT + ".md", rows, structure_rows, snapshot_notes, checks, len(snaps))
    print(f"{len(rows)} label and {len(structure_rows)} structure differences, {len(unexplained)} unexplained; "
          f"wrote {OUT}.md / .csv")
    return 1 if unexplained else 0


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    header = ["session_date", "kind", "target", "stored", "replay", "old_rules_new_snapshot", "new", "cause", "detail"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def _table(header, body):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c).replace("|", "/") for c in row) + " |" for row in body]
    return out


def _cause_key(cause: str) -> str:
    """The cause without session-specific numbers, for grouping."""
    parts = []
    for c in cause.split(" | "):
        if c.startswith("snapshot: "):
            names = sorted({re.split(r"[ (]", x.strip())[0] for x in c[len("snapshot: "):].split(";")})
            c = "snapshot: " + ", ".join(names) + " changed"
        elif c.startswith("FL candidates: ") and "reached first" in c:
            c = "FL candidates: a new candidate reached first"
        elif c.startswith("FL candidates: ") and "ambiguous" in c:
            c = "FL candidates: a new candidate on the other side in the same bar - ambiguous"
        elif c.startswith("FL candidates: a new candidate is unavailable"):
            c = "FL candidates: a new candidate is unavailable"
        parts.append(c)
    return " + ".join(parts)


def _section(lines, title, rows, kinds):
    lines += ["", f"## {title}", ""]
    if not rows:
        lines.append("None.")
    for target in kinds:
        diffs = [r for r in rows if r["target"] == target]
        if not diffs:
            continue
        lines += [f"### `{target}`" if title.startswith("Label") else f"### {target}", ""]
        moves = Counter((r["stored"], r["new"]) for r in diffs)
        lines += _table(["stored", "new", "sessions"], [(a, b, n) for (a, b), n in moves.most_common()])
        lines += ["", "Why:", ""]
        by_cause = defaultdict(list)
        for r in diffs:
            by_cause[_cause_key(r["cause"])].append(r)
        body = []
        for cause, rs in sorted(by_cause.items(), key=lambda kv: -len(kv[1])):
            ex = "<br>".join(f"{r['session_date']}: {r['stored']} -> {r['new']} - {r['cause']}"
                             + (f" ({r['detail'][:140]})" if r["detail"] else "") for r in rs[:2])
            body.append((cause, len(rs), ex))
        lines += _table(["cause", "sessions", "examples"], body) + [""]


def write_markdown(path, rows, structure_rows, snapshot_notes, checks, n_sessions):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    compared = checks["session-targets"] - checks["targets new in impl5"]
    new_snapshot = defs.PROFILES[PROFILE].snapshot_version
    lines = [
        f"# Label disagreement: {OLD_LABELS.rsplit('_', 1)[-1]} against {defs.LABEL_VERSION.rsplit('_', 1)[-1]}",
        "",
        f"Generated {now} by `scripts/label_revision_report.py`, reading only; nothing was recorded. Every row is in "
        f"the CSV beside this file.",
        "",
        f"- **Stored:** `{OLD_LABELS}` outcomes on `{OLD_SNAPSHOTS}` snapshots ({n_sessions} sessions); structure "
        f"annotations `{OLD_RULES}` (v3 gives the same labels).",
        f"- **New:** `{defs.LABEL_VERSION}` ({defs.FIRST_LEVEL_CONVENTION}, {defs.OPENING_SWEEP_CONVENTION}, "
        f"{defs.LEVEL_OUTCOME_CONVENTION}) on `{new_snapshot}` snapshots ({defs.CONVENTION_VERSION}, strict "
        f"completeness) built in memory; structure `{pre.RULES_PROTOCOL_VERSION}`.",
        f"- **Steps:** stored -> impl3 replayed from git ({LEGACY_COMMIT}) on today's bars -> impl3 on the new "
        f"snapshot -> impl5 on the new snapshot. The step where a label moves names the cause.",
        f"- **Checks:** the replay reproduces {checks['replay reproduces']} of {compared} stored session-targets"
        + (f"; {checks['no stored outcome']} session(s) without a stored outcome" if checks["no stored outcome"] else "")
        + (f"; {checks['no new snapshot']} without a new snapshot" if checks["no new snapshot"] else "") + ".",
        "",
        "## Summary",
        "",
    ]
    body = []
    for target in defs.TARGETS:
        diffs = [r for r in rows if r["target"] == target]
        n = sum(1 for _ in diffs)
        both = sum(1 for r in diffs if not r["stored"].startswith("(") and not r["new"].startswith("("))
        lost = sum(1 for r in diffs if not r["stored"].startswith("(") and r["new"].startswith("("))
        gained = sum(1 for r in diffs if r["stored"].startswith("(") and not r["new"].startswith("("))
        reason = sum(1 for r in diffs if r["stored"].startswith("(") and r["new"].startswith("("))
        unexpl = sum(1 for r in diffs if "unexplained" in r["cause"])
        body.append((f"`{target}`", n_sessions - n, n, both, lost, gained, reason, unexpl))
    lines += _table(["target", "same", "changed", "label -> other label", "label -> unavailable",
                     "unavailable -> label", "unavailable, other reason", "unexplained"], body)
    lines += ["", "Unavailable values show as `(reason)`. A session counts as \"same\" when the label or the reason "
                  "is unchanged."]
    causes = Counter()
    for r in rows:
        for c in _cause_key(r["cause"]).split(" + "):
            causes[c] += 1
    lines += ["", "### Causes, all targets", ""]
    lines += _table(["cause", "session-targets"], causes.most_common())
    _section(lines, "Label changes by target", rows, list(defs.TARGETS))

    lines += ["", f"## Structure annotation: {OLD_RULES} against {pre.RULES_PROTOCOL_VERSION}", ""]
    lines += [f"Values compared ({checks['session-fields']} session-fields); a basis-only change is not a "
              f"difference. The rule step is {pre.RULES_PROTOCOL_VERSION} on the stored snapshot, the snapshot step "
              f"the same rules on the new one.", ""]
    fcount = Counter(r["target"] for r in structure_rows)
    lines += _table(["field", "changed"], [(f, fcount[f]) for f in pre.FIELDS if fcount[f]] or [("(none)", 0)])
    _section(lines, "Structure changes by field", structure_rows, list(pre.FIELDS))

    lines += ["", "## New snapshots with something unavailable", ""]
    if snapshot_notes:
        lines += _table(["session", f"stored ({OLD_SNAPSHOTS})", f"new ({defs.PROFILES[PROFILE].snapshot_version})"],
                        snapshot_notes)
    else:
        lines += ["None."]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
