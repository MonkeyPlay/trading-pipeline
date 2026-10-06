# forecaster/fan_live.py
"""
The intermarket fan experiment as the dashboard draws it (docs/fan_experiment.md, chunk 9).
fan_rw_v2 - the experiment's baseline since the gate - for every instrument with a fan; and
for the experiment's primary target the frozen model, at exactly the horizons whose own
holdout interval lay below zero - the manifest's drawing rule: "the model draws a target's
horizon only where its own holdout interval for that target and horizon lies below zero;
the baseline draws the rest". The holdout opened when the model was frozen, so the
explorer's exemption - fan_rw_v1 drawn until then - has ended.

  drawn(conn, name)             the frozen model and the horizons the stored holdout result
                                lets it draw - none before the holdout is scored, with why
  v2_shape(days, symbol)        the shape v2 issues for the session after ``days``
  v2_accuracy(days, before, ..) v2's accuracy as drawn, walk-forward over the sessions before a
                                day - each session fitted on the sessions before it and drawn
                                with the shape it was issued with: per horizon the CRPS of that
                                distribution, its skill against a flat random walk with normal
                                errors, and how often its 50 and 90 % bands held
  ModelDraw, load_model(...)    the frozen model with the sessions before the day already read
                                (the panel's history), so an origin needs only the day itself
  recorded(conn, md, origin)    the forward record's issue from an origin, if one was made: the
                                immutable forecast as issued, at the horizons the model draws
  model_marks(...)              the frozen model's quantiles at its drawn horizons from an
                                origin, computed from its stored definition and the bars stored
                                now - never refitted, but not a record: a bar revised or arriving
                                after the moment changes it
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from contracts import fan as F
from database import journal_store as store
from features import calendar as cal
from forecaster import fan_benchmark as fb
from forecaster import fan_experiment as fx
from forecaster import fan_features as ff
from forecaster import fan_forward as fwd
from forecaster import fan_harness as fh
from forecaster import fan_model as fm
from forecaster import fan_panel as fp
from forecaster import fan_v2
from forecaster.fan_benchmark import DAY_SLOTS, Day, InsufficientHistory
from forecaster.fan_scoring import PHASE_OF_SLOT

ACCURACY_SESSIONS = 30
ACCURACY_FORMAT = 2                      # 2: walk-forward shapes, the drawn distribution scored (1: one shape, normal)
HISTORY_SESSIONS = 35                    # the forward record's and the replay's: 20 full sessions for the usual
LEVELS = np.array(F.QUANTILES)           # the chart's quantiles


def drawn(conn, name: str = fx.EXPERIMENT_NAME) -> Tuple[Optional[Dict[str, Any]], List[int], str]:
    """``(frozen model record, horizons it draws, why not)``: the horizons of the stored holdout result whose whole
    interval lay below zero, in minutes - empty, with the reason, while there is no frozen model or no scored
    holdout."""
    try:
        exp = fx.load_experiment(conn, name)
    except ValueError:
        return None, [], f"no registered experiment {name}"
    model = fx.frozen_model(conn, exp)
    if model is None:
        return None, [], f"{name}'s holdout is sealed: no frozen model"
    done = [r for r in store.experiment_results(conn, name) if r["results"].get("kind") == "holdout"]
    if not done:
        return model, [], f"{model['version']} is frozen but its holdout is not scored yet"
    verdicts = done[0]["results"]["verdicts"]
    horizons = [h for h in fh.REPORT_HORIZONS if verdicts.get(f"h{h}") == "better"]
    return model, horizons, "" if horizons else "no horizon's holdout interval lay below zero"


# --------------------------------------------------------------------------
# v2's shape and its accuracy as drawn
# --------------------------------------------------------------------------

def _errors(ordered: Sequence[Day], i: int, symbol: str, cache_dir: Optional[str],
            model: Optional[fan_v2.FanModelV2] = None) -> Optional[Dict[int, np.ndarray]]:
    """v2's standardised errors of the complete full session ``ordered[i]`` on its own fit from the sessions before
    it (``model``, when already fitted) - read from the forward record's per-session cache where computed before,
    and written there (atomically) when not. None without the history for a fit."""
    d = ordered[i]
    path = fwd._errors_path(cache_dir, symbol, d.session_date) if cache_dir else None
    if path and os.path.exists(path):
        with np.load(path) as z:
            return {int(k[1:]): z[k] for k in z.files}
    if model is None:
        try:
            model = fan_v2.fit(d, ordered[:i])
        except InsufficientHistory:
            return None
    e = fan_v2.errors(model, d)
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path[:-len('.npz')]}.{os.getpid()}.tmp.npz"
        np.savez(tmp, **{f"h{h}": v for h, v in e.items()})
        os.replace(tmp, path)
    return e


def v2_shape(days: Sequence[Day], symbol: str, cache_dir: Optional[str] = None) -> fan_v2.Shape:
    """fan_rw_v2's shape for the session after ``days`` (complete sessions before it), as a Shape for any minutes
    ahead - walk_forward's: the last SHAPE_SESSIONS sessions' standardised errors, each from its own fit, read from
    the forward record's session-by-session cache where computed before (forecaster/fan_forward.py)."""
    ordered = sorted(days, key=lambda d: d.session_date)
    usable = [i for i, d in enumerate(ordered) if d.complete and d.schedule == "full"]
    errs: List[Dict[int, np.ndarray]] = []
    for i in reversed(usable):
        if len(errs) == F.SHAPE_SESSIONS:
            break
        e = _errors(ordered, i, symbol, cache_dir)
        if e is None:
            break                                           # walk_forward has no error for it, nor for any before
        errs.append(e)
    return fan_v2.shape_from(list(reversed(errs)))


def _tally(tallies: Dict[int, Dict[str, float]], model: fan_v2.FanModelV2, day: Day, shape: fan_v2.Shape) -> None:
    """Adds one session's origins to ``tallies``: per score horizon v2's CRPS under ``shape`` and the flat reference's
    under the normal (both the manifest's quantile form, basis points), and v2's 50 and 90 % bands held."""
    V = fb.horizon_variances(model, day.returns, F.SCORE_HORIZONS)
    lp = np.log(day.last_price)
    t = np.arange(DAY_SLOTS)
    for i, h in enumerate(F.SCORE_HORIZONS):
        vf, vr = V["full"][i], V["flat"][i]
        ok = np.isfinite(lp) & (t + h < day.end)
        ok[ok] &= np.isfinite(lp[(t + h)[ok]])
        ok &= np.isfinite(vf) & (np.nan_to_num(vf) > fan_v2.MIN_VARIANCE)
        ok &= np.isfinite(vr) & (np.nan_to_num(vr) > fan_v2.MIN_VARIANCE)
        if not ok.any():
            continue
        y = lp[t[ok] + h] - lp[t[ok]]
        sigma, flat = np.sqrt(vf[ok]), np.sqrt(vr[ok])
        Q = shape.at(h)
        a = np.abs(y / sigma)
        acc = tallies.setdefault(h, {"crps": 0.0, "flat": 0.0, "in50": 0, "in90": 0, "n": 0})
        acc["crps"] += float((sigma * fh.crps(y / sigma, Q)).sum() * 1e4)
        acc["flat"] += float((flat * fh.crps(y / flat, fan_v2.NORMAL_Q)).sum() * 1e4)
        acc["in50"] += int((a <= np.interp(0.75, fh.TAU, Q)).sum())
        acc["in90"] += int((a <= np.interp(0.95, fh.TAU, Q)).sum())
        acc["n"] += int(ok.sum())


def _walk(days: Sequence[Day], before: date, sessions: int, symbol: str, cache_dir: Optional[str]
          ) -> Iterator[Tuple[Day, fan_v2.FanModelV2, fan_v2.Shape]]:
    """``(day, model, shape)`` of the last ``sessions`` complete full sessions of ``days`` before ``before``, oldest
    first: each fitted on the sessions before it, with the shape it was issued with - fan_v2.walk_forward's, from the
    standardised errors of the SHAPE_SESSIONS sessions before it, each on its own fit (cached per session)."""
    ordered = sorted((d for d in days if d.session_date < before), key=lambda d: d.session_date)
    usable = [i for i, d in enumerate(ordered) if d.complete and d.schedule == "full"]
    picks = set(usable[-sessions:])
    errs: List[Dict[int, np.ndarray]] = []                  # the sessions before the one in hand, oldest first
    for i in usable[max(0, len(usable) - sessions - F.SHAPE_SESSIONS):]:
        d = ordered[i]
        model = None
        if i in picks:
            try:
                model = fan_v2.fit(d, ordered[:i])
            except InsufficientHistory:
                continue
            yield d, model, fan_v2.shape_from(errs[-F.SHAPE_SESSIONS:])
        e = _errors(ordered, i, symbol, cache_dir, model)
        if e is not None:
            errs.append(e)


def v2_accuracy(days: Sequence[Day], before: date, symbol: str, sessions: int = ACCURACY_SESSIONS,
                cache_dir: Optional[str] = None, cache_path: Optional[str] = None) -> Dict[str, Any]:
    """
    fan_rw_v2's accuracy as the chart draws it, walk-forward over the last ``sessions`` complete full sessions of
    ``days`` before ``before`` (_walk): each session fitted on the sessions before it and drawn with the shape it was
    issued with - never its own errors or a later session's. Per score horizon over every origin: the CRPS of that
    distribution (the manifest's quantile form, basis points), its skill against a flat random walk with normal
    errors (one volatility for every minute: 1 - CRPS v2 / CRPS flat) and how often its central 50 and 90 % bands
    held. Cached at ``cache_path``.
    """
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as fh_:
            return json.load(fh_)
    tallies: Dict[int, Dict[str, float]] = {}
    scored: List[str] = []
    for d, model, shape in _walk(days, before, sessions, symbol, cache_dir):
        _tally(tallies, model, d, shape)
        scored.append(d.session_date.isoformat())
    out: Dict[str, Any] = {
        "sessions": len(scored), "first": scored[0] if scored else None, "last": scored[-1] if scored else None,
        "horizons": [], "version": F.FAN_V2_VERSION, "format": ACCURACY_FORMAT,
        "method": f"walk-forward: each session fitted on the sessions before it and drawn with the shape it was issued "
                  f"with (the standardised errors of the {F.SHAPE_SESSIONS} sessions before it); the CRPS of that "
                  f"distribution in the manifest's quantile form ({F.SHAPE_LEVELS} levels); the reference a flat "
                  "random walk with normal errors"}
    for h in F.SCORE_HORIZONS:
        t = tallies.get(h)
        if not t or not t["n"]:
            continue
        out["horizons"].append({"horizon": h, "origins": int(t["n"]), "crps_bps": t["crps"] / t["n"],
                                "flat_crps_bps": t["flat"] / t["n"],
                                "skill": 1 - t["crps"] / t["flat"] if t["flat"] > 0 else None,
                                "cover50": t["in50"] / t["n"], "cover90": t["in90"] / t["n"]})
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as fh_:
            json.dump(out, fh_)
    return out


# --------------------------------------------------------------------------
# The frozen model: recorded and computed
# --------------------------------------------------------------------------

@dataclass
class ModelDraw:
    """The frozen model ready to draw on one session: its definition, the horizons it draws, the instruments it reads
    and the panel of the sessions before the day (read once)."""
    version: str
    definition: Dict[str, Any]
    horizons: List[int]
    target: str
    symbols: List[str]
    first_complete: Dict[str, Optional[str]]
    history: fp.Panel
    session: date


def load_model(conn, symbol: str, day: date, name: str = fx.EXPERIMENT_NAME) -> Tuple[Optional[ModelDraw], str]:
    """The frozen model for ``symbol``'s session ``day`` - only the experiment's primary target has one - or None and
    why not."""
    model, horizons, why = drawn(conn, name)
    if model is None or not horizons:
        return None, why
    m = fx.load_experiment(conn, name)["definition"]
    target = m["targets"]["primary"]
    if symbol != target:
        return None, f"the learned fan forecasts {target} only"
    definition = model["definition"]
    symbols = sorted({n.split(".")[0] for n in definition["features"] if not n.startswith("base.")})
    avail = m["instruments"]["availability"]
    window = [s.session_date.isoformat() for s in cal.sessions_before(day, HISTORY_SESSIONS)]
    history = fp.load_panel(conn, window, symbols)
    return ModelDraw(model["version"], definition, horizons, target, symbols,
                     {s: avail[s]["first_complete"] for s in symbols}, history, day), ""


def recorded(conn, md: ModelDraw, origin: int) -> Optional[Dict[str, Any]]:
    """
    The forward record's issue from slot ``origin`` of the day (forecaster/fan_forward.py), if one was made: the
    immutable forecast as issued - per horizon the model draws, its quantiles and v2's as recorded, the multiplier,
    and the horizon's class under the rules the issue was made under - with the mark, when the issue was recorded
    and the origin price as read then. None without one.
    """
    row = conn.execute(
        "SELECT i.issue_id, extract(epoch FROM i.mark_at), extract(epoch FROM i.recorded_at), i.origin_price, "
        "i.forecast, i.record, v.definition FROM journal.fan_forward_issues i LEFT JOIN journal.definition_versions v "
        "ON v.version = i.record WHERE i.model = %s AND i.target = %s AND i.session_date = %s AND i.origin_slot = %s;",
        (md.version, md.target, md.session, origin)).fetchone()
    if row is None:
        return None
    issue_id, mark, at, price, forecast, record, rules = row
    forecast = json.loads(forecast) if isinstance(forecast, str) else forecast
    params = fwd.rules_params(json.loads(rules) if isinstance(rules, str) else rules)
    mark_at = datetime.fromtimestamp(float(mark), timezone.utc)
    recorded_at = datetime.fromtimestamp(float(at), timezone.utc)
    marks = []
    for h in md.horizons:
        f = forecast.get(str(h))
        if f is None:
            continue
        marks.append({"minutes": h, "multiplier": float(f["multiplier"]), "sigma": float(f["sigma"]),
                      "q": [round(float(x), 2) for x in f["model"]], "base": [round(float(x), 2) for x in f["base"]],
                      "class": fwd.classify(params, mark_at, recorded_at, h) if params else "legacy"})
    return {"issue_id": str(issue_id), "mark_at": mark_at, "recorded_at": recorded_at, "origin_price": float(price),
            "record": record, "marks": marks}


def _with_day(history: fp.Panel, today: fp.Panel) -> fp.Panel:
    return fp.Panel(history.sessions + today.sessions, history.symbols,
                    np.concatenate([history.close, today.close]), np.concatenate([history.age, today.age]),
                    np.concatenate([history.volume, today.volume]), np.concatenate([history.contract, today.contract]))


def model_marks(conn, md: ModelDraw, day: Day, v2model: fan_v2.FanModelV2, Q: np.ndarray, origin: int
                ) -> List[Dict[str, Any]]:
    """The frozen model's distribution of the price at each of its drawn horizons from slot ``origin`` (the close
    there): v2's sigma times the model's multiplier, with v2's shape, at the chart's quantiles. A horizon past the
    day's end is left out. The features read the day's bars to the origin only (a feature at a minute reads nothing
    after it - tested in fan_features) - but as stored now: not a record of what an earlier moment could read."""
    price = float(day.last_price[origin]) if 0 <= origin < len(day.closes) else float("nan")
    hs = [h for h in md.horizons if origin + h < day.end]
    if not np.isfinite(price) or not hs:
        return []
    today = fp.load_panel(conn, [md.session.isoformat()], md.symbols)
    table = ff.build(_with_day(md.history, today), md.target, md.first_complete)
    V = fb.horizon_variances(v2model, day.returns, tuple(hs))["full"][:, origin]
    keep = np.isfinite(V) & (V > fan_v2.MIN_VARIANCE)
    hs, V = [h for h, k in zip(hs, keep) if k], V[keep]
    if not hs:
        return []
    idx = [fh.FRAME_HORIZONS.index(h) for h in hs]
    slots = [slot for _, _, slot in fan_v2.placed(day)]
    n = len(hs)
    rows = fh.Rows(np.full(n, np.datetime64(md.session.isoformat(), "D")), np.full(n, origin), np.array(hs),
                   np.full(n, PHASE_OF_SLOT[origin]), np.array([bool(fh._ahead(slots, h)[origin]) for h in hs]), V,
                   np.full(n, np.nan), np.array([float(np.interp(0.75, fh.TAU, Q[i])) for i in idx]))
    predictor = fm.frozen_predictor(md.definition, table)
    mult = predictor.predict(rows)
    fm.check_columns(predictor, md.definition)
    out = []
    for h, i, v, mm in zip(hs, idx, V, mult):
        z = np.interp(LEVELS, fh.TAU, Q[i])
        sigma = float(np.sqrt(v))
        out.append({"minutes": int(h), "multiplier": float(mm), "sigma": sigma,
                    "q": [round(float(price * np.exp(sigma * mm * x)), 2) for x in z],
                    "base": [round(float(price * np.exp(sigma * x)), 2) for x in z]})
    return out
