#!/usr/bin/env python3
"""
Disagreement report (guideline stage 1E): the old v5 labels
(``nq_labels_v5_candidate``) against the NQ-v2 labels, session by session, with
the definition that explains every difference.

    python scripts/label_disagreement_report.py \\
        --old-db postgresql://trading:trading@localhost:5432/tp_scratch_v5_labels

The v5 records were dropped by migration 0008; restore the ``forecast`` schema
from the pre-0008 backup into a scratch database first (docs/reports/README.md).
Reads only - the scratch database for v5, ``--db`` for the journal and the bars -
and writes docs/reports/label_disagreement_v5_vs_nq_v2.{md,csv}.

How a difference is explained:
  1. v5 is replayed on today's bars with its own code (git commit LEGACY_COMMIT)
     and its own frozen A / ON levels. A replay that differs from the stored v5
     label means the bars were revised since; the definitions are then compared
     on the replay.
  2. First move and the direction targets: the NQ-v2 rule is rerun with v5's
     threshold (0.10 A / 0.20 A instead of T / B). If that reproduces v5, the
     threshold explains the difference; for first move, the remaining difference
     is v5 deciding a same-minute double touch by the minute's open.
  3. Opening type and session type, whose rule lists differ wholesale: v5's
     winning class is looked up under NQ-v2 - it holds but NQ-v2 checks the new
     class first (rule order), or it fails, and the failing condition is given;
     v5's "mixed" has no NQ-v2 class, NQ-v2's "uncovered" no v5 class.

The NQ-v2 labels are the recorded ``RECORDED`` outcomes; NQ-v2 is also recomputed
in memory (the current label version) for the rule details and the IB direction,
and the report says whether the recomputation reproduces every recorded label.
"""

import argparse
import csv
import importlib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pandas as pd

from config import Config
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from database.connection import get_db_connection
from features import calendar as cal
from forecaster import labels_prompt_v2 as labels

LEGACY_COMMIT = "f00a0c1"
OLD_LABELS, OLD_FEATURES = "nq_labels_v5_candidate", "nq_features_v3"
RECORDED = "nq_prompt_v2_1_impl2"
SNAPSHOT_VERSION = defs.PROFILES["research_0929"].snapshot_version
OUT = os.path.join(_PROJECT_ROOT, "docs", "reports", "label_disagreement_v5_vs_nq_v2")

DIRECTION = {"up": "bullish", "down": "bearish", "flat": "neutral_band"}
# v5 target -> (NQ-v2 target, v5 label -> NQ-v2 label; None: no NQ-v2 class)
PAIRS = {
    "first_move_5m": ("first_move_5m", {"up_first": "up_first", "down_first": "down_first", "neither": "neither"}),
    "direction_15m": ("direction_15m", DIRECTION),
    "opening_type_15m": ("opening_type_15m", {
        "drive_up": "opening_drive_up", "drive_down": "opening_drive_down", "sweep_low_rebound": "sweep_low_rebound",
        "sweep_high_reverse": "sweep_high_reverse", "two_sided": "two_sided_whipsaw", "range": "range",
        "mixed": None}),
    "direction_1h": ("ib_direction", DIRECTION),
    "direction_rth": ("close_direction_rth", DIRECTION),
    "session_type_rth": ("session_type_rth", {
        "bull_trend": "bull_trend_day", "bear_trend": "bear_trend_day", "reversal": "reversal_day",
        "two_sided_volatile": "two_sided_volatile_day", "range": "range_day", "mixed": None}),
}
NOT_COMPARED = {"v5 (no NQ-v2 counterpart)": ["first_break_1h", "range_15m_regime", "range_1h_regime",
                                               "range_rth_regime"]}
# v5 bands, in units of its frozen A, of the direction targets
V5_BAND = {"direction_15m": Fraction(1, 10), "direction_1h": Fraction(1, 10), "direction_rth": Fraction(1, 5)}
CLOSE_MINUTE = {"direction_15m": 14, "direction_1h": 59}


# --------------------------------------------------------------------------
# v5, from git history
# --------------------------------------------------------------------------

def load_v5():
    """forecaster/labels_v2.py (and the indicators it needs) as they were at LEGACY_COMMIT."""
    def show(path):
        return subprocess.check_output(["git", "show", f"{LEGACY_COMMIT}:{path}"], cwd=_PROJECT_ROOT, text=True)
    root = tempfile.mkdtemp(prefix="legacy_v5_")
    pkg = os.path.join(root, "legacy_v5")
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    with open(os.path.join(pkg, "indicators.py"), "w") as f:
        f.write(show("features/indicators.py"))
    with open(os.path.join(pkg, "labels_v2.py"), "w") as f:
        f.write(show("forecaster/labels_v2.py").replace("from features.indicators import",
                                                        "from legacy_v5.indicators import"))
    sys.path.insert(0, root)
    module = importlib.import_module("legacy_v5.labels_v2")
    assert module.LABEL_VERSION == OLD_LABELS
    return module


def replay_v5(v5, bars, session, ref):
    """v5's labels for one session from today's bars, with the old snapshot's frozen A / ON levels."""
    rows = [{"bar_start_at": pd.Timestamp(b.start), "open": float(b.open), "high": float(b.high),
             "low": float(b.low), "close": float(b.close)} for b in bars if b.start >= session.rth_open_at]
    df = pd.DataFrame(rows, columns=["bar_start_at", "open", "high", "low", "close"])
    out = v5.compute_metrics(df, session, ref.get("A"), ref.get("ONH"), ref.get("ONL"))
    return {t: v["label"] for t, v in v5.compute_labels(out["metrics"], out["status"], session).items()}


# --------------------------------------------------------------------------
# Explanations
# --------------------------------------------------------------------------

def _pts(v) -> str:
    v = Decimal(v.numerator) / Decimal(v.denominator) if isinstance(v, Fraction) else Decimal(str(v))
    return f"{v.quantize(Decimal('0.01'))}"


def _direction(diff, band) -> str:
    return "bullish" if diff > band else "bearish" if diff < -band else "neutral_band"


def explain_first_move(x, A_old, replay_new, new):
    """Barrier (T vs 0.10 A), v5's same-minute tie rule, or v5's whole-window coverage."""
    t = x.T
    b_old = Decimal(repr(max(1.0, 0.10 * A_old)))
    x.T = b_old
    hybrid = labels._first_move(x, {})                     # NQ-v2 rule, v5 barrier
    x.T = t
    numbers = f"T = {t} pts, v5 barrier 0.10A = {_pts(b_old)} pts"
    if hybrid["label"] == replay_new:
        return "threshold: T vs 0.10 A", f"{numbers}; with v5's barrier NQ-v2 gives v5's label"
    if hybrid["reason"] == "ambiguous_intrabar" and replay_new is not None:
        both = "tie rule" if new == hybrid["label"] or new is None else "tie rule + threshold"
        return (f"{both}: v5 decides a same-minute double touch by the minute's open",
                f"{numbers}; both barriers in one bar, NQ-v2 leaves it unavailable")
    if replay_new is None and hybrid["label"] is not None:
        return "coverage: v5 needs the whole 5-minute window", numbers
    return "unexplained", f"{numbers}; NQ-v2 with v5 barrier gives {hybrid['label'] or hybrid['reason']}"


def explain_direction(target, x, meas, A_old, replay_new, new):
    """The band: NQ-v2's T or B against v5's 0.10 A / 0.20 A."""
    if target == "direction_rth":
        close, band, name = meas.get("RTH_close"), x.B, "B"
    else:
        close, band, name = meas.get("C15" if target == "direction_15m" else "IB_close"), x.T, "T"
    if close is None or x.O is None:
        return "unexplained", "no close or open measured"
    diff = Decimal(close) - x.O
    v5_band = V5_BAND[target] * Fraction(Decimal(repr(A_old)))
    hybrid = _direction(Fraction(diff), v5_band)
    numbers = f"close - O = {diff} pts; NQ-v2 band {name} = {band} pts, v5 band {V5_BAND[target]}A = {_pts(v5_band)} pts"
    if hybrid == replay_new:
        return f"threshold: {name} vs {V5_BAND[target]} A", numbers
    return "unexplained", numbers


def _drive_fail(meas, O, T, up):
    H, L, C = Decimal(meas["H15"]), Decimal(meas["L15"]), Decimal(meas["C15"])
    conds = ([(f"C15 - O = {C - O} <= T", C > O + T)] if up else [(f"O - C15 = {O - C} <= T", C < O - T)])
    conds += [(f"efficiency {meas.get('efficiency_15m')} < 0.60", H > L and abs(C - O) >= Decimal("0.60") * (H - L))]
    conds += ([(f"counter-excursion O - L15 = {O - L} > T", O - L <= T)] if up else
              [(f"counter-excursion H15 - O = {H - O} > T", H - O <= T)])
    return ", ".join(text for text, ok in conds if not ok)


def explain_opening_type(x, meas, replay_old, new, new_reason):
    unavailable, rules = labels.opening_type_rules(x)
    if unavailable is not None:
        return "NQ-v2 unavailable", unavailable["reason"]
    result = {label: r for label, r, _ in rules}
    counterpart = PAIRS["opening_type_15m"][1][replay_old]
    O, T = x.O, x.T
    if counterpart is None:
        return "v5 'mixed' has no NQ-v2 class", f"NQ-v2: {new or new_reason}"
    if new is None:
        return "NQ-v2 unavailable", new_reason
    if counterpart == "range":                 # NQ-v2's range is what no other rule takes
        H, L = Decimal(meas["H15"]), Decimal(meas["L15"])
        if new == "two_sided_whipsaw":
            return ("threshold: both O + T and O - T reached (v5 two-sided needs 0.12 A each way)",
                    f"H15 - O = {H - O}, O - L15 = {O - L}, T = {T}")
        if new in ("sweep_low_rebound", "sweep_high_reverse"):
            return ("sweep definition: NQ-v2 adds the previous-RTH levels, breach >= T",
                    f"v5 range; T = {T}; v5 sweeps used the ON level only, breach 0.02 A")
        return f"{new} definition: efficiency and counter-excursion against T", f"v5 range; T = {T}"
    if result[counterpart] is True:
        return "NQ-v2 rule order", f"{counterpart} also holds under NQ-v2, which checks {new} first"
    if counterpart in ("opening_drive_up", "opening_drive_down"):
        return (f"{counterpart} fails under NQ-v2",
                _drive_fail(meas, O, T, counterpart == "opening_drive_up") + f"; T = {T}")
    if counterpart == "two_sided_whipsaw":
        H, L = Decimal(meas["H15"]), Decimal(meas["L15"])
        return ("two_sided_whipsaw fails under NQ-v2",
                f"H15 - O = {H - O}, O - L15 = {O - L} against T = {T} (v5 needs both >= 0.12 A)")
    C = Decimal(meas["C15"])
    side = "C15 - O" if counterpart == "sweep_low_rebound" else "O - C15"
    move = C - O if counterpart == "sweep_low_rebound" else O - C
    if move <= T:
        return f"{counterpart} fails under NQ-v2", f"{side} = {move} <= T = {T}"
    return (f"{counterpart} fails under NQ-v2",
            "no frozen reference (ON and previous-RTH high / low) breached by >= T, reclaimed and not gap-crossed; "
            "v5 used the ON level only, with a 0.02 A breach")


def explain_session_type(x, meas, close_dir, replay_old, new, new_reason):
    unavailable, rules = labels.session_type_rules(x, close_dir)
    if unavailable is not None:
        return "NQ-v2 unavailable", unavailable["reason"]
    result = dict(rules)
    counterpart = PAIRS["session_type_rth"][1][replay_old]
    A = x.A
    E, CL, R = meas.get("E"), meas.get("CL"), meas.get("R")
    figures = f"close {close_dir}, E = {E}, CL = {CL}, R/A = {_pts(Fraction(Decimal(R)) / A)}" if R else "zero range"
    if counterpart is None:
        return "v5 'mixed' has no NQ-v2 class", f"NQ-v2: {new or new_reason}; {figures}"
    if new is None and new_reason == "uncovered":
        return f"no P2 rule fits (NQ-v2 uncovered; v5 {replay_old})", figures
    if new is None:
        return "NQ-v2 unavailable", new_reason
    if result[counterpart]:
        return "NQ-v2 rule order", f"{counterpart} also holds under P2, which checks {new} first; {figures}"
    O = Fraction(x.O)
    high, low, close = (Fraction(Decimal(meas[k])) for k in ("RTH_high", "RTH_low", "RTH_close"))
    r = high - low
    e, cl = abs(close - O) / r, (close - low) / r
    failing = {
        "bull_trend_day": [(close_dir != "bullish", f"close {close_dir}"), (e < Fraction(3, 5), f"E = {_pts(e)} < 0.60"),
                           (cl < Fraction(4, 5), f"CL = {_pts(cl)} < 0.80")],
        "bear_trend_day": [(close_dir != "bearish", f"close {close_dir}"), (e < Fraction(3, 5), f"E = {_pts(e)} < 0.60"),
                           (cl > Fraction(1, 5), f"CL = {_pts(cl)} > 0.20")],
        "two_sided_volatile_day": [(r < A, f"R/A = {_pts(r / A)} < 1"), (e >= Fraction(7, 20), f"E = {_pts(e)} >= 0.35")],
        "range_day": [(r >= A, f"R/A = {_pts(r / A)} >= 1"), (e >= Fraction(7, 20), f"E = {_pts(e)} >= 0.35")],
    }
    if counterpart in failing:
        return (f"{counterpart} fails under P2",
                ", ".join(text for bad, text in failing[counterpart] if bad) + f"; NQ-v2 gives {new}")
    if counterpart == "reversal_day":
        ib_h, ib_l, O = Fraction(Decimal(meas["IB_high"])), Fraction(Decimal(meas["IB_low"])), Fraction(x.O)
        return ("reversal_day fails under P2",
                f"IB high - O = {_pts(ib_h - O)}, O - IB low = {_pts(O - ib_l)} against A/4 = {_pts(A / 4)} with the "
                f"close {close_dir} (v5: first-hour return vs RTH return, 0.20 A each)")
    return f"{counterpart} fails under P2", figures


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def load_old(conn_old):
    snaps = {}
    for r in conn_old.execute(
            "SELECT snapshot_id, session_date, instrument_id, reference_values FROM forecast.feature_snapshots "
            "WHERE feature_version = %s ORDER BY session_date, created_at;", (OLD_FEATURES,)).fetchall():
        snaps[str(r["session_date"])] = {"snapshot_id": str(r["snapshot_id"]), "contract_id": int(r["instrument_id"]),
                                         "ref": json.loads(r["reference_values"]), "labels": {}}
    by_id = {s["snapshot_id"]: s for s in snaps.values()}
    for r in conn_old.execute(
            "SELECT DISTINCT ON (snapshot_id, target_id) snapshot_id, target_id, actual_label, ineligibility_reason "
            "FROM forecast.realised_outcomes WHERE label_version = %s "
            "ORDER BY snapshot_id, target_id, outcome_revision DESC;", (OLD_LABELS,)).fetchall():
        s = by_id.get(str(r["snapshot_id"]))
        if s is not None:
            s["labels"][r["target_id"]] = (r["actual_label"], r["ineligibility_reason"])
    return snaps


def main(argv=None):
    parser = argparse.ArgumentParser(description="v5 vs NQ-v2 label disagreement report")
    parser.add_argument("--old-db", required=True, help="scratch database holding the restored forecast schema")
    parser.add_argument("--db", default=Config.DATABASE_URL, help="database with the journal and the bars")
    parser.add_argument("--out", default=OUT, help="output path without extension")
    args = parser.parse_args(argv)

    v5 = load_v5()
    conn_old, conn = get_db_connection(args.old_db), get_db_connection(args.db)
    old = load_old(conn_old)
    rows, data_revised, recompute_mismatch, a_diffs, contract_mismatch = [], [], [], [], []
    snaps = store.list_snapshots(conn, "2000-01-01", "2100-01-01", SNAPSHOT_VERSION)
    sessions = [s for s in snaps if s["session_date"] in old]
    for snap in sessions:
        day = snap["session_date"]
        o = old[day]
        session = cal.session(day)
        bars = labels.load_realised_bars(conn, snap)
        recorded = store.latest_outcome(conn, snap["snapshot_id"], RECORDED)
        out = labels.compute_outcome(snap, bars)
        recompute_mismatch += [(day, t) for t, v in recorded["labels"].items() if out["labels"][t] != v]
        x, meas = labels._Session(snap, bars), out["measurements"]
        if o["contract_id"] != snap["contract_id"]:
            contract_mismatch.append(day)
            old_bars = labels.load_realised_bars(conn, {"session_date": day, "contract_id": o["contract_id"]})
        else:
            old_bars = bars
        replay = replay_v5(v5, old_bars, session, o["ref"])
        A_old = o["ref"].get("A")
        if A_old is not None and x.A is not None:
            a_diffs.append(abs(Fraction(Decimal(repr(A_old))) - x.A))

        for v5_target, (new_target, mapping) in PAIRS.items():
            stored, stored_reason = o["labels"].get(v5_target, (None, "missing"))
            source = recorded if new_target in recorded["labels"] else out
            nv = source["labels"][new_target]
            new, new_reason = nv["label"], nv["reason"]
            rep = replay.get(v5_target)
            if rep != stored:
                data_revised.append((day, v5_target, stored, rep))
            mapped_stored = mapping.get(stored) if stored else None
            if stored is not None and new is not None and mapped_stored == new:
                agreement, category, detail = "agree", "", ""
            elif stored is None and new is None:
                agreement, category, detail = "both unavailable", "", f"v5: {stored_reason}; NQ-v2: {new_reason}"
            elif stored is None:
                agreement, category, detail = "v5 unavailable", f"v5 unavailable: {stored_reason}", ""
            elif new is None and new_reason not in ("uncovered", "ambiguous_intrabar"):
                agreement, category, detail = "NQ-v2 unavailable", f"NQ-v2 unavailable: {new_reason}", nv["detail"] or ""
            else:
                agreement = "disagree"
                if rep != stored and rep is not None and mapping.get(rep) == new:
                    category, detail = "bars revised since the v5 label", f"v5 on today's bars: {rep}"
                elif rep is None:
                    category, detail = "bars revised since the v5 label", "v5 on today's bars: unavailable"
                elif v5_target == "first_move_5m":
                    category, detail = explain_first_move(x, A_old, mapping.get(rep), new)
                elif v5_target in V5_BAND:
                    category, detail = explain_direction(v5_target, x, meas, A_old, mapping.get(rep), new)
                elif v5_target == "opening_type_15m":
                    category, detail = explain_opening_type(x, meas, rep, new, new_reason)
                else:
                    category, detail = explain_session_type(x, meas, out["labels"]["close_direction_rth"]["label"],
                                                            rep, new, new_reason)
                if rep != stored:
                    detail += f" (stored v5 {stored}, v5 on today's bars {rep})"
            rows.append({"session_date": day, "v5_target": v5_target, "nq_v2_target": new_target,
                         "v5_label": stored or f"({stored_reason})", "v5_replay": rep or "(unavailable)",
                         "nq_v2_label": new or f"({new_reason})", "agreement": agreement,
                         "cause": category, "detail": detail})

    write_csv(args.out + ".csv", rows)
    write_markdown(args.out + ".md", rows, sessions, old, data_revised, recompute_mismatch, a_diffs,
                   contract_mismatch)
    unexplained = sum(1 for r in rows if r["cause"] == "unexplained")
    print(f"{len(sessions)} sessions, {len(rows)} comparisons, "
          f"{sum(r['agreement'] == 'disagree' for r in rows)} disagreements, {unexplained} unexplained; "
          f"wrote {args.out}.md / .csv")
    conn.close()
    conn_old.close()
    return 1 if unexplained or recompute_mismatch else 0


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _table(header, body):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in body]
    return out


def write_markdown(path, rows, sessions, old, data_revised, recompute_mismatch, a_diffs, contract_mismatch):
    n = len(sessions)
    first, last = sessions[0]["session_date"], sessions[-1]["session_date"]
    exact = sum(1 for d in a_diffs if d < Fraction(1, 10 ** 6))
    lines = [
        "# Label disagreement: v5 against NQ-v2",
        "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `scripts/label_disagreement_report.py` "
        "(guideline stage 1E). Every row is in the CSV beside this file.",
        "",
        f"- **Old:** `{OLD_LABELS}` on `{OLD_FEATURES}` snapshots, restored from the pre-0008 backup "
        f"({len(old)} sessions).",
        f"- **New:** `{RECORDED}` outcomes recorded on `{SNAPSHOT_VERSION}` snapshots; the IB direction (no recorded "
        f"counterpart in {RECORDED}) from `{defs.LABEL_VERSION}`, recomputed.",
        f"- **Compared:** the {n} sessions both have, {first} to {last}.",
        f"- **Checks:** recomputing NQ-v2 reproduces every recorded label: "
        f"{'yes' if not recompute_mismatch else f'no - {len(recompute_mismatch)} differ'}. "
        f"v5 replayed on today's bars with its own code (commit {LEGACY_COMMIT}) reproduces the stored v5 label on "
        f"{n * len(PAIRS) - len(data_revised)} of {n * len(PAIRS)} session-targets"
        + (f"; the {len(data_revised)} others are bars revised since v5 was computed." if data_revised else ".")
        + f" The frozen daily ATR A agrees within 0.000001 on {exact} of {len(a_diffs)} sessions"
        + (f" (largest difference {_pts(max(a_diffs))} points)." if a_diffs else ".")
        + (f" On {len(contract_mismatch)} session(s) the two snapshots used different contracts: "
           f"{', '.join(contract_mismatch)}." if contract_mismatch else ""),
        "",
        "## Summary",
        "",
    ]
    by_target = defaultdict(list)
    for r in rows:
        by_target[r["v5_target"]].append(r)
    summary = []
    for t, rs in by_target.items():
        c = Counter(r["agreement"] for r in rs)
        unexplained = sum(1 for r in rs if r["cause"] == "unexplained")
        summary.append([f"`{t}` -> `{PAIRS[t][0]}`", c["agree"], c["disagree"], c["v5 unavailable"],
                        c["NQ-v2 unavailable"], c["both unavailable"], unexplained])
    lines += _table(["v5 -> NQ-v2", "agree", "disagree", "only NQ-v2 labelled", "only v5 labelled",
                     "both unavailable", "unexplained"], summary)
    lines += ["", "Labels compare through the canonical mapping (v5 up / down / flat = bullish / bearish / "
              "neutral_band; two_sided = two_sided_whipsaw; the session-type names). An NQ-v2 `ambiguous_intrabar` "
              "or `uncovered` is counted as a disagreement, since it is a definition outcome, not missing data.",
              "", "Not compared: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in NOT_COMPARED.items())
              + "; NQ-v2 opening bias, first level, LO-v1 and the descriptors have no v5 counterpart.", ""]

    for t, rs in by_target.items():
        new_t = PAIRS[t][0]
        lines += [f"## `{t}` -> `{new_t}`", ""]
        old_vals = sorted({r["v5_label"] for r in rs})
        new_vals = sorted({r["nq_v2_label"] for r in rs})
        matrix = Counter((r["v5_label"], r["nq_v2_label"]) for r in rs)
        lines += ["Rows v5, columns NQ-v2:", ""]
        lines += _table(["v5 \\ NQ-v2"] + new_vals, [[o] + [matrix.get((o, nv), "") for nv in new_vals]
                                                      for o in old_vals])
        causes = Counter(r["cause"] for r in rs if r["cause"])
        if causes:
            lines += ["", "Why they differ:", ""]
            body = []
            for cause, count in causes.most_common():
                examples = [r for r in rs if r["cause"] == cause][:2]
                body.append([cause, count, "<br>".join(f"{e['session_date']}: v5 {e['v5_label']}, NQ-v2 "
                                                       f"{e['nq_v2_label']} - {e['detail']}" for e in examples)])
            lines += _table(["cause", "sessions", "examples"], body)
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
