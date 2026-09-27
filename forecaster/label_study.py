# forecaster/label_study.py
"""
Label-threshold study: how the stored sessions would be labelled under
alternative ``labels_v2.PARAMETERS``, and where the underlying metrics lie.

Nothing is written. The section 11 thresholds are a starting configuration;
this report is what a new LABEL_VERSION's thresholds are chosen from (on
already-realised sessions, which only defines the vocabulary - the models are
still trained and scored walk-forward).

    variants      {name: overrides}; overrides are dotted paths into PARAMETERS,
                  e.g. {"opening_type_15m.drive.counter_excursion_max": 0.08}
"""

import copy
from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np

from features import calendar as cal
from features.indicators import finite
from forecaster import labels_v2

# Candidates worth seeing next to the current rules. They are starting points
# for the comparison, not a recommendation: the quantiles below decide.
PRESETS: Dict[str, Dict[str, Any]] = {
    "current": {},
    "first_move_B0.075": {"first_move_atr_fraction": 0.075},
    "first_move_B0.10": {"first_move_atr_fraction": 0.10},
    "looser_types": {
        "opening_type_15m.two_sided.u_min": 0.10, "opening_type_15m.two_sided.d_min": 0.10,
        "opening_type_15m.drive.r_abs_min": 0.15, "opening_type_15m.drive.counter_excursion_max": 0.08,
        "opening_type_15m.drive.e_min": 0.40,
        "opening_type_15m.range.w_max": 0.25, "opening_type_15m.range.r_abs_max": 0.08,
        "session_type_rth.trend.r_abs_min": 0.40, "session_type_rth.trend.q_bull_min": 0.75,
        "session_type_rth.trend.q_bear_max": 0.25, "session_type_rth.trend.e_min": 0.25,
        "session_type_rth.two_sided_volatile.w_min": 0.90,
        "session_type_rth.range.w_max": 0.80, "session_type_rth.range.r_abs_max": 0.25,
    },
}

# Metric -> how it is summarised (the label rules read these).
QUANTILE_METRICS: List[Tuple[str, Any]] = [
    ("return_15m_atr", None), ("|return_15m_atr|", lambda m: _abs(m, "return_15m_atr")),
    ("up_excursion_15m_atr", None), ("down_excursion_15m_atr", None),
    ("min(up,down)_15m_atr", lambda m: _min(m, "up_excursion_15m_atr", "down_excursion_15m_atr")),
    ("range_15m_atr", None), ("efficiency_15m", None),
    ("return_rth_atr", None), ("|return_rth_atr|", lambda m: _abs(m, "return_rth_atr")),
    ("up_excursion_rth_atr", None), ("down_excursion_rth_atr", None),
    ("min(up,down)_rth_atr", lambda m: _min(m, "up_excursion_rth_atr", "down_excursion_rth_atr")),
    ("range_rth_atr", None), ("rth_close_location", None), ("efficiency_rth_5m", None),
    ("first_hour_return_atr", None),
]
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)


def _abs(m, k):
    return None if m.get(k) is None else abs(m[k])


def _min(m, a, b):
    return None if m.get(a) is None or m.get(b) is None else min(m[a], m[b])


def with_overrides(overrides: Dict[str, Any]) -> Dict[str, Any]:
    params = copy.deepcopy(labels_v2.PARAMETERS)
    for path, value in overrides.items():
        node, keys = params, path.split(".")
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                raise KeyError(f"unknown label parameter {path!r}")
            node = node[k]
        if keys[-1] not in node:
            raise KeyError(f"unknown label parameter {path!r}")
        node[keys[-1]] = value
    return params


def one_per_session(snapshots: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The live capture of each session if there is one, else its newest snapshot."""
    best: Dict[str, Dict[str, Any]] = {}
    for s in snapshots:
        day, cur = str(s["session_date"]), best.get(str(s["session_date"]))
        rank = (s["data_mode"] == "live_capture", str(s["created_at"]))
        if cur is None or rank > (cur["data_mode"] == "live_capture", str(cur["created_at"])):
            best[day] = s
    return [best[d] for d in sorted(best)]


def study(md, snapshots: Iterable[Dict[str, Any]], variants: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    {'sessions': n, 'labels': {variant: {target: Counter(label or 'INELIGIBLE: reason')}},
     'quantiles': {metric: (n, [q10, q25, q50, q75, q90])}} - quantiles under the current rules.
    """
    params = {name: with_overrides(o) for name, o in variants.items()}
    labels = {name: {t: Counter() for t in labels_v2.TARGETS} for name in variants}
    values: Dict[str, List[float]] = {name: [] for name, _ in QUANTILE_METRICS}
    snapshots = list(snapshots)
    for snap in snapshots:
        s = cal.session(snap["session_date"])
        ref = snap["reference_values"]
        A, ONH, ONL = finite(ref.get("A")), finite(ref.get("ONH")), finite(ref.get("ONL"))
        df, _, _ = md.bars(int(snap["instrument_id"]), s.rth_open_at, s.scheduled_close_at)
        for name, p in params.items():
            res = labels_v2.compute_metrics(df, s, A, ONH, ONL, p)
            for target, o in labels_v2.compute_labels(res["metrics"], res["status"], s, p).items():
                labels[name][target][o["label"] or f"INELIGIBLE: {o['status']}"] += 1
        base = labels_v2.compute_metrics(df, s, A, ONH, ONL)["metrics"]
        for name, fn in QUANTILE_METRICS:
            v = fn(base) if fn else base.get(name)
            if v is not None:
                values[name].append(float(v))
    quantiles = {name: (len(v), [float(np.quantile(v, q)) for q in QUANTILES] if v else [])
                 for name, v in values.items()}
    return {"sessions": len(snapshots), "labels": labels, "quantiles": quantiles}


def format_report(result: Dict[str, Any], variants: Dict[str, Dict[str, Any]]) -> str:
    lines = [f"{result['sessions']} session(s), one snapshot each. Label shares are of the eligible "
             f"sessions; ineligible ones are counted separately.", ""]
    for target in labels_v2.TARGETS:
        vocab = list(labels_v2.TARGETS[target]["labels"])
        lines.append(f"== {target}")
        header = f"{'variant':20} {'elig':>5} " + " ".join(f"{lab[:18]:>18}" for lab in vocab) + "  ineligible"
        lines.append(header)
        for name in variants:
            c = result["labels"][name][target]
            elig = sum(c[lab] for lab in vocab)
            shares = " ".join(f"{(f'{c[lab]} ({100 * c[lab] / elig:.0f}%)' if elig else '-'):>18}" for lab in vocab)
            bad = ", ".join(f"{k[len('INELIGIBLE: '):]} {v}" for k, v in sorted(c.items())
                            if k.startswith("INELIGIBLE: "))
            lines.append(f"{name:20} {elig:5d} {shares}  {bad}")
        lines.append("")
    lines.append("Metric quantiles over the sessions where each is measured (current rules; units: daily ATR "
                 "unless a ratio):")
    lines.append(f"{'metric':24} {'n':>4} " + " ".join(f"{'q' + str(int(q * 100)):>7}" for q in QUANTILES))
    for name, (n, qs) in result["quantiles"].items():
        lines.append(f"{name:24} {n:4d} " + " ".join(f"{q:7.3f}" for q in qs))
    lines.append("")
    lines.append("Variants:")
    for name, o in variants.items():
        lines.append(f"  {name}: " + (", ".join(f"{k}={v}" for k, v in o.items()) or "labels_v2.PARAMETERS"))
    return "\n".join(lines)
