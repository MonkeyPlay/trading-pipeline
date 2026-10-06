# forecaster/fan_harness.py
"""
Scoring for the intermarket fan experiment (docs/fan_experiment.md), as its manifest
fixes it before any result:

  crps(z, Q)               the CRPS of a standardised error z against standardised
                           quantiles Q at the TAU levels: 2/K x the summed pinball loss
                           (K = 200); times sigma it is the CRPS of the log price
  compare_session(...)     one session, two versions on identical origins: per horizon
                           each one's mean CRPS in basis points, and the pre-open slice
  paired(rows, key)        the mean difference over sessions (date order) and its
                           moving-block bootstrap interval
  baseline_gate(conn, name)   chunk 2: fan_rw_v2 against fan_rw_v1 for the primary target
                           over the checks' sessions - which one is the baseline
  write_gate_report(...)   docs/reports/fan_rw_v2_gate_<experiment>.md

An origin is scored where the baseline has a positive variance and both ends have a
price inside the trading day, for both versions alike. Chunk 3 extends this harness to
the learned model (the checks' training, phases, attribution).
"""

from __future__ import annotations

import os
from datetime import date, time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

import numpy as np

from contracts import fan as F
from forecaster import fan_benchmark as fb
from forecaster import fan_experiment as fx
from forecaster import fan_v2
from forecaster.experiments import block_bootstrap
from forecaster.fan_benchmark import DAY_SLOTS, Day, FanModel, InsufficientHistory, slot_of_time
from forecaster.fan_data import history_start, load_days

K = F.SHAPE_LEVELS
TAU = fan_v2.TAU
NORMAL_Q = fan_v2.NORMAL_Q
REPORT_HORIZONS = (1, 5, 15, 20, 30, 60, 120, 240)      # the manifest's primary, secondary and exploratory minutes
PRE_OPEN_ORIGIN = slot_of_time(time(9, 28))              # the bar ending 09:29: the last completed by the P1 cutoff
PRE_OPEN_MINUTES = (16, 31, 61)                          # to 09:45, 10:00 and 10:30
Version = Tuple[FanModel, Callable[[int], np.ndarray]]   # a fitted model and its standardised quantiles per horizon
# How fan_rw_v2's changes were chosen: diagnostics on the development sessions before the checks (2025-07-21 to
# 2026-03-02), measured 2026-10-06; written into the gate's report (docs/fan.md, "Version 2", has the detail).
V2_NOTES = (
    "- **Releases by name:** at the release minute, CPI moved about 140x its usual minute variance (median), "
    "payrolls about 50x, PPI about 28x, ISM Manufacturing about 2x - one 'high' multiplier fitted none of them. Per "
    "release, shrunk towards the group: CRPS of origins with a release ahead -2 % to -5 %, their 90 % band from "
    "about 82 % to 86-88 %.",
    "- **Earnings at the close:** the store dates an earnings release by its 8-K filing, which follows the market's "
    "reaction (AMZN and GOOGL showed nothing at their filing minute) and is not known in advance. Their own group "
    "at 16:00-17:00: origins reaching 16:00-17:00 from 89.9 % to 90.7 % held; the 16:15-17:00 minutes had been "
    "forecast at under half their realised variance.",
    "- **Fat-tailed shape:** the symmetric empirical shape of the last 120 sessions' standardised errors: CRPS "
    "-0.25 % to -0.32 % at 1, 15 and 60 minutes, every interval below zero; 40 sessions did as well as 120.",
    "- **The open:** no rule. The 09:30-10:30 minutes were forecast at 0.9-1.0 of their realised variance; the "
    "pre-open origins' shortfall came from the 08:30 releases. Two candidates changed nothing: stopping the "
    "last hour's level at 09:30, and a phase-bounded outlier cap.",
    "- **Combined, on those sessions:** CRPS -0.25 % (1 min), -0.40 % (15 min), -0.44 % (60 min), every interval "
    "below zero. The checks' sessions were not scored until v2 was registered.",
)


def crps(z: np.ndarray, Q: np.ndarray) -> np.ndarray:
    """Per error, 2/K x sum over k of the pinball loss at TAU[k] of z against Q[k]."""
    z = np.asarray(z, dtype=float)
    out = np.zeros(len(z))
    for k in range(K):
        d = z - Q[k]
        out += np.where(d >= 0, TAU[k] * d, (TAU[k] - 1) * d)
    return 2.0 * out / K


def _variances(model: FanModel, day: Day, horizons: Sequence[int]) -> Dict[int, np.ndarray]:
    V = fb.horizon_variances(model, day.returns, horizons)["full"]
    return {h: V[i] for i, h in enumerate(horizons)}


def compare_session(day: Day, base: Version, other: Version,
                    horizons: Sequence[int] = REPORT_HORIZONS) -> Dict[str, Tuple[float, float, int]]:
    """``{key: (base mean CRPS, other mean CRPS, origins)}`` in basis points - keys 'h<minutes>' over every scored
    origin and 'pre_open_<minutes>' for the slice's one origin - on the origins both score."""
    hs = sorted(set(horizons) | set(PRE_OPEN_MINUTES))
    vb, vo = _variances(base[0], day, hs), _variances(other[0], day, hs)
    lp = np.log(day.last_price)
    t = np.arange(DAY_SLOTS)
    out: Dict[str, Tuple[float, float, int]] = {}
    for h in hs:
        ok = np.isfinite(lp) & (t + h < day.end)
        ok[ok] &= np.isfinite(lp[(t + h)[ok]])
        ok &= np.isfinite(vb[h]) & (np.nan_to_num(vb[h]) > fan_v2.MIN_VARIANCE)
        ok &= np.isfinite(vo[h]) & (np.nan_to_num(vo[h]) > 0)
        if not ok.any():
            continue
        y = lp[t[ok] + h] - lp[t[ok]]
        cb = np.sqrt(vb[h][ok]) * crps(y / np.sqrt(vb[h][ok]), base[1](h)) * 1e4
        co = np.sqrt(vo[h][ok]) * crps(y / np.sqrt(vo[h][ok]), other[1](h)) * 1e4
        if h in horizons:
            out[f"h{h}"] = (float(cb.mean()), float(co.mean()), int(ok.sum()))
        if h in PRE_OPEN_MINUTES:
            sel = t[ok] == PRE_OPEN_ORIGIN
            if sel.any():
                out[f"pre_open_{h}"] = (float(cb[sel][0]), float(co[sel][0]), 1)
    return out


def paired(rows: Sequence[Dict[str, Tuple[float, float, int]]], key: str) -> Optional[Dict[str, Any]]:
    """The other version minus the base over the sessions holding ``key`` (date order): the means, the mean
    difference, its share of the base and the manifest's moving-block bootstrap interval."""
    pairs = [r[key] for r in rows if key in r]
    if not pairs:
        return None
    base = np.array([p[0] for p in pairs])
    other = np.array([p[1] for p in pairs])
    diffs = list(other - base)
    b = F.BOOTSTRAP
    interval = block_bootstrap(diffs, b["block_sessions"], b["resamples"], b["seed"], b["interval"])
    return {"sessions": len(pairs), "origins": int(sum(p[2] for p in pairs)), "base_crps_bps": float(base.mean()),
            "other_crps_bps": float(other.mean()), "diff_bps": float(np.mean(diffs)),
            "diff_share": float(np.mean(diffs) / base.mean()) if base.mean() else None,
            "interval": list(interval) if interval else None}


def _verdict(p: Optional[Dict[str, Any]]) -> str:
    if p is None or p["interval"] is None:
        return "no interval"
    lo, hi = p["interval"]
    return "better" if hi < 0 else "worse" if lo > 0 else "inconclusive"


def baseline_gate(conn, name: str = fx.EXPERIMENT_NAME) -> Dict[str, Any]:
    """
    The manifest's baseline rule: fan_rw_v2 against fan_rw_v1 for the primary target over the three checks'
    sessions, both walk-forward as they would have been issued (each session fitted on the sessions before it,
    v2's shape from the standardised errors of the sessions before it). v2 becomes the baseline when the whole
    95 % interval of its difference at the primary horizon lies below zero.
    """
    exp = fx.load_experiment(conn, name)
    m = exp["definition"]
    blocks = m["split"]["checks"]["blocks"]
    first, last = blocks[0]["sessions"]["first"], blocks[-1]["sessions"]["last"]
    fx.guard(conn, first, last)                                       # development only
    target = m["targets"]["primary"]
    primary = f"h{m['horizons']['primary']['minutes']}"
    wanted = [d for d in m["split"]["development"]["sessions"]
              if first <= d <= last and d not in set(m["data"]["excluded"][target])]
    d0, d1 = date.fromisoformat(first), date.fromisoformat(last)
    days = load_days(conn, target, history_start(d0, F.EVENT_SESSIONS + F.SHAPE_SESSIONS), d1)
    ordered = sorted(days, key=lambda d: d.session_date)
    v1 = {}
    for i, d in enumerate(ordered):
        if d0 <= d.session_date <= d1 and d.complete and d.schedule == "full":
            try:
                v1[d.session_date.isoformat()] = fb.fit(d, ordered[:i])
            except InsufficientHistory:
                continue
    rows, scored = [], []
    normal = lambda h: NORMAL_Q
    for d, model, shape in fan_v2.walk_forward(days, d0, d1):
        key = d.session_date.isoformat()
        if key not in wanted or key not in v1:
            continue
        rows.append(compare_session(d, (v1[key], normal), (model, shape.at)))
        scored.append(key)
    keys = [f"h{h}" for h in REPORT_HORIZONS] + [f"pre_open_{h}" for h in PRE_OPEN_MINUTES]
    results = {k: paired(rows, k) for k in keys}
    gate = results[primary]
    baseline = F.FAN_V2_VERSION if _verdict(gate) == "better" else F.FAN_VERSION
    return {
        "kind": "baseline_gate", "experiment": name, "experiment_hash": exp["definition_hash"],
        "rule": m["baselines"]["v2_rule"], "target": target, "primary": primary,
        "base": {"version": F.FAN_VERSION, "definition_hash": F.fan_record()["definition_hash"]},
        "candidate": {"version": F.FAN_V2_VERSION, "definition_hash": F.fan_v2_record()["definition_hash"]},
        "sessions": {"first": first, "last": last, "wanted": len(wanted), "scored": scored,
                     "missing": [d for d in wanted if d not in scored]},
        "uncertainty": dict(F.BOOTSTRAP),
        "results": results, "verdicts": {k: _verdict(v) for k, v in results.items()},
        "baseline": baseline,
    }


def _f(x, nd=4):
    return "-" if x is None else f"{x:.{nd}f}"


def write_gate_report(res: Dict[str, Any], report_dir: str, diagnostics: Sequence[str] = ()) -> str:
    """The gate's report: docs/reports/fan_rw_v2_gate_<experiment>.md."""
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"fan_rw_v2_gate_{res['experiment']}.md")
    s = res["sessions"]
    rev = res.get("code_revision", "")
    rev = rev[:12] + ("+dirty" if rev.endswith("+dirty") else "")
    lines = [f"# Baseline gate: fan_rw_v2 against fan_rw_v1 ({res['experiment']})", "",
             f"Experiment hash `{res['experiment_hash'][:16]}`; fan_rw_v1 `{res['base']['definition_hash'][:16]}`, "
             f"fan_rw_v2 `{res['candidate']['definition_hash'][:16]}`; code {rev}.", "",
             f"**Rule** (fixed in the manifest before any result): {res['rule']}.", "",
             f"**Sessions:** the three checks, {s['first']} to {s['last']}: {len(s['scored'])} of {s['wanted']} scored"
             + (f" (missing: {', '.join(s['missing'])})" if s["missing"] else "") + f"; target {res['target']}. "
             "Both versions walk forward as they would have been issued: each session fitted on the sessions before "
             "it, v2's shape from the standardised errors of the 120 sessions before it. CRPS of the log price in "
             "basis points, the manifest's quantile form (K = 200) on identical origins; v2 minus v1 per session, "
             "95 % moving-block bootstrap (5-session blocks, 2000 resamples).", "",
             f"## Decision: the baseline is **{res['baseline']}**", "",
             "| Horizon | Role | Sessions | Origins | v1 CRPS | v2 CRPS | v2 - v1 | Share | 95 % interval | Verdict |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    roles = {"h15": "primary", "h5": "secondary", "h30": "secondary", "h60": "secondary"}
    for k, p in res["results"].items():
        if p is None:
            continue
        role = roles.get(k, "secondary" if k.startswith("pre_open") else "exploratory")
        label = (f"{k[1:]} min" if k.startswith("h") else f"pre-open, 09:29 + {k.split('_')[-1]} min")
        iv = p["interval"]
        lines.append(f"| {label} | {role} | {p['sessions']} | {p['origins']:,} | {_f(p['base_crps_bps'])} | "
                     f"{_f(p['other_crps_bps'])} | {_f(p['diff_bps'], 5)} | "
                     f"{'-' if p['diff_share'] is None else f'{100 * p['diff_share']:+.2f} %'} | "
                     f"{'-' if not iv else f'[{iv[0]:+.5f}, {iv[1]:+.5f}]'} | {res['verdicts'][k]} |")
    if diagnostics:
        lines += ["", "## How v2 was chosen (development sessions before the checks)", ""] + list(diagnostics)
    lines.append("")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    return path
