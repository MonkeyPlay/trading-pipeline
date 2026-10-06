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

The checks (chunk 3): a candidate - a learned fan - against the decided baseline on the
manifest's three checks, each candidate trained on the development sessions before its
check only:

  Frame, baseline_frames(days, ...)   per session the baseline as issued (walk-forward):
                           its variance to t + h from every origin, its shape, the
                           releases ahead - what the candidate scales and is scored against
  load_frames(conn, name, target)     the development sessions' frames, cached on disk
  Rows, frame_rows(frame, every)      origin-horizon rows: the baseline's variance, the
                           realised move, the origin's phase; training samples every
                           TRAIN_EVERY minutes, the checks score every origin. Both add the
                           pre-open slice's origin at its 16, 31 and 61 minutes
  Candidate                fit(training rows), predict(rows) -> a multiplier of the
                           baseline's sigma per row; the fan keeps the baseline's shape
  Identity, PhaseScale     reference candidates: the baseline itself (every difference
                           exactly zero) and one constant width per horizon and phase
  run_checks(frames, ...)  per check: train a fresh candidate, score it and the baseline
                           on identical origins; paired intervals per check and pooled over
                           the checks' sessions - per horizon, horizon x phase, with a
                           release ahead, and the pre-open slice
  write_checks_report(...) docs/reports/fan_checks_<experiment>_<target>_<candidate>.md

An origin is scored where the baseline has a positive variance and both ends have a
price inside the trading day, for both versions alike. A check's result is development,
never the verdict: nothing from the checks is stored in the journal.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, time
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from contracts import fan as F
from database import journal_store as store
from forecaster import fan_benchmark as fb
from forecaster import fan_data
from forecaster import fan_experiment as fx
from forecaster import fan_v2
from forecaster.experiments import block_bootstrap
from forecaster.fan_benchmark import DAY_SLOTS, Day, FanModel, InsufficientHistory, slot_of_time
from forecaster.fan_data import history_start, load_days
from forecaster.fan_scoring import PHASE_OF_SLOT, PHASES

K = F.SHAPE_LEVELS
TAU = fan_v2.TAU
NORMAL_Q = fan_v2.NORMAL_Q
REPORT_HORIZONS = (1, 5, 15, 20, 30, 60, 120, 240)      # the manifest's primary, secondary and exploratory minutes
PRE_OPEN_ORIGIN = slot_of_time(time(9, 28))              # the bar ending 09:29: the last completed by the P1 cutoff
PRE_OPEN_MINUTES = (16, 31, 61)                          # to 09:45, 10:00 and 10:30
FRAME_HORIZONS = tuple(sorted(set(REPORT_HORIZONS) | set(PRE_OPEN_MINUTES)))
TRAIN_EVERY = 5                                          # training rows: origins on the 5-minute marks (slot 0 = 18:00)
FRAME_FORMAT = 1                                         # bump when a cached frame's content changes
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


# --------------------------------------------------------------------------
# The checks (chunk 3): the baseline as issued, per session
# --------------------------------------------------------------------------

@dataclass
class Frame:
    """One session of the baseline as it would have been issued: from every origin t, the variance of the log price
    to t + h and the standardised quantiles at each of FRAME_HORIZONS, and whether a release is placed in (t, t + h]."""
    session: str
    end: int                     # the first slot after the trading day
    log_price: np.ndarray        # (1440,) the log of the last price
    var: np.ndarray              # (len(FRAME_HORIZONS), 1440)
    shape: np.ndarray            # (len(FRAME_HORIZONS), K)
    ahead: np.ndarray            # (len(FRAME_HORIZONS), 1440) bool


def _ahead(slots: Sequence[int], h: int) -> np.ndarray:
    """True at origin t when one of the release ``slots`` falls in (t, t + h]."""
    marks = np.zeros(DAY_SLOTS + 1)
    for s in slots:
        if 0 <= s < DAY_SLOTS:
            marks[s + 1] += 1
    C = np.cumsum(marks)                           # C[k]: releases at slots below k
    t = np.arange(DAY_SLOTS)
    return (C[np.minimum(DAY_SLOTS, t + h + 1)] - C[t + 1]) > 0


def _issued(days: Sequence[Day], d0: date, d1: date, version: str) -> Iterator[Tuple[Day, FanModel, np.ndarray,
                                                                                         List[int]]]:
    """``(day, model, shape at FRAME_HORIZONS, release slots)`` of every complete full session from ``d0`` to ``d1``,
    walk-forward as ``version`` issues it."""
    if version == F.FAN_V2_VERSION:
        for d, model, shape in fan_v2.walk_forward(days, d0, d1):
            yield d, model, shape.at(np.array(FRAME_HORIZONS)), [slot for _, _, slot in fan_v2.placed(d)]
        return
    if version != F.FAN_VERSION:
        raise ValueError(f"no frames for the baseline {version!r}")
    ordered = sorted(days, key=lambda d: d.session_date)
    for i, d in enumerate(ordered):
        if d0 <= d.session_date <= d1 and d.complete and d.schedule == "full":
            try:
                model = fb.fit(d, ordered[:i])
            except InsufficientHistory:
                continue
            yield (d, model, np.tile(NORMAL_Q, (len(FRAME_HORIZONS), 1)),
                   [r.slot for r in d.releases] if d.releases_known else [])


def baseline_frames(days: Sequence[Day], d0: date, d1: date, version: str = F.FAN_V2_VERSION) -> Dict[str, Frame]:
    """The frame of every complete full session of ``days`` from ``d0`` to ``d1`` under the baseline ``version``,
    each fitted on the sessions of ``days`` before it."""
    out = {}
    for d, model, Q, slots in _issued(days, d0, d1, version):
        V = fb.horizon_variances(model, d.returns, FRAME_HORIZONS)["full"]
        key = d.session_date.isoformat()
        out[key] = Frame(key, d.end, np.log(d.last_price), V, np.asarray(Q, dtype=float),
                         np.vstack([_ahead(slots, h) for h in FRAME_HORIZONS]))
    return out


def decided_baseline(conn, name: str) -> str:
    """The baseline the experiment's gate decided (scripts/fan.py baseline-gate); ValueError before the gate."""
    gates = [r for r in store.experiment_results(conn, name) if r["results"].get("kind") == "baseline_gate"]
    if not gates:
        raise ValueError(f"{name}'s baseline is not decided yet: run scripts/fan.py baseline-gate first")
    return gates[-1]["results"]["baseline"]


def _sources_hash() -> str:
    """The code a frame is computed by: a changed estimator or loader invalidates the cache (a change to the frames
    here bumps FRAME_FORMAT)."""
    h = hashlib.sha256()
    for mod in (fb, fan_v2, fan_data):
        with open(mod.__file__, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def load_frames(conn, name: str, target: str, cache_dir: Optional[str] = None,
                refresh: bool = False) -> Tuple[Dict[str, Any], str, Dict[str, Frame]]:
    """
    ``(experiment, baseline version, frames)``: the decided baseline's frames of ``target``'s development sessions,
    walk-forward from the stored bars - read from ``cache_dir`` when computed before by the same code for the same
    experiment, else computed (a few minutes) and cached. ``refresh`` recomputes, e.g. after a backfill revised a
    development session's bars. Never the holdout (fx.guard).
    """
    exp = fx.load_experiment(conn, name)
    m = exp["definition"]
    dev = m["split"]["development"]["sessions"]
    fx.guard(conn, dev[0], dev[-1])
    baseline = decided_baseline(conn, name)
    record = F.fan_v2_record() if baseline == F.FAN_V2_VERSION else F.fan_record()
    key = hashlib.sha256(json.dumps({
        "experiment": exp["definition_hash"], "target": target, "baseline": record["definition_hash"],
        "horizons": FRAME_HORIZONS, "format": FRAME_FORMAT, "code": _sources_hash()}, sort_keys=True).encode()
    ).hexdigest()[:16]
    path = os.path.join(cache_dir, f"{name}_{target}_{baseline}_{key}.npz") if cache_dir else None
    if path and os.path.exists(path) and not refresh:
        return exp, baseline, _read_frames(path)
    d0, d1 = date.fromisoformat(dev[0]), date.fromisoformat(dev[-1])
    days = load_days(conn, target, history_start(d0, F.EVENT_SESSIONS + F.SHAPE_SESSIONS), d1)
    frames = baseline_frames(days, d0, d1, baseline)
    if path:
        _write_frames(path, frames)
    return exp, baseline, frames


def _write_frames(path: str, frames: Dict[str, Frame]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fs = [frames[k] for k in sorted(frames)]
    tmp = path + ".tmp.npz"
    np.savez(tmp, session=np.array([f.session for f in fs]), end=np.array([f.end for f in fs]),
             log_price=np.stack([f.log_price for f in fs]), var=np.stack([f.var for f in fs]),
             shape=np.stack([f.shape for f in fs]), ahead=np.stack([f.ahead for f in fs]))
    os.replace(tmp, path)


def _read_frames(path: str) -> Dict[str, Frame]:
    with np.load(path) as z:
        return {str(s): Frame(str(s), int(e), lp, v, q, a)
                for s, e, lp, v, q, a in zip(z["session"], z["end"], z["log_price"], z["var"], z["shape"], z["ahead"])}


# --------------------------------------------------------------------------
# The checks: rows, candidates, scoring
# --------------------------------------------------------------------------

@dataclass
class Rows:
    """Origin-horizon rows, one entry per row in every array."""
    session: np.ndarray          # datetime64[D]
    slot: np.ndarray             # the origin's slot
    horizon: np.ndarray          # minutes ahead
    phase: np.ndarray            # the origin's phase: an index into fan_scoring.PHASES
    ahead: np.ndarray            # a release placed in (t, t + h]
    var: np.ndarray              # the baseline's variance of the log price to t + h
    y: np.ndarray                # the realised log return from t to t + h
    q75: np.ndarray              # the baseline shape's standardised 75 % quantile at h

    def __len__(self) -> int:
        return len(self.y)

    @property
    def z(self) -> np.ndarray:
        """The realised move in the baseline's sigmas."""
        return self.y / np.sqrt(self.var)

    @staticmethod
    def concat(parts: Sequence["Rows"]) -> "Rows":
        if not parts:
            raise ValueError("no rows")
        return Rows(*(np.concatenate([getattr(p, f) for p in parts]) for f in Rows.__dataclass_fields__))


def frame_rows(fr: Frame, horizons: Sequence[int] = REPORT_HORIZONS, every: int = 1) -> Rows:
    """The frame's scored origins at ``horizons`` whose slot is a multiple of ``every``, and the pre-open slice's
    one origin at PRE_OPEN_MINUTES. An origin is scored with a price at both ends inside the trading day and a
    positive baseline variance."""
    t = np.arange(DAY_SLOTS)
    lp = fr.log_price
    parts = []
    for h in sorted(set(horizons) | set(PRE_OPEN_MINUTES)):
        i = FRAME_HORIZONS.index(h)
        ok = np.isfinite(lp) & (t + h < fr.end)
        ok[ok] &= np.isfinite(lp[(t + h)[ok]])
        ok &= np.isfinite(fr.var[i]) & (np.nan_to_num(fr.var[i]) > fan_v2.MIN_VARIANCE)
        sel = ok & (t % every == 0) if h in horizons else np.zeros(DAY_SLOTS, dtype=bool)
        if h in PRE_OPEN_MINUTES:
            sel[PRE_OPEN_ORIGIN] = ok[PRE_OPEN_ORIGIN]
        o = t[sel]
        n = len(o)
        parts.append(Rows(np.full(n, np.datetime64(fr.session, "D")), o, np.full(n, h), PHASE_OF_SLOT[o],
                          fr.ahead[i][o], fr.var[i][o], lp[o + h] - lp[o],
                          np.full(n, float(np.interp(0.75, TAU, fr.shape[i])))))
    return Rows.concat(parts)


class Candidate:
    """
    A learned fan in the checks. ``fit`` gets the training rows - the development sessions before the check,
    sampled every TRAIN_EVERY minutes plus the pre-open slice's origin - and ``predict`` gives each row a
    multiplier of the baseline's sigma (finite, > 0). The candidate's fan is the baseline's, sigma x multiplier,
    with the baseline's shape. Features beyond the rows (the panel, chunk 4) are the candidate's own: a row names
    its session and origin slot.
    """
    name = "candidate"

    def fit(self, rows: Rows) -> None:
        raise NotImplementedError

    def predict(self, rows: Rows) -> np.ndarray:
        raise NotImplementedError


class Identity(Candidate):
    """The baseline itself: every difference is exactly zero (the harness's self-check)."""
    name = "identity"

    def fit(self, rows: Rows) -> None:
        pass

    def predict(self, rows: Rows) -> np.ndarray:
        return np.ones(len(rows))


class PhaseScale(Candidate):
    """
    One constant width per horizon and origin phase: the median of |z| / Q(0.75) over the training rows - 1 where
    the baseline's width is right in that phase. A reference with no features, that a learned model has to beat
    to show it reads more than the time of day; a phase with fewer than MIN_ROWS rows takes its horizon's value.
    """
    name = "phase_scale"
    MIN_ROWS = 200
    BOUNDS = (0.5, 2.0)

    def fit(self, rows: Rows) -> None:
        u = np.abs(rows.z) / rows.q75
        clip = lambda x: float(np.clip(np.median(x), *self.BOUNDS))
        self.by_horizon, self.by_phase = {}, {}
        for h in np.unique(rows.horizon):
            sh = rows.horizon == h
            self.by_horizon[int(h)] = clip(u[sh])
            for p in np.unique(rows.phase[sh]):
                s = sh & (rows.phase == p)
                if s.sum() >= self.MIN_ROWS:
                    self.by_phase[(int(h), int(p))] = clip(u[s])

    def predict(self, rows: Rows) -> np.ndarray:
        trained = np.array(sorted(self.by_horizon))
        out = np.ones(len(rows))
        for h in np.unique(rows.horizon):
            sh = rows.horizon == h
            near = int(trained[np.argmin(np.abs(np.log(trained) - np.log(h)))])
            out[sh] = self.by_horizon[near]
            for p in np.unique(rows.phase[sh]):
                if (near, int(p)) in self.by_phase:
                    out[sh & (rows.phase == p)] = self.by_phase[(near, int(p))]
        return out


CANDIDATES: Dict[str, Callable[[], Candidate]] = {"identity": Identity, "phase_scale": PhaseScale}


def check_keys() -> List[str]:
    """Every comparison the checks report: per horizon, per horizon and origin phase, with a release ahead, and the
    pre-open slice."""
    keys = [f"h{h}" for h in REPORT_HORIZONS]
    keys += [f"h{h}:{name}" for h in REPORT_HORIZONS for name, _, _ in PHASES]
    keys += [f"h{h}:release" for h in REPORT_HORIZONS]
    return keys + [f"pre_open_{m}" for m in PRE_OPEN_MINUTES]


def score_frame(fr: Frame, rows: Rows, mult: np.ndarray) -> Dict[str, Tuple[float, float, int]]:
    """One check session, the frame's ``rows`` (frame_rows, every origin) and the candidate's sigma multipliers:
    ``{key: (baseline mean CRPS, candidate mean CRPS, origins)}`` in basis points over check_keys()."""
    mult = np.asarray(mult, dtype=float)
    if mult.shape != (len(rows),) or not np.all(np.isfinite(mult) & (mult > 0)):
        raise ValueError("a candidate must give every row a finite multiplier above zero")
    sb = np.sqrt(rows.var)
    cb, cc = np.empty(len(rows)), np.empty(len(rows))
    for h in np.unique(rows.horizon):
        s = rows.horizon == h
        Q = fr.shape[FRAME_HORIZONS.index(int(h))]
        sc = sb[s] * mult[s]
        cb[s] = sb[s] * crps(rows.y[s] / sb[s], Q) * 1e4
        cc[s] = sc * crps(rows.y[s] / sc, Q) * 1e4
    out: Dict[str, Tuple[float, float, int]] = {}

    def put(key: str, sel: np.ndarray) -> None:
        if sel.any():
            out[key] = (float(cb[sel].mean()), float(cc[sel].mean()), int(sel.sum()))

    for h in REPORT_HORIZONS:
        sh = rows.horizon == h
        put(f"h{h}", sh)
        for p, (name, _, _) in enumerate(PHASES):
            put(f"h{h}:{name}", sh & (rows.phase == p))
        put(f"h{h}:release", sh & rows.ahead)
    for m in PRE_OPEN_MINUTES:
        put(f"pre_open_{m}", (rows.horizon == m) & (rows.slot == PRE_OPEN_ORIGIN))
    return out


def run_checks(frames: Dict[str, Frame], manifest: Dict[str, Any], target: str,
               make: Callable[[], Candidate], every: int = TRAIN_EVERY) -> Dict[str, Any]:
    """
    The manifest's three checks for ``target``: per check a fresh candidate (``make()``) fitted on the rows of the
    development sessions before the check (every ``every`` minutes), then it and the baseline scored on every origin
    of the check's sessions. Paired intervals (paired) per check and pooled over the checks' sessions in date order.
    A session excluded for the target or without a frame is neither trained on nor scored.
    """
    sp = manifest["split"]
    dev = sp["development"]["sessions"]
    excluded = set(manifest["data"]["excluded"].get(target, []))
    keys = check_keys()
    pooled: List[Dict[str, Tuple[float, float, int]]] = []
    checks = []
    name = None
    for b in sp["checks"]["blocks"]:
        first, last = b["sessions"]["first"], b["sessions"]["last"]
        train_days = [d for d in dev if d < first and d not in excluded and d in frames]
        wanted = [d for d in dev if first <= d <= last and d not in excluded]
        train = Rows.concat([frame_rows(frames[d], every=every) for d in train_days])
        cand = make()
        name = cand.name
        cand.fit(train)
        per, scored = [], []
        for d in wanted:
            if d not in frames:
                continue
            rows = frame_rows(frames[d])
            per.append(score_frame(frames[d], rows, cand.predict(rows)))
            scored.append(d)
        pooled += per
        checks.append({"check": b["check"],
                       "train": {"first": train_days[0], "last": train_days[-1], "sessions": len(train_days),
                                 "rows": len(train)},
                       "sessions": {"first": first, "last": last, "wanted": len(wanted), "scored": scored,
                                    "missing": [d for d in wanted if d not in scored]},
                       "results": {k: paired(per, k) for k in keys}})
    results = {k: paired(pooled, k) for k in keys}
    return {"kind": "checks", "experiment": manifest.get("name"), "target": target, "candidate": name,
            "train_every": every, "uncertainty": dict(F.BOOTSTRAP), "checks": checks, "results": results,
            "verdicts": {k: _verdict(v) for k, v in results.items()},
            "roles": {k: _role(k, manifest, target) for k in keys}}


def checks(conn, name: str, target: str, make: Callable[[], Candidate], cache_dir: Optional[str] = None,
           refresh: bool = False) -> Dict[str, Any]:
    """run_checks on the stored development sessions of a registered experiment against its decided baseline."""
    m = fx.load_experiment(conn, name)["definition"]
    targets = [m["targets"]["primary"], *m["targets"]["secondary"]]
    if target not in targets:
        raise ValueError(f"{target} is not a target of {name} ({', '.join(targets)})")
    exp, baseline, frames = load_frames(conn, name, target, cache_dir, refresh)
    res = run_checks(frames, m, target, make)
    rec = F.fan_v2_record() if baseline == F.FAN_V2_VERSION else F.fan_record()
    res.update(experiment=name, experiment_hash=exp["definition_hash"], baseline=baseline,
               baseline_hash=rec["definition_hash"])
    return res


def _role(key: str, manifest: Dict[str, Any], target: str) -> str:
    """A comparison's role in the manifest: primary, secondary or exploratory (the breakdowns always are)."""
    h = manifest["horizons"]
    primary_target = target == manifest["targets"]["primary"]
    if ":" in key:
        return "exploratory"
    if key.startswith("pre_open"):
        return "secondary" if primary_target else "exploratory"
    minutes = int(key[1:])
    if minutes == h["primary"]["minutes"]:
        return "primary" if primary_target else "secondary"
    return "secondary" if primary_target and minutes in h["secondary"]["minutes"] else "exploratory"


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


_MARK = {"better": " ✓", "worse": " ✗"}


def _label(key: str) -> str:
    return f"{key[1:]} min" if key.startswith("h") else f"pre-open, 09:29 + {key.split('_')[-1]} min"


def _share(p: Optional[Dict[str, Any]]) -> str:
    if p is None or p["diff_share"] is None:
        return "-"
    return f"{100 * p['diff_share']:+.2f} %" + _MARK.get(_verdict(p), "")


def write_checks_report(res: Dict[str, Any], report_dir: str) -> str:
    """The checks' report: docs/reports/fan_checks_<experiment>_<target>_<candidate>.md."""
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"fan_checks_{res['experiment']}_{res['target']}_{res['candidate']}.md")
    rev = res.get("code_revision", "")
    rev = rev[:12] + ("+dirty" if rev.endswith("+dirty") else "")
    main_keys = [f"h{h}" for h in REPORT_HORIZONS] + [f"pre_open_{m}" for m in PRE_OPEN_MINUTES]
    L = [f"# Checks: {res['candidate']} against {res['baseline']} ({res['experiment']}, {res['target']})", "",
         f"Experiment hash `{res['experiment_hash'][:16]}`; baseline {res['baseline']} "
         f"`{res['baseline_hash'][:16]}`; code {rev}.", "",
         "**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the "
         f"development sessions before it only (rows every {res['train_every']} minutes, plus the pre-open origin), "
         "then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in "
         "basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block "
         "bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, "
         "✗ above.", "",
         "| Check | Trained on | Training rows | Checked | Scored |", "|---|---|---|---|---|"]
    for c in res["checks"]:
        t, s = c["train"], c["sessions"]
        L.append(f"| {c['check']} | {t['first']} to {t['last']} ({t['sessions']}) | {t['rows']:,} | "
                 f"{s['first']} to {s['last']} | {len(s['scored'])} of {s['wanted']}"
                 + (f" (missing {', '.join(s['missing'])})" if s["missing"] else "") + " |")
    L += ["", "## By horizon, the checks pooled", "",
          "| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | "
          "95 % interval | Verdict |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k in main_keys:
        p = res["results"].get(k)
        if p is None:
            continue
        iv = p["interval"]
        L.append(f"| {_label(k)} | {res['roles'][k]} | {p['sessions']} | {p['origins']:,} | "
                 f"{_f(p['base_crps_bps'])} | {_f(p['other_crps_bps'])} | {_f(p['diff_bps'], 5)} | "
                 f"{'-' if p['diff_share'] is None else f'{100 * p['diff_share']:+.2f} %'} | "
                 f"{'-' if not iv else f'[{iv[0]:+.5f}, {iv[1]:+.5f}]'} | {res['verdicts'][k]} |")
    L += ["", "## Per check (share of the baseline's CRPS)", "",
          "| Horizon | " + " | ".join(f"Check {c['check']}" for c in res["checks"]) + " |",
          "|---|" + "---|" * len(res["checks"])]
    for k in main_keys:
        L.append(f"| {_label(k)} | " + " | ".join(_share(c["results"].get(k)) for c in res["checks"]) + " |")
    L += ["", "## By origin phase (ET), the checks pooled", "",
          "| Phase | " + " | ".join(f"{h} min" for h in REPORT_HORIZONS) + " |",
          "|---|" + "---|" * len(REPORT_HORIZONS)]
    for name, a, b in PHASES:
        L.append(f"| {name.replace('_', ' ')} {a}-{b} | "
                 + " | ".join(_share(res["results"].get(f"h{h}:{name}")) for h in REPORT_HORIZONS) + " |")
    L.append("| a release ahead | " + " | ".join(_share(res["results"].get(f"h{h}:release"))
                                               for h in REPORT_HORIZONS) + " |")
    L.append("")
    with open(path, "w") as fh:
        fh.write("\n".join(L))
    return path
