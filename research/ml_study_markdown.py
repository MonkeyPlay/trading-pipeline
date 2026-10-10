# research/ml_study_markdown.py
"""docs/reports/ml_study.md from ml_study_v1's results (research/ml_study_report.score)."""

from __future__ import annotations

from typing import Any, Dict, List

from research import ml_study as st
from research import ml_study_run as run
from research.ml_study_report import ARM_NAMES, LEVEL


def _f(x, nd=4, sign=False):
    if x is None:
        return "-"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def _ci(iv):
    return "-" if not iv else f"[{iv[0]:+.4f}, {iv[1]:+.4f}]"


def _pct(x, nd=1):
    return "-" if x is None else f"{100 * x:.{nd}f} %"


def _verdict(d) -> str:
    if not d or d.get("interval") is None:
        return "no interval"
    lo, hi = d["interval"]
    if hi < 0:
        return "better (interval below zero)"
    if lo > 0:
        return "worse (interval above zero)"
    return "no difference established"


def _name(arm: str, rth: bool = False) -> str:
    return "CLOCK same-clock frequencies" if rth and arm == "PRIOR" else ARM_NAMES.get(arm, arm)


def _decomposition(summary: Dict[str, Any], ref: str, rth: bool = False) -> List[str]:
    """Direction or size: each arm against ``ref`` on the full three-class score, on the symmetrised forecasts
    (bullish and bearish set to their mean: what is left is the chance of a move past the band - size), and on the
    direction alone (p(bullish) / (p(bullish) + p(bearish)) on the sessions' rows that moved past the band)."""
    lines = [f"| arm | three classes vs {_name(ref, rth)} | symmetrised: size only | direction only (moved rows) | "
             "its own tilt: original - symmetrised |", "|---|---|---|---|---|"]
    for arm, a in summary["arms"].items():
        if arm == ref or ref not in a["paired"]:
            continue
        p = a["paired"][ref]
        lines.append(f"| {_name(arm, rth)} | {_f(p['brier'], 4, True)} {_ci(p['interval'])} | "
                     f"{_f(p['sym']['mean'], 4, True)} {_ci(p['sym']['interval'])} | "
                     f"{_f(p['dir']['mean'], 4, True)} {_ci(p['dir']['interval'])} | "
                     f"{_f(a['tilt_effect']['mean'], 4, True)} {_ci(a['tilt_effect']['interval'])} |")
    return lines


def _arm_table(summary: Dict[str, Any], refs: List[str], names: Dict[str, str], rth: bool = False) -> List[str]:
    head = "| arm | Brier | log loss | ECE | " + " | ".join(f"vs {names.get(r, r)} (95 % interval)" for r in refs) + \
           " | mean TV from prior | p(bullish) sd |"
    lines = [head, "|---|---:|---:|---:|" + "---|" * len(refs) + "---:|---:|"]
    for arm, a in summary["arms"].items():
        cells = []
        for r in refs:
            p = a["paired"].get(r)
            cells.append("-" if p is None else f"{_f(p['brier'], 4, True)} {_ci(p['interval'])}")
        lines.append(f"| {_name(arm, rth)} | {_f(a['brier'])} | {_f(a['logloss'])} | {_f(a['ece'], 3)} | "
                     + " | ".join(cells) + f" | {_f(a.get('tv_prior'), 3)} | {_f(a['sd']['bullish'], 3)} |")
    return lines


def _and(items: List[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _below(d) -> bool:
    return bool(d and d.get("interval") and d["interval"][1] < 0)


def _above(d) -> bool:
    return bool(d and d.get("interval") and d["interval"][0] > 0)


def _cmp(d) -> str:
    return "-" if not d else f"{_f(d.get('mean', d.get('brier')), 4, True)} {_ci(d.get('interval'))}"


DEVIATIONS = [
    "TabPFN refuses more than 1000 training rows on a CPU by default (a speed guard); the RTH folds train on 1400 "
    "to 3000 rows, so the guard was lifted (TABPFN_ALLOW_CPU_LARGE_DATASET=1). The model and its defaults are "
    "unchanged.",
    "The direction-or-size decomposition (symmetrised forecasts, direction alone on the rows that moved) and the "
    "symmetric prior (PRIOR_SYM: the prior's bullish and bearish shares replaced by their mean) were added after the "
    "first scoring pass, when the RTH arms' gains over the same-clock prior needed explaining. They are analysis "
    "views computed from the stored predictions; no prediction, configuration or fold changed.",
    "The planted-signal control first used fixed betas whose oracle gains (0.065 and 0.145 pre-open, 0.022 and "
    "0.072 RTH) missed the protocol's targets. The final run solves beta for the targets (ml_study.beta_for: 0.04 "
    "and 0.015). The shuffled-label controls are also scored against the symmetric prior: with shuffled labels, "
    "the conditional prior beat CLOCK itself in 3 of 5 RTH runs, so beating CLOCK needs no signal.",
    "An RTH label first required only its window's two end bars; the protocol (and rth_eval.window_move) requires "
    "every bar of the window. Fixed before the final run; no window in the data lacks an inner bar (0 of 29,160), so "
    "no label changed.",
]


def _fold_lines(rows: List[Dict[str, Any]], arms: List[str], rth: bool = False) -> List[str]:
    arms = [a for a in arms if rows and a in rows[0]]
    lines = ["| outer fold (test sessions) | n | " + " | ".join(_name(a, rth) for a in arms) + " |",
             "|---|---:|" + "---:|" * len(arms)]
    for r in rows:
        lines.append(f"| {r['test'][0]} to {r['test'][1]} | {r['sessions']} | "
                     + " | ".join(_f(r[a]) for a in arms) + " |")
    return lines


def _fold_reading(rows: List[Dict[str, Any]]) -> str:
    """Which arms beat A in how many folds, and any arm ahead of A in every one of the latest folds (a run of at
    least three)."""
    arms = [a for a in ("N", "M", "P", "LR", "GB", "TPF", "PRIOR_SYM") if rows and a in rows[0]]
    wins = {a: sum(r[a] < r["A"] for r in rows) for a in arms}
    out = f"Folds in which an arm beats A, of {len(rows)}: " + ", ".join(f"{_name(a)} {w}" for a, w in wins.items())
    out += ". No arm beats A in every fold." if max(wins.values()) < len(rows) else "."
    for a in arms:
        k = 0
        for r in reversed(rows):
            if r[a] < r["A"]:
                k += 1
            else:
                break
        if 3 <= k < len(rows):
            d = [r[a] - r["A"] for r in rows[-k:]]
            out += (f" {_name(a)} beats A in each of the last {k} folds (from {rows[-k]['test'][0]}; differences "
                    f"{', '.join(f'{x:+.3f}' for x in d)}) after trailing earlier. With 20 sessions a fold "
                    "(a standard error of about 0.03 each), and noticed after the fact, that is within noise. It is "
                    "what p1_ml_forward_v2's prospective sessions test.")
    return out + " No regime split was declared before the study, so none is reported."


def _choices(fc: Dict[str, Dict[str, int]]) -> str:
    return "; ".join(f"{fam} {', '.join(f'{k} {v}' for k, v in sorted(c.items()))}" for fam, c in sorted(fc.items()))


def render(res: Dict[str, Any]) -> str:
    pre, rth, ctl, pw = res["preopen"], res["rth"], res["controls"], res["power"]
    s = pre["summary"]
    A = s["arms"]
    P = rth["problems"]
    L: List[str] = []
    add = L.extend
    ladder = [a for a in A if a not in ("A", "B", "PRIOR_SYM")]

    # ------------------------------------------------------------------ the findings the answer rests on
    pre_beats_a = [a for a in ladder if _below(A[a]["paired"].get("A"))]
    pre_worse_a = [a for a in ladder if _above(A[a]["paired"].get("A"))]
    pre_dir = [a for a in A if a != "PRIOR_SYM" and _below((A[a]["paired"].get("PRIOR_SYM") or {}).get("dir"))]
    rth_arms = [a for a in P["rth/h15/cutoff"]["matched"]["arms"] if a not in ("PRIOR", "PRIOR_SYM")]
    rth_dir, rth_clock, rth_size, n_cmp = [], [], [], 0
    for name, p in P.items():
        for a, v in p["matched"]["arms"].items():
            if a in ("PRIOR", "PRIOR_SYM"):
                continue
            n_cmp += 1
            if _below((v["paired"].get("PRIOR_SYM") or {}).get("dir")):
                rth_dir.append((name, a))
            if _below(v["paired"].get("PRIOR")):
                rth_clock.append((name, a))
            if _below((v["paired"].get("PRIOR_SYM") or {}).get("sym")):
                rth_size.append((name, a))
    best_pre = min(ladder, key=lambda a: A[a]["brier"])
    p15 = P["rth/h15/cutoff"]["matched"]["arms"]
    sym_vs_clock = {n: P[n]["matched"]["arms"]["PRIOR_SYM"]["paired"]["PRIOR"] for n in P}
    rth_vs_sym = [(n, a) for n, p in P.items() for a, v in p["matched"]["arms"].items()
                  if a not in ("PRIOR", "PRIOR_SYM") and _below(v["paired"].get("PRIOR_SYM"))]

    add([f"# NQ direction: ML audit and bounded improvement study ({st.VERSION})", "",
         "**Development research on inspected sessions, not a test.** Every session here was looked at before "
         "(hist_dev_v1, p1_pool_tuning_v1, the fan experiments, the ML development comparison). Anything this study "
         "finds is at most a candidate for a prospective study. Nothing here is registered, promoted or delivered; A "
         "and B stay as they are.", "",
         f"The protocol `{st.VERSION}` (hash `{res['protocol_hash']}`, [research/ml_study.py](../../research/ml_study.py)) "
         "was fixed before any outer prediction. Every outer prediction was written with its sha256 before the "
         f"scoring stage read an outcome ({len(res['verified_files'])} files checked at scoring). The trial manifest, "
         "predictions and per-session scores are in [ml_study_v1/](ml_study_v1/).", "",
         "## Answer", ""])
    chance = 0.025 * n_cmp
    if not pre_beats_a and not pre_dir and len(rth_dir) <= max(1, round(chance)):
        add(["**No edge established.** Nothing predicts NQ's direction better than having no view, before the open "
             "or at any RTH horizon. No challenger is recommended."
             + (f" One direction-only interval of {n_cmp} dips below no view ({_name(rth_dir[0][1], True)} at "
                f"{rth_dir[0][0].split('/')[1][1:]} min, {rth_dir[0][0].split('/')[2]} origin): about what chance "
                f"alone gives ({chance:.1f})." if len(rth_dir) == 1 else ""), ""])
    else:
        add(["**No reliable directional edge.** The intervals that fall below zero are listed below. They are "
             f"development data with {n_cmp} RTH comparisons per score, so about {chance:.1f} such intervals are "
             "expected by chance alone.", ""])
    va = A[best_pre]["paired"].get("A")
    add([f"- **Pre-open direction_15m** ({s['sessions']} test sessions): nothing beats A. The best candidate is "
         f"{_name(best_pre)}, at {_f(A[best_pre]['brier'])} against A's {_f(A['A']['brier'])} "
         f"({_cmp(va)}). "
         + (f"{_and([_name(a) for a in pre_worse_a])} are worse than A, with intervals above zero. "
            if pre_worse_a else "")
         + f"B scores {_f(A['B']['brier'])}, N {_f(A['N']['brier'])}, M {_f(A['M']['brier'])} and P "
         f"{_f(A['P']['brier'])}. The frequencies with no view on direction (bullish = bearish) score "
         f"{_f(A['PRIOR_SYM']['brier'])}, slightly better than A ({_cmp(A['PRIOR_SYM']['paired'].get('A'))}). "
         "So even A's own tilt carries no information; this is far below a material edge, and a post-hoc view. "
         "With one row per session this sample misses most real signals smaller than about 0.04 (sections 1 and "
         "5), so the pre-open \"no edge\" rules out only a large one.",
         f"- **RTH, the next 15 minutes from the cutoff** ({P['rth/h15/cutoff']['matched']['sessions']} test "
         f"sessions): the same-clock prior (CLOCK) scores {_f(p15['PRIOR']['brier'])}. Across the 8 horizons and "
         f"origins, {len(rth_clock)} of {n_cmp} arm and problem pairs beat CLOCK with an interval below zero. "
         "But CLOCK's own per-cutoff up-shares are noise: the symmetric prior (the same frequencies with no view on "
         f"direction) beats CLOCK in {sum(_below(v) for v in sym_vs_clock.values())} of 8 problems. Against the "
         f"symmetric prior, {len(rth_vs_sym)} pair(s) keep an interval below zero on the three classes"
         + (" (" + "; ".join(f"{_name(a, True)} at {n.split('/')[1][1:]} min, {n.split('/')[2]} origin: "
                             f"{_cmp(P[n]['matched']['arms'][a]['paired']['PRIOR_SYM'])}, of which size "
                             f"{_cmp(P[n]['matched']['arms'][a]['paired']['PRIOR_SYM']['sym'])} and direction "
                             f"{_cmp(P[n]['matched']['arms'][a]['paired']['PRIOR_SYM']['dir'])}"
                             for n, a in rth_vs_sym) + ")" if rth_vs_sym else "")
         + f". On direction alone, {len(rth_dir)} of {n_cmp} fall below no view: "
         + (", ".join(f"{_name(a, True)} at {n.split('/')[1][1:]} min ({n.split('/')[2]})" for n, a in rth_dir)
            or "none") + f". About {0.025 * n_cmp:.1f} would by chance alone.",
         f"- **RTH analogues:** RTH-20's member frequencies score worse than the same-clock history at every "
         f"horizon (15 minutes: {_cmp(p15['B_rth']['paired']['PRIOR'])}). Adding the recent path to the "
         f"similarity does not help ({_cmp(p15['B_rth_recent']['paired'].get('B_rth'))} against RTH-20).",
         "- **Size, not direction:** a scale model forecasts the size of the next 15 minutes better than the "
         "same-clock history (section 4.6). The location added to it does not help.",
         "- **Why N, M and P look like A:** it is shrinkage on a weak signal, not a coding bug (section 2).",
         "- **What it would take:** detecting a true 0.01 improvement needs hundreds of sessions, and the "
         "registered rule cannot reach 80 % power for an effect of exactly 0.01 at any sample size. Sixty sessions "
         "detect only effects several times larger (section 5).", ""])

    # ------------------------------------------------------------------ what ran
    m = run._manifest()
    stages = sorted((m.get("stages") or {}).items(), key=lambda kv: kv[1].get("at", ""))
    add(["## What ran", "", "| stage | when (UTC) | code | what |", "|---|---|---|---|"])
    for name, e in stages:
        what = {"predict_preopen": f"{e.get('sessions')} pool sessions, {e.get('folds')} outer folds, test "
                                   f"{' to '.join(e.get('test', []))}",
                "predict_rth": f"{e.get('sessions')} sessions {' to '.join(e.get('span', []))}, "
                               f"{e.get('feature_rows')} cutoff rows, {e.get('label_rows')} windows",
                "analogues": f"{e.get('sessions')} test sessions, {e.get('rows')} forecasts",
                "tabpfn": "TabPFN v2 on the pre-open and the two 15-minute RTH problems (.venv-research)",
                "controls": f"{e.get('preopen_runs')} pre-open and {e.get('rth_runs')} RTH control runs, the "
                            "future-bar checks",
                "score": f"{len(e.get('verified', []))} prediction files verified, then the outcomes read"}.get(name, "")
        add([f"| {name} | {e.get('at')} | `{str(e.get('code_revision', ''))[:12]}"
             f"{'+dirty' if str(e.get('code_revision', '')).endswith('+dirty') else ''}` | {what} |"])
    rerun = [(name, e) for name, e in stages if e.get("earlier_runs")]
    if rerun:
        add(["", "Re-run from the final code, after TabPFN and the controls had used the first run's outputs: "
             + "; ".join(f"{name} (first {e['earlier_runs'][0]['at']}) - "
                         + ("every prediction file byte-identical" if e.get("reproduced") else "FILES DIFFER")
                         for name, e in rerun) + "."])
    add(["", "**The trial budget.** Every configuration of every family was tried in every outer fold, each scored "
             "on three inner folds. Nothing else was tried.", "",
         "| family | configurations | grid |", "|---|---:|---|",
         f"| CP conditional prior | {len(st.CP_GRID_PREOPEN)} pre-open, {len(st.CP_GRID_RTH)} RTH | context x "
         "shrinkage kappa. Pre-open: none / overnight volatility / gap / VIX level x 5, 20, 80. RTH: clock or phase "
         "x none / 30-minute / session volatility x 10, 50, 200 |",
         f"| LR logistic | {len(st.LR_GRID)} | features NQ only / NQ plus others x C 0.001 to 1 |",
         f"| GB boosted trees | {len(st.GB_GRID)} | features x depth 1, 2 x (rate, iterations) (0.03, 50), (0.03, "
         "150), (0.1, 50) |",
         f"| RES prior + residual | {len(st.RES_GRID)} | features x C 0.003 to 1 |",
         "| TPF TabPFN v2 | 1 | defaults, NQ features |",
         "| BL blend | 1 | w fitted on the inner predictions |",
         "| frozen N, M, P | 4 each | their registered grids and 75/25 tuning (contracts/nq_ml) |", "",
         "The outer folds train on every earlier session less one embargoed session, test the next 20, and start "
         "after 120 sessions. For the pre-open task they are exactly the development comparison's eight folds. "
         "Each tuned family is tried on two feature sets (NQ only, NQ plus other instruments), an ablation counted "
         "in its grid.", "",
         "## Deviations from the protocol", ""] + [f"- {d}" for d in DEVIATIONS] + [""])

    # ------------------------------------------------------------------ 1. correctness
    dev, wk = pre["dev_reproduction"], pre["week"]
    inv = (ctl or {}).get("invariance", {})
    add(["## 1. Implementation checks: correctness, not skill", "",
         "| check | result | evidence |", "|---|---|---|",
         f"| The development comparison reproduced | {'identical' if dev['identical'] else 'DIFFERS'} | "
         f"{dev['sessions']} common sessions, eight arms. The largest Brier difference from "
         f"docs/reports/ml_development.json is {max(v['abs_diff'] for v in dev['arms'].values()):.0e}, which is "
         "floating-point summation order |",
         f"| The five-session check reproduced, full vectors | {'identical' if wk['identical'] else 'DIFFERS'} | "
         f"Every vector equals the earlier check's saved output (ml_study_v1/week_check_2026-10-09.json; largest "
         f"difference {max(wk['max_vector_diff'].values()):.0e}). The mean Brier scores are "
         + ", ".join(f"{a} {wk['mean_brier'][a]:.4f}" for a in ("A", "B", "N", "M", "P"))
         + ". The brief's N 0.662 was a mean of rounded session scores |",
         "| Class order and full-vector Brier | pass | tests/test_ml_study.py: probabilities are mapped by class "
         "name, and the Brier score uses every class, never the realised class's probability alone |",
         "| Folds, embargo and label windows | pass | Every outer fold is checked (ml_study.check_fold), and the inner "
         "folds stay inside their outer training window. Each label ends inside its own session |",
         f"| Pooled split by session date | pass | In the production fold code (forecaster/ml_eval._fold), the "
         f"8 folds have {sum(f['rows_on_test_or_embargo_dates'] for f in pre['pooled']['folds'])} training rows "
         f"on test or embargoed dates and {sum(f['dates_outside_training'] for f in pre['pooled']['folds'])} "
         "outside the training dates. Scoring uses NQ's rows only; the test is "
         "tests/test_ml_study.py::test_the_pooled_fit_trains_on_training_dates_only |",
         "| Outer test labels cannot move a prediction | pass | tests/test_ml_study.py: every outer test label was "
         "flipped and the embargoed session's labels removed; the predictions, choices and blend did not change |"])
    if inv:
        pt = inv.get("preopen_truncation", [])
        add([f"| Future bars: pre-open features | {'pass' if all(c['identical'] for c in pt) else 'FAIL'} | "
             "Rebuilt from bars truncated at " + ", ".join(
                 f"{c['boundary']} ({c['sessions']} sessions {'identical' if c['identical'] else 'DIFFER'})"
                 for c in pt) + " |",
             f"| Future bars: RTH features | {'pass' if inv['rth_features']['changed'] == 0 else 'FAIL'} | "
             f"{inv['rth_features']['rows']} cutoff rows had every later bar of their session altered: "
             f"{inv['rth_features']['changed']} changed |",
             f"| Future bars: analogue ranks | {'pass' if inv['rth_ranks']['changed'] == 0 else 'FAIL'} | "
             f"{inv['rth_ranks']['rankings']} rankings had the target's later bars altered: "
             f"{inv['rth_ranks']['changed']} changed |"])
    if ctl:
        for task, label in (("preopen_shuffled", "pre-open"), ("rth_shuffled", "RTH, 15 minutes")):
            sh = ctl[task]
            runs = next(iter(sh.values()))["runs"]
            fp = sum(v["below_zero_interval"] for v in sh.values())
            have_sym = all(v.get("below_zero_vs_sym") is not None for v in sh.values())
            fs = sum(v["below_zero_vs_sym"] for v in sh.values()) if have_sym else None
            ok = fs <= max(1, round(0.05 * runs * len(sh))) if have_sym else fp <= max(1, round(0.05 * runs * len(sh)))
            add([f"| Shuffled labels, {label} | {'pass' if ok else 'CHECK'} | {runs} permutations x {len(sh)} arms: "
                 + (f"against the symmetric prior, {fs} interval(s) below zero" if have_sym else
                    "the symmetric comparison was not computed in this run")
                 + f" (about {0.025 * runs * len(sh):.1f} expected by chance); against the prior itself, {fp}. Mean "
                   "differences from the prior: "
                 + ", ".join(f"{a} {v['mean']:+.4f}" for a, v in sh.items()) + " |"])
        for task, label in (("preopen_synthetic", "pre-open"), ("rth_synthetic", "RTH, 15 minutes")):
            for beta, v in sorted(ctl[task].items(), key=lambda kv: kv[1]["oracle_gain"]):
                det = {a: x["detected"] for a, x in v["arms"].items()}
                best = max(n for a, n in det.items() if a != "CP")
                verdict = (f"found ({best}/{v['runs']})" if best >= v["runs"] / 2 else
                           f"rarely found ({best}/{v['runs']})" if best else "not found")
                add([f"| Planted signal, {label}, oracle gain {v['oracle_gain']:.3f} (beta {float(beta):.3f}) | "
                     f"{verdict} | The oracle's gain is "
                     f"{v['oracle_gain']:.4f}. Detected (interval below zero) in "
                     + ", ".join(f"{a} {n}/{v['runs']}" for a, n in det.items()) + " runs. Mean gains: "
                     + ", ".join(f"{a} {-x['mean']:.4f}" for a, x in v["arms"].items()) + " |"])
    det_lines = []
    if ctl:
        for task, label in (("rth_synthetic", "RTH (15 minutes)"), ("preopen_synthetic", "pre-open")):
            parts = []
            for beta, v in sorted(ctl[task].items(), key=lambda kv: kv[1]["oracle_gain"]):
                best = max(x["detected"] for a, x in v["arms"].items() if a != "CP")
                parts.append(f"a planted gain of {v['oracle_gain']:.3f} in at most {best} of {v['runs']} runs")
            det_lines.append(f"{label}: " + "; ".join(parts))
    add(["", "The code does what it claims:", "",
         "- the reproductions are identical;",
         "- nothing leaks across the date split or from later bars;",
         "- shuffled labels give no skill against the symmetric prior;",
         "- a planted signal is found where the sample allows: " + ("; ".join(det_lines) if det_lines else "-")
         + ".", "",
         "So the two tasks' silences mean different things:", "",
         "- The RTH task, with twelve cutoffs per session, finds a signal of the size that would matter. Its \"no "
         "edge\" is informative.",
         "- The pre-open task has one row per session. It misses most planted signals of 0.04, so its \"no edge\" "
         "says only that no large signal exists (section 5).", "",
         "None of this says whether NQ is predictable. The next sections do.", ""])

    # ------------------------------------------------------------------ 2. why the ML looks like the prior
    folds = pre["folds"]
    add(["## 2. Why N, M and P look like the prior", "",
         f"**The sample.** Each development fold trains on {folds[0]['train'][2]} to {folds[-1]['train'][2]} "
         "sessions, one row per session. The classes in the last training window are "
         + ", ".join(f"{c} {n}" for c, n in folds[-1]["class_counts"].items())
         + f". The pooled model's rows grow to {folds[-1]['pooled_rows']}. But ES's label agrees with NQ's on "
         f"{_pct(pre['pooled']['label_agreement_with_nq'].get('ES'), 0)} of dates and RTY's on "
         f"{_pct(pre['pooled']['label_agreement_with_nq'].get('RTY'), 0)}: they share the day's news, so the "
         "pooled rows carry far less than three times the information.", ""])
    feats = pre["features"]
    const = sorted((k for k, v in feats.items() if v["modal_share"] and v["modal_share"] >= 0.9),
                   key=lambda k: -feats[k]["modal_share"])
    miss = sorted((k for k, v in feats.items() if v["missing"] > 0), key=lambda k: -feats[k]["missing"])
    sds = {k: v["sd"] for k, v in feats.items() if v["sd"] and v["modal_share"] < 0.9}
    lo_k, hi_k = min(sds, key=sds.get), max(sds, key=sds.get)
    add(["**The inputs.**", "",
         "- Missing features are imputed with the training median, with a missing indicator: "
         + (", ".join(f"{k} {_pct(feats[k]['missing'])}" for k in miss) or "none") + ".",
         "- Nearly constant features (one value on at least 90 % of sessions): "
         + (", ".join(f"{k} {_pct(feats[k]['modal_share'], 0)}" for k in const) or "none") + ".",
         f"- The continuous features' standard deviations run from {sds[lo_k]:.2f} ({lo_k}) to "
         f"{sds[hi_k]:.2f} ({hi_k}). The logistic models standardise each training window anyway.", ""])
    av = pre["availability"]
    add([f"**Were the inputs there at the time?** In the development data, "
         f"{av['development']['NQ'].get('reconstructed', 0)} of {sum(av['development']['NQ'].values())} sessions' "
         "bars are backfilled history: their status is 'reconstructed', meaning they were stored more than two hours "
         "after the bar ended. So the development rows show what the history says, not what Auto would have had. "
         f"Rebuilt as of each session's replay deadline (10:04 ET, receipts respected), only "
         f"{av['rows_equal_at_deadline']} rows equal the development rows: {', '.join(av['live_nq_days'])}, the "
         "sessions Auto collected live. No other session's inputs had been received by then.", "",
         "Two more points on availability:", "",
         f"- VIX is closed at the 09:29 cutoff, so by design it uses its 09:14 bar "
         f"({av['age_minutes'].get('VIX', {}).get('median', 0):.0f} minutes old).",
         "- VXN is not used. No released economic value is an input, only the scheduled times from the snapshot's "
         "frozen calendar.", "",
         "The development comparison therefore says nothing about live availability. Only the forward sessions can.",
         ""])
    from collections import Counter
    cn = Counter(f["frozen_chosen"]["nq_only/logit"]["C"] for f in folds)
    cm = Counter(f["frozen_chosen"]["multi/logit"]["C"] for f in folds)
    lr_c = Counter(f["study_chosen"]["LR"]["C"] for f in folds)
    lines = ["**Shrinkage.** N's frozen grid is C 0.01 to 0.3.", "",
             "- N's tuning chose " + ", ".join(f"C {c} in {n} fold(s)" for c, n in sorted(cn.items())) + ".",
             "- M's tuning chose " + ", ".join(f"C {c} in {n} fold(s)" for c, n in sorted(cm.items())) + ".",
             "- With a grid down to 0.001, the study's nested logistic regression chose "
             + ", ".join(f"C {c} in {n}" for c, n in sorted(lr_c.items())) + ".", "",
             "The table refits along a wider path. It is post hoc: scored here, never used to choose.", "",
             "| C | N: test Brier | N: mean TV from the frequencies | N: p(bullish) sd | M: test Brier | M: mean TV "
             "| M: p(bullish) sd |", "|---:|---:|---:|---:|---:|---:|---:|"]
    for a, b in zip(pre["path"]["nq_only"], pre["path"]["multi"]):
        lines.append(f"| {a['C']:g} | {_f(a['test_brier'])} | {_f(a['tv_from_frequencies'], 3)} | "
                     f"{_f(a['sd_bullish'], 3)} | {_f(b['test_brier'])} | {_f(b['tv_from_frequencies'], 3)} | "
                     f"{_f(b['sd_bullish'], 3)} |")
    path_n = pre["path"]["nq_only"]
    best_c = min(path_n, key=lambda r: r["test_brier"])
    add(lines + ["", "A looser penalty moves the forecasts further from the frequencies and makes them vary more "
                     "from day to day. The Brier score gets steadily worse as it does: N's best point is "
                     f"C {best_c['C']:g} ({_f(best_c['test_brier'])}), which is essentially the frequencies, against "
                     f"A's {_f(A['A']['brier'])}. The frozen grid's strongest penalty, C 0.01, is still looser than "
                     "that.", ""])
    lines = ["**Dispersion and divergence** on the test sessions:", "",
             "| arm | p(bearish) sd | p(bullish) sd | p(neutral) sd | mean TV from A_s | argmax differs from A_s | "
             "mean KL from A_s |", "|---|---:|---:|---:|---:|---:|---:|"]
    for arm, a in A.items():
        if arm == "PRIOR_SYM":
            continue
        lines.append(f"| {_name(arm)} | {_f(a['sd']['bearish'], 3)} | {_f(a['sd']['bullish'], 3)} | "
                     f"{_f(a['sd']['neutral_band'], 3)} | {_f(a.get('tv_prior'), 3)} | "
                     f"{_pct(a.get('argmax_differs_prior'), 0)} | {_f(a.get('kl_prior'), 4)} |")
    tvs = [A[a]["tv_prior"] for a in ("N", "M", "P")]
    amx = [A[a]["argmax_differs_prior"] for a in ("N", "M", "P")]
    add(lines + ["", f"N, M and P do condition on their inputs. On an average day they move {100 * min(tvs):.0f} to "
                     f"{100 * max(tvs):.0f} points of probability away from the frequencies, and their most likely "
                     f"class differs from A_s's on {_pct(min(amx), 0)} to {_pct(max(amx), 0)} of days. Those moves "
                     "do not improve the score. The frozen grids stop at C 0.01, looser than what the data support "
                     "(the path above), so the models carry a little more noise than the best shrinkage would.", ""])
    prod = pre["production"]
    add(["**Fallbacks.** N, M and P never return a fallback prior. Each issues its model's probabilities or is "
         "unavailable, with the reason: a session in the training window, no frozen T, NQ's own features missing, "
         f"or, for M only, a required instrument stale. M abstained on {len(pre['abstain_in_test'])} development "
         "test sessions.", "",
         "In production so far:", ""]
        + [f"- {r['algorithm']} ({r['mode']}): {r['n']} {r['status']}" for r in prod["runs"]]
        + ["- deliveries: " + ", ".join(f"{k} {v}" for k, v in prod["deliveries"].items())
           + ". The one delivery is 2026-10-09's, recorded after the fact by 363c818 and shown as a "
             "reconstruction.", "",
           "**Verdict.** The probabilities track A because the data leave little better to do:", "",
           "- the tuning prefers the strongest penalty on offer;",
           "- a looser penalty scores worse;",
           "- the same code learns a planted signal (section 1);",
           "- the features come from history only.", "",
           "This is shrinkage on a weak signal, not a bug.", ""])

    # ------------------------------------------------------------------ 3. pre-open ladder
    add(["## 3. Pre-open direction_15m: the candidate ladder", "",
         f"There are {pre['test_sessions']} scheduled test sessions, of which {pre['labelled_test']} have a recorded "
         f"label, and {s['sessions']} are scored. 2026-10-09 has no recorded label yet. The score is the unhalved "
         "Brier score (0 to 2, lower is better). Differences are paired per session, with 95 % moving-block bootstrap "
         "intervals. They are descriptive: development data, many comparisons.", ""])
    add(_arm_table(s, ["A", "B", "PRIOR"], {"PRIOR": "A_s"}))
    add(["", "**Direction or size**, against the symmetric prior (A_s with no view on direction). The direction-only "
             "score is 2 x (q - [bullish])^2, with q = p(bullish) / (p(bullish) + p(bearish)), on the sessions "
             "that moved past the band; having no view scores exactly 0.5:", ""])
    add(_decomposition(s, "PRIOR_SYM"))
    pre_dir_best = min((a for a in A if a != "PRIOR_SYM"), key=lambda a: A[a]["dir_brier"])
    add(["", f"No arm is better than no view on direction. The best direction-only score is "
             f"{_name(pre_dir_best)}'s {_f(A[pre_dir_best]['dir_brier'])}, against no view's 0.5000 "
             f"({_cmp(A[pre_dir_best]['paired']['PRIOR_SYM']['dir'])}). Even A's own small tilt between bullish and "
             f"bearish costs {_cmp(A['A']['paired']['PRIOR_SYM'])} against the symmetric prior."])
    bl = [f["study_chosen"].get("BL") for f in folds]
    add(["", "**Blend weights.** w goes on the best inner family and 1 - w on A_s, fitted on earlier inner "
         "predictions: " + ", ".join(f"{b['family']} {b['w']:.2f}" for b in bl if b)
         + ". The weight jumps between 0 and 1 across folds. That is what noise looks like to the inner folds, not "
           "a stable signal.", "",
         "**Chosen configurations per fold:**", ""]
        + [f"- {f['test'][0]}: LR C {f['study_chosen']['LR']['C']} ({f['study_chosen']['LR']['features']}); GB "
           f"{f['study_chosen']['GB']['features']}, depth {f['study_chosen']['GB']['max_depth']}, "
           f"{f['study_chosen']['GB']['max_iter']} iterations; RES C {f['study_chosen']['RES']['C']}; CP "
           f"{f['study_chosen']['CP']['context'] or 'no context'} with kappa {f['study_chosen']['CP']['kappa']}"
           for f in folds] + [""])
    add(["**Each outer fold** (mean per-session Brier score; the symmetric prior is the frequencies with no view on "
         "direction):", ""] + _fold_lines(pre["by_fold"], ["A", "B", "N", "M", "P", "PRIOR_SYM", "LR", "GB", "TPF"])
        + ["", _fold_reading(pre["by_fold"]), "",
           "**NQ only, or NQ plus other instruments?** The inner folds chose: " + _choices(pre["feature_choices"])
           + ". No arm found an edge that the other instruments would be needed for.", ""])
    wk_rows = ["| session | realised | " + " | ".join(f"{a}: bear/bull/neutral %, Brier" for a in "ABNMP") + " |",
               "|---|---|" + "---|" * 5]
    for r in wk["rows"]:
        cells = []
        for a in "ABNMP":
            v = r["arms"].get(a)
            cells.append("-" if v is None else f"{'/'.join(f'{100 * x:.1f}' for x in v['p'])}, {v['brier']:.3f}")
        wk_rows.append(f"| {r['session']} | {r['label']}{' *' if 'computed' in r['source'] else ''} | "
                       + " | ".join(cells) + " |")
    add(["**The five supplied sessions.** These are walk-forward refits on every earlier labelled session, the "
         "earlier check's method, shown as full vectors:", ""] + wk_rows +
        ["", "Means: " + ", ".join(f"{a} {wk['mean_brier'][a]:.3f}" for a in "ABNMP") + ".", "",
         "\\* 2026-10-09's label is computed from the stored bars; no outcome is recorded yet.", "",
         "Five sessions establish nothing: not equality, not non-inferiority, not a lasting disadvantage.", ""])

    # ------------------------------------------------------------------ 4. RTH
    add(["## 4. RTH rolling targets", "",
         "**The target.** At every 30 minutes from 10:00 to 15:30 ET, the direction of the next h minutes (15 "
         "primary; 5, 30 and 60 secondary). The band is 0.5 x the two-minute ATR frozen at the cutoff x "
         "sqrt(h / 15). At 15 minutes this is direction_15m's rule, and it was fixed ex ante.", "",
         "**Two origins.**", "",
         "- *Cutoff origin:* the window starts at the cutoff.",
         f"- *Delayed origin:* the window starts {st.RTH_ORIGINS['delayed']} minutes later. That allows for the "
         "feed's ~10-minute delay, the confirming bar, and the start at the second full minute after the build. The "
         "features still end at the cutoff.", "",
         "Sessions weigh equally: each session's cutoffs are averaged first. Every row has a label (label reasons: "
         f"{rth['label_reasons']}).", "",
         "### 4.1 The primary horizon: 15 minutes", ""])
    for name, title in (("rth/h15/cutoff", "Cutoff origin"), ("rth/h15/delayed", "Delayed origin")):
        p = P[name]
        mt = p["matched"]
        fb = ", ".join(f"{_name(a, True)} {n}" for a, n in p["fallbacks"].items() if n) or "none"
        add([f"**{title}.** {mt['sessions']} sessions and {mt['rows']} matched rows (every arm present). Rows where "
             f"an arm had no forecast and fell back to the prior: {fb}.", ""])
        add(_arm_table(mt, ["PRIOR", "PRIOR_SYM", "B_rth"], {"PRIOR": "CLOCK", "PRIOR_SYM": "symmetric prior",
                                                             "B_rth": "B_rth"}, rth=True) + [""])
    add(["**Each outer fold**, 15 minutes from the cutoff:", ""]
        + _fold_lines(P["rth/h15/cutoff"]["by_fold"], ["PRIOR", "PRIOR_SYM", "LR", "GB", "TPF", "B_rth"], rth=True)
        + [""])
    from collections import Counter
    fc_all: Dict[str, Counter] = {}
    for p in P.values():
        for fam, c in p["feature_choices"].items():
            fc_all.setdefault(fam, Counter()).update(c)
    add(["**NQ only, or NQ plus other instruments?** Over all eight problems and their folds, the inner folds chose: "
         + _choices({k: dict(v) for k, v in fc_all.items()}) + ". NQ's own features win most choices. ES, RTY and "
         "VXN win about a third, and none of it adds up to a directional edge (section 4.3).", ""])
    add(["### 4.2 Direction or size, 15 minutes", "",
         "Each arm is compared with the symmetric prior: the same-clock frequencies with no view on direction. "
         "Having no view scores exactly 0.5 on direction alone.", ""])
    for name, title in (("rth/h15/cutoff", "Cutoff origin"), ("rth/h15/delayed", "Delayed origin")):
        add([f"**{title}**", ""] + _decomposition(P[name]["matched"], "PRIOR_SYM", rth=True) + [""])
    res_worse = [n for n, p in P.items() if _above((p["matched"]["arms"].get("RES", {}).get("paired", {})
                                                    .get("PRIOR_SYM") or {}).get("dir"))]
    add(["How to read the two tables:", "",
         "- The symmetrised column is the size part: the chance of a move past the band.",
         "- The direction-only column is measured against no view, which scores exactly 0.5. A gain over CLOCK "
         "that disappears here was CLOCK's noisy tilt, not the arm's skill.",
         f"- RES, the prior plus residual, carries CLOCK's tilt in its offset (log CLOCK). Its direction-only score "
         f"is worse than no view in {len(res_worse)} of 8 problems.", ""])
    add(["### 4.3 Every horizon and origin", "",
         "| horizon | origin | sessions | CLOCK | symmetric prior - CLOCK | best arm | its Brier | best - CLOCK | "
         "best - symmetric prior | best direction-only - no view | RTH-20 - CLOCK |",
         "|---:|---|---:|---:|---|---|---:|---|---|---|---|"])
    for name, p in P.items():
        mt = p["matched"]["arms"]
        best = min((a for a in mt if a not in ("PRIOR", "PRIOR_SYM")), key=lambda a: mt[a]["brier"])
        bdir = min((a for a in mt if a not in ("PRIOR", "PRIOR_SYM")), key=lambda a: mt[a]["dir_brier"])
        _, h, origin = name.split("/")
        add([f"| {h[1:]} | {origin} | {p['matched']['sessions']} | {_f(mt['PRIOR']['brier'])} | "
             f"{_cmp(mt['PRIOR_SYM']['paired']['PRIOR'])} | {_name(best, True)} | {_f(mt[best]['brier'])} | "
             f"{_cmp(mt[best]['paired']['PRIOR'])} | {_cmp(mt[best]['paired']['PRIOR_SYM'])} | "
             f"{_name(bdir, True)} {_cmp(mt[bdir]['paired']['PRIOR_SYM']['dir'])} | "
             + (_cmp(mt['B_rth']['paired']['PRIOR']) if "B_rth" in mt else "-") + " |"])
    add(["", f"Across the 8 problems and {len(rth_arms)} arms ({n_cmp} pairs), the direction-only score has "
             f"{len(rth_dir)} interval(s) below zero against no view: "
         + (", ".join(f"{_name(a, True)} at {n.split('/')[1][1:]} min ({n.split('/')[2]})" for n, a in rth_dir)
            or "none")
         + f". At 95 %, about {0.025 * n_cmp:.1f} would fall below zero by chance alone. The symmetrised (size) "
           f"score has {len(rth_size)} interval(s) below zero against the symmetric prior.", ""])
    ph = P["rth/h15/cutoff"]["phases"]
    cols = [a for a in ("LR", "GB", "BL", "B_rth", "B_rth_recent") if a in next(iter(ph.values()))]
    add(["### 4.4 By session phase: 15 minutes, cutoff origin (exploratory)", "",
         "| phase | " + " | ".join(f"{_name(a, True)} - symmetric prior" for a in cols) + " |",
         "|---|" + "---|" * len(cols)])
    for phase, v in ph.items():
        add([f"| {phase} | " + " | ".join(_cmp(v[a]["vs_sym"]) for a in cols) + " |"])
    b15 = p15["B_rth_recent"]["paired"].get("B_rth")
    add(["", "### 4.5 The analogues: an expanding prefix against expanding plus recent", "",
         "B_rth_recent ranks the same pool by the mean of two similarities: v3's, and a recent-path similarity "
         "(the last 30 minutes, re-anchored, judged at the path tolerance for 30 minutes). Against RTH-20 at 15 "
         f"minutes from the cutoff it scores {_cmp(b15)}. The recent path does not rescue the analogues: both "
         "variants lose to the same-clock history at every horizon (section 4.3). A later reversal is not what "
         "they are missing.", "",
         "These are development estimates from reconstructed rankings. rth_session_v1 is the prospective test, and "
         "it scores direction as up or not up, with size separately.", ""])
    d1, d2 = P["rth/h15/cutoff"].get("distribution"), P["rth/h15/delayed"].get("distribution")
    if d1:
        add(["### 4.6 Size and direction as distributions: 15 minutes", "",
             "CRPS is in units of sigma_1m x sqrt(15).", "",
             "| origin | CLOCK empirical | LS0: scale model, zero drift | LSmu: scale + ridge location | "
             "LS0 - CLOCK | LSmu - LS0 | 90 % coverage, empirical / LS | 90 % width, empirical / LS |",
             "|---|---:|---:|---:|---|---|---|---|"])
        for origin, d in (("cutoff", d1), ("delayed", d2)):
            if d:
                add([f"| {origin} | {_f(d['crps']['CLOCK_emp'])} | {_f(d['crps']['LS0'])} | {_f(d['crps']['LSmu'])} | "
                     f"{_cmp(d['paired']['LS0 - CLOCK_emp'])} | {_cmp(d['paired']['LSmu - LS0'])} | "
                     f"{_pct(d['coverage']['cover90_emp'])} / {_pct(d['coverage']['cover90_ls'])} | "
                     f"{_f(d['width90']['emp'], 2)} / {_f(d['width90']['ls'], 2)} |"])
        size_better = _below(d1["paired"]["LS0 - CLOCK_emp"]) and (not d2 or _below(d2["paired"]["LS0 - CLOCK_emp"]))
        alphas = d1["direction"]["alphas"]
        maxed = len(alphas) == 1 and float(next(iter(alphas))) == max(float(a) for a in (1e4, *map(float, alphas)))
        add(["", ("**Size is forecastable.** " if size_better else "**Size:** ")
             + "The scale is a regression of the log squared move on the volatility so far, relative volume, the "
               "range, the time of day and scheduled events. "
             + ("It beats the same-clock history with intervals below zero at both origins. " if size_better
                else "It does not clearly beat the same-clock history. ")
             + f"Its log correlates with the log size of the move at {_f(d1['scale']['corr_log_sigma_abs_z'], 2)}.", "",
             "**Direction is not.** The ridge location's penalty "
             + ("went to its maximum in every fold" if maxed else f"was chosen as {alphas}")
             + f". Its out-of-sample correlation with the move is {_f(d1['direction']['corr_mu_z'], 3, True)}, "
               f"and it gets the sign right {_pct(d1['direction']['sign_hit'])} of the time.", "",
             "This repeats 2026-09's finding. A narrower calibrated range is not a directional edge. The project's "
             "random-walk fan (fan_rw_v1) is the deployed size benchmark. Its own reports "
             "(docs/reports/fan_rw_v1_NQ_*.md) cover it, and it was not re-run here.", ""])

    # ------------------------------------------------------------------ 5. power
    add(["## 5. Power", "",
         "These figures come from the development per-session differences: their standard deviation, and the "
         "moving-block bootstrap's design effect (the variance of the mean against independent sessions). The table "
         "gives the sessions needed for 80 % power at a true improvement delta. It uses the normal approximation and "
         f"two-sided {100 * LEVEL:.2f} % intervals (Bonferroni over three candidates). Two rules:", "",
         "- *registered* (p1_ml_forward_v2): the point estimate at most -0.01 and the upper bound below 0;",
         "- *material* (recommended for any new study): the upper bound below -0.01.", "",
         "| comparison | n | sd | design effect | registered: delta 0.01 / 0.02 / 0.03 | material: delta 0.02 / "
         "0.03 / 0.05 | material at 95 % (fixed sequence): 0.02 / 0.03 / 0.05 | power at 60 sessions, registered, "
         "delta 0.02 / 0.03 | detectable at 60 (registered) |",
         "|---|---:|---:|---:|---|---|---|---|---:|"])
    for name, v in pw.items():
        rr, mr, fx = v["registered_rule"], v["material_rule"], v["material_fixed_sequence"]
        add([f"| {name} | {v['n']} | {v['sd']:.3f} | {v['design_effect']:.2f} | "
             f"{rr['0.01'] or 'never'} / {rr['0.02'] or 'never'} / {rr['0.03'] or 'never'} | "
             f"{mr['0.02'] or 'never'} / {mr['0.03'] or 'never'} / {mr['0.05'] or 'never'} | "
             f"{fx['0.02'] or 'never'} / {fx['0.03'] or 'never'} / {fx['0.05'] or 'never'} | "
             f"{_pct(v['power_at_60']['0.02'], 0)} / {_pct(v['power_at_60']['0.03'], 0)} | "
             f"{v['detectable_at_60']['registered']:.3f} |"])
    pre_rows = [v for k, v in pw.items() if "pre-open" in k]
    rth_rows = [v for k, v in pw.items() if "RTH" in k]
    add(["", "A difference without significance does not show that two forecasts are equally good. A claim of "
             "non-inferiority would need a margin registered in advance and an adjusted upper bound below it. Even "
             "then, it would not meet the goal of a positive edge.", "",
         "**What the table shows:**", "",
         "- *An effect of exactly 0.01 never reaches 80 % power under the registered rule.* Its point threshold is "
         "0.01 itself, so power stays at most 50 % however many sessions accrue."]
        + ([f"- *Pre-open differences are noisy.* Their standard deviation is {min(v['sd'] for v in pre_rows):.2f} "
            f"to {max(v['sd'] for v in pre_rows):.2f} per session, so 60 sessions detect only improvements of "
            f"{min(v['detectable_at_60']['registered'] for v in pre_rows):.3f} to "
            f"{max(v['detectable_at_60']['registered'] for v in pre_rows):.3f} - several times what any development "
            "estimate suggests."] if pre_rows else [])
        + ([f"- *RTH differences are much tighter*, at {min(v['sd'] for v in rth_rows):.3f} to "
            f"{max(v['sd'] for v in rth_rows):.3f} per session, because each session averages twelve cutoffs. An "
            "RTH study is where 60 sessions could settle something - provided the comparator is the symmetric "
            "prior, not CLOCK."] if rth_rows else []) + [""])

    # ------------------------------------------------------------------ 6. forecast vs trading edge
    add(["## 6. Forecast edge is not trading edge", "",
         "Nothing here measures money. A lower Brier score is a forecast edge. A trading claim would need:", "",
         "- a simple execution and risk policy, fixed before the evaluation;",
         "- net expectancy after fees, spread, slippage and the measured delivery delay;",
         "- a comparison against the same policy run on A and B, and against no trade;",
         "- conservative counting of ambiguous fills, because minute bars cannot settle the fill order inside a "
         "bar.", "",
         "No trading claim is made, and this study authorises no trade.", ""])

    # ------------------------------------------------------------------ 7. recommendation
    add(["## 7. Recommendation", "",
         "**No edge established. No challenger is recommended.**", "",
         "- A and B stay in force as they are: B as the existing baseline, A as the benchmark.",
         "- N, M and P stay experimental.",
         "- p1_ml_forward_v2 runs as registered. Its prospective sessions are the only clean test of N, M and P.",
         "- Registering another direction model now would spend the forward sample on a candidate the development "
         "data do not support.", "",
         "**Worth keeping in view, as size forecasts rather than direction challengers:**", "",
         "- the scale model (section 4.6);",
         "- the models' small gain on the neutral-or-not part at 15 minutes (section 4.2).", "",
         "**Any later RTH comparison** should be made against the symmetric prior, not CLOCK. CLOCK's noisy "
         "per-cutoff tilt is beaten even with shuffled labels (section 1).", "",
         "Both belong to the fan's territory. There they would compete with fan_rw_v1, not with A.", "",
         "**The analogues.** rth_session_v1 (registered by its first issue) tests the full-session analogues "
         "prospectively. The development estimate above gives little reason to expect a directional result.", ""])
    add(["## Reproduce", "", "```",
         "python scripts/ml_study.py all    # or the stages one by one: predict-preopen, predict-rth, analogues,",
         "                                  # tabpfn, controls, score", "```", "",
         "- TabPFN runs in .venv-research (research/requirements-tabpfn.txt), never in the production .venv. Its "
         "weights are pinned by sha256 in research/tabpfn_arm.py.",
         "- The database is read with default_transaction_read_only; nothing is written to it.", ""])
    return "\n".join(L) + "\n"
