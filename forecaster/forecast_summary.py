# forecaster/forecast_summary.py
"""
The forecast's presentation, built only from validated numbers (it replaces the LLM
synthesis): no generated text, no causal explanation, no confidence score that a model
did not produce.

  build(conn, day)   the session's runs, read back: per arm its status and class
                     probabilities, the ML forecasts' differences from A and B in
                     percentage points, whether a run is a reconstruction (a replay issued
                     after its replay deadline, contracts/nq_ml.replay_deadline), the
                     forecast in force and why (the session's first recorded delivery,
                     if decided by the replay deadline - one recorded later is shown as
                     such, forecaster/delivery.py), B's reference levels with their
                     distances from the cutoff price (points and multiples of T), the
                     instruments the multi-instrument model used, missed or found stale,
                     with their market times and ages, what was observed in each (its
                     moves in its own units), the data cutoff, the issue times, the model
                     versions and artifact hashes, and for every target the source of the
                     forecast shown - the ML forecast covers direction_15m only, B the rest;
                     and the seven-target bundles (contracts/nq_ml_bundle.py, shadow): per
                     bundle and target its status or reason, probabilities, class, head
                     family and differences from A and B in percentage points, the first
                     level's frozen price and the artifact - model output only, kept apart
                     from the forecast in force (a bundle is never a source)
  lines(summary)     the same as plain sentences - each states a measurement or a stored
                     field, never a cause
"""

from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Dict, List, Optional

from contracts import nq_forecast as fc
from contracts import nq_ml as ml
from contracts import nq_ml_bundle as mb
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal

WINDOW = {"direction_15m": "09:30-09:45 ET", "first_move_5m": "09:30-09:35 ET", "opening_type_15m": "09:30-09:45 ET",
          "opening_bias_30m": "09:30-10:00 ET", "session_type_rth": "the RTH session",
          "close_direction_rth": "the RTH session", "first_level_tested": "the RTH session"}


def _f(v) -> float:
    return float(Fraction(str(v))) if isinstance(v, str) and "/" in v else float(v)


def _et(value) -> Optional[str]:
    if not value:
        return None
    t = datetime.fromisoformat(str(value).replace("Z", "").replace(" ", "T"))
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return f"{t.astimezone(cal.NY_TZ):%Y-%m-%d %H:%M:%S} ET"


def _dist(run: Optional[Dict[str, Any]], target: str) -> Optional[Dict[str, float]]:
    p = ((run or {}).get("predictions") or {}).get(target) or {}
    return None if not p.get("distribution") else {c: _f(v) for c, v in p["distribution"].items()}


def build(conn, day: str, profile: str = ml.PROFILE, mode: str = "historical_replay") -> Dict[str, Any]:
    """The session's summary (see the module docstring): the newest run of each arm in ``mode``."""
    runs = {}
    for r in store.list_forecast_runs(conn, day, day, profile, mode):     # newest first within the session
        arm = ml.arm_of(r["algorithm_version"])
        if arm is not None and arm not in runs:
            runs[arm] = store.get_forecast_run(conn, r["run_id"])
    delivery = store.first_delivery(conn, day, profile, mode)
    late = None
    if delivery is not None and mode == "historical_replay" and runs:
        cutoff = next(iter(runs.values()))["input_cutoff_at"]
        if ml._instant(delivery["decided_at"]) > ml.replay_deadline(ml._instant(cutoff)):
            late, delivery = delivery, None       # recorded after the fact: not the forecast in force that morning
    a, b = _dist(runs.get("A"), ml.TARGET), _dist(runs.get("B"), ml.TARGET)
    arms = {}
    for arm, run in runs.items():
        d = _dist(run, ml.TARGET)
        entry = {"version": run["algorithm_version"], "label": ml.ARM_LABELS.get(run["algorithm_version"]),
                 "status": run["lifecycle_status"], "reason": run["failure_reason"],
                 "issued_at": _et(run["issued_at"]), "probabilities": d,
                 "class": ((run.get("predictions") or {}).get(ml.TARGET) or {}).get("predicted_label"),
                 "experimental": run["algorithm_version"] in ml.ALGORITHMS
                 and ml.STATUS[run["algorithm_version"]] != "production",
                 "reconstruction": ml.reconstruction(run)}
        if d and arm not in ("A",):
            if a:
                entry["minus_A_pp"] = {c: round(100 * (d[c] - a[c]), 1) for c in ml.CLASSES}
            if b and arm != "B":
                entry["minus_B_pp"] = {c: round(100 * (d[c] - b[c]), 1) for c in ml.CLASSES}
        ev = run.get("evidence") or {}
        if ev.get("model"):
            entry["model"] = {"family": ev["model"].get("family"), "sha256": ev["model"].get("sha256"),
                              "trained": [ev["model"]["training"]["from"], ev["model"]["training"]["to"]]
                              if ev["model"].get("training") else None}
        arms[arm] = entry
    snap_run = runs.get("B") or runs.get("A") or next(iter(runs.values()), None)
    out: Dict[str, Any] = {"session_date": day, "profile": profile, "mode": mode, "target": ml.TARGET,
                           "window": WINDOW[ml.TARGET], "cutoff": _et(snap_run["input_cutoff_at"]) if snap_run else None,
                           "arms": arms,
                           "in_force": None if delivery is None else {
                               "run_id": delivery["run_id"], "label": ml.ARM_LABELS.get(delivery["algorithm"]),
                               "reason": delivery["reason"], "decided_at": _et(delivery["decided_at"])},
                           "late_delivery": None if late is None else {
                               "label": ml.ARM_LABELS.get(late["algorithm"]), "decided_at": _et(late["decided_at"])},
                           "sources": {}, "levels": None, "instruments": None, "observed": None}
    # every target's source: the ML forecast in force covers direction_15m; B every other target (A without B)
    covered = delivery and delivery.get("algorithm") in ml.ALGORITHMS
    for _, t in fc.FORECAST_TARGETS:
        src = (delivery["algorithm"] if covered and t == ml.TARGET else
               fc.BASELINE_VERSION if "B" in runs else fc.PRIOR_VERSION if "A" in runs else None)
        out["sources"][t] = {"source": ml.ARM_LABELS.get(src) if src else None, "window": WINDOW[t]}
    out["bundles"] = _bundles(conn, day, profile, mode, runs)
    if "B" in runs:
        refs = (runs["B"].get("outputs") or {}).get("reference_targets") or {}
        t = ((runs["B"].get("evidence") or {}).get("thresholds") or {}).get("T")
        levels = {}
        for side in ("upside", "downside"):
            levels[side] = [{"id": x["id"], "price": _f(x["price"]), "points": _f(x["distance"]),
                             "in_T": None if not t else round(_f(x["distance"]) / _f(t), 2)}
                            for x in refs.get(side) or []]
        out["levels"] = {"cutoff_price": refs.get("cutoff_price"), "T": t, **levels}
    multi = runs.get("M")
    if multi is not None:
        ev = multi.get("evidence") or {}
        out["instruments"] = {s: {"status": (st or {}).get("status"), "market_ts": (st or {}).get("market_ts"),
                                  "age_min": (st or {}).get("age_min")}
                              for s, st in (ev.get("instruments") or {}).items()}
        out["observed"] = ev.get("observations")
        out["as_of"] = ev.get("as_of")
    return out


def _bundles(conn, day: str, profile: str, mode: str, runs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The newest run of each seven-target bundle (shadow): per target status, class, probabilities, reason, head
    family and differences from A's and B's distributions of the same target."""
    out: Dict[str, Any] = {}
    for r in store.list_forecast_runs(conn, day, day, profile, mode):     # newest first within the session
        arm = mb.display_arm_of(r["algorithm_version"])
        if arm not in mb.DISPLAY_ARMS or arm in out:
            continue
        run = store.get_forecast_run(conn, r["run_id"])
        heads = (run.get("outputs") or {}).get("heads") or {}
        targets = {}
        for t in mb.TARGETS:
            p = (run.get("predictions") or {}).get(t) or {}
            d = _dist(run, t)
            e = {"status": p.get("status", "missing"), "class": p.get("predicted_label"), "probabilities": d,
                 "reason": p.get("reason"), "family": (heads.get(t) or {}).get("family"),
                 "label_coverage": None if not p else {"labelled": p.get("eligible"),
                                                       "without_label": p.get("without_label")}}
            for base in ("A", "B"):
                bd = _dist(runs.get(base), t)
                if d and bd:
                    e[f"minus_{base}_pp"] = {c: round(100 * (d[c] - bd[c]), 1) for c in mb.CLASSES[t]}
            targets[t] = e
        out[arm] = {"version": run["algorithm_version"], "label": mb.ARM_LABELS[run["algorithm_version"]],
                    "status": run["lifecycle_status"], "reason": run["failure_reason"],
                    "issued_at": _et(run["issued_at"]), "reconstruction": ml.reconstruction(run), "shadow": True,
                    "sha256": ((run.get("outputs") or {}).get("model") or {}).get("sha256"),
                    "first_level_price": (run.get("outputs") or {}).get("first_level_price"), "targets": targets}
    return out


def _pct(d: Optional[Dict[str, float]]) -> str:
    return "-" if not d else ", ".join(f"{defs.display(ml.TARGET, c, 'predicted')} {100 * d[c]:.0f} %"
                                       for c in ml.CLASSES)


def lines(s: Dict[str, Any]) -> List[str]:
    """The summary as plain sentences: measurements and stored fields only."""
    out = [f"{s['session_date']}: {ml.TARGET} over {s['window']}, from data to the {s['cutoff'] or '?'} cutoff "
           f"({s['mode'].replace('_', ' ')})."]
    f = s.get("in_force")
    late = s.get("late_delivery")
    out.append(f"In force: {f['label'] or 'no forecast'} - {f['reason']} (decided {f['decided_at']})." if f else
               f"In force: none recorded by the replay deadline - {late['label']} was recorded after it "
               f"({late['decided_at']}), a reconstruction." if late else
               "In force: not recorded for this session (none is recorded after its replay deadline).")
    for arm, e in s["arms"].items():
        head = (f"{arm} {e['label'] or e['version']}" + (" [experimental]" if e["experimental"] else "")
                + (" [reconstruction: issued after the replay deadline, never in force]" if e["reconstruction"]
                   else ""))
        if e["status"] != "issued" or not e["probabilities"]:
            out.append(f"{head}: {e['status']}" + (f" - {e['reason']}" if e["reason"] else "") + ".")
            continue
        text = f"{head}: {_pct(e['probabilities'])}; issued {e['issued_at']}"
        if e.get("minus_A_pp"):
            text += "; minus A " + ", ".join(f"{c} {v:+.1f} pp" for c, v in e["minus_A_pp"].items())
        if e.get("minus_B_pp"):
            text += "; minus B " + ", ".join(f"{c} {v:+.1f} pp" for c, v in e["minus_B_pp"].items())
        if e.get("model"):
            text += (f"; {e['model']['family']} model {str(e['model']['sha256'])[:12]}, trained "
                     f"{e['model']['trained'][0]} to {e['model']['trained'][1]}" if e["model"]["trained"] else "")
        out.append(text + ".")
    lv = s.get("levels")
    if lv:
        parts = []
        for side in ("upside", "downside"):
            for x in lv[side]:
                parts.append(f"{x['id']} {x['price']:.2f} ({side}, {x['points']:.2f} points"
                             + (f", {x['in_T']} T" if x["in_T"] is not None else "") + ")")
        out.append(f"Reference levels from the cutoff price {lv['cutoff_price']} (T = {lv['T']}): "
                   + ("; ".join(parts) or "none valid") + ".")
    if s.get("instruments"):
        used = [f"{k} ({v['status']}, {v['age_min']} min old at the cutoff)" for k, v in s["instruments"].items()
                if v["status"] in ("observed", "reconstructed", "closed", "delayed")]
        missing = [f"{k} ({v['status']})" for k, v in s["instruments"].items()
                   if v["status"] not in ("observed", "reconstructed", "closed", "delayed")]
        out.append("Instruments used by the multi-instrument model: " + (", ".join(used) or "none")
                   + ("; missing or stale: " + ", ".join(missing) if missing else "") + ".")
    if s.get("observed"):
        obs = []
        for k, v in s["observed"].items():
            if v.get("since_close") is not None:
                obs.append(f"{k} {v['since_close']:+.2f} {v['unit']} since the previous close"
                           + (f", {v['since_0800']:+.2f} {v['unit']} since 08:00" if v.get("since_0800") is not None
                              else ""))
        if obs:
            out.append("Observed to the cutoff: " + "; ".join(obs) + ".")
    covered = [t for t, v in s["sources"].items() if v["source"]]
    if covered:
        out.append("Sources: " + "; ".join(f"{t} - {s['sources'][t]['source']}" for t in covered) + ".")
    for arm, b in (s.get("bundles") or {}).items():
        head = (f"{arm} {b['label']} [shadow: model output only, never in force]"
                + (" [reconstruction: issued after the replay deadline]" if b["reconstruction"] else ""))
        if b["status"] != "issued":
            out.append(f"{head}: {b['status']}" + (f" - {b['reason']}" if b["reason"] else "") + ".")
            continue
        parts = []
        for t, e in b["targets"].items():
            if not e["probabilities"]:
                parts.append(f"{t} unavailable ({str(e['reason']).split(':')[0]})")
                continue
            top = max(e["probabilities"], key=e["probabilities"].get)
            txt = f"{t} {defs.display(t, top, 'predicted')} {100 * e['probabilities'][top]:.0f} %"
            if e.get("minus_A_pp"):
                txt += f" ({e['minus_A_pp'][top]:+.1f} pp vs A"
                txt += f", {e['minus_B_pp'][top]:+.1f} pp vs B)" if e.get("minus_B_pp") else ")"
            parts.append(txt + (f" [{e['family']}]" if e.get("family") else ""))
        out.append(f"{head}: " + "; ".join(parts) + (f"; first level price {b['first_level_price']}"
                                                     if b.get("first_level_price") is not None else "")
                   + f"; issued {b['issued_at']}; artifact {str(b['sha256'])[:12]}.")
    return out
