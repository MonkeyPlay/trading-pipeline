# forecaster/fan_scoring.py
"""
Scoring the benchmark fan at every origin (contracts/fan.FAN 'score'): for every
minute of every complete full session with a last price, and every score horizon
that ends inside the trading day, the fan's distribution of the log price against
the realised last price - under the full model and its three references.

  score_day(model, day)       one session's origin-by-origin scores, tallied
  score_sessions(days, ...)   walk-forward over a range: each session fitted on
                              the sessions before it only, then scored
  recent_accuracy(days, ...)  the last sessions before a day, per horizon: skill
                              against flat and interval coverage - the chart's
                              accuracy fade
  summarise(...)              per horizon: CRPS (bps), z RMS, coverage, PIT
                              deciles, paired differences between the variants
                              with block-bootstrap intervals, the origin phases,
                              and origins with a release inside the horizon
  write_report(...)           docs/reports/<version>_<symbol>_<start>_<end>.md/.csv

Pure apart from the report file; the CLI (scripts/fan.py) loads the days.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import date, time
from statistics import NormalDist
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

from contracts import fan as F
from forecaster.experiments import block_bootstrap
from forecaster.fan_benchmark import (DAY_SLOTS, VARIANTS, Day, FanModel, InsufficientHistory, crps_normal,
                                      fit, horizon_variances, norm_cdf, slot_of_time)

BPS = 1e4
MIN_VARIANCE = 1e-14            # a closed market: nothing to score
PHASES = (("overnight", "18:00", "08:00"), ("pre_open", "08:00", "09:30"), ("opening_hour", "09:30", "10:30"),
          ("midday", "10:30", "14:00"), ("afternoon", "14:00", "16:00"), ("after_close", "16:00", "18:00"))
PAIRS = (("full", "flat"), ("seasonal", "flat"), ("seasonal_events", "seasonal"), ("full", "seasonal_events"))
_ZC = np.array([NormalDist().inv_cdf((1 + c) / 2) for c in F.COVERAGE_LEVELS])


def _phase_index() -> np.ndarray:
    out = np.zeros(DAY_SLOTS, dtype=int)
    for i, (_, a, b) in enumerate(PHASES):
        lo = slot_of_time(_hm(a))
        hi = slot_of_time(_hm(b)) or DAY_SLOTS
        out[lo:hi] = i
    return out


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


PHASE_OF_SLOT = _phase_index()


@dataclass
class Tally:
    """Sums for one (variant, horizon) over any set of origins."""
    n: int = 0
    crps: float = 0.0
    z2: float = 0.0
    cover: np.ndarray = field(default_factory=lambda: np.zeros(len(F.COVERAGE_LEVELS)))
    pit: np.ndarray = field(default_factory=lambda: np.zeros(10))

    def add(self, crps_bps: np.ndarray, z: np.ndarray) -> None:
        self.n += len(z)
        self.crps += float(crps_bps.sum())
        self.z2 += float((z * z).sum())
        self.cover += (np.abs(z)[:, None] <= _ZC[None, :]).sum(axis=0)
        self.pit += np.histogram(np.clip(norm_cdf(z), 0, 1 - 1e-12), bins=10, range=(0, 1))[0]

    def merge(self, other: "Tally") -> None:
        self.n += other.n
        self.crps += other.crps
        self.z2 += other.z2
        self.cover += other.cover
        self.pit += other.pit

    def summary(self) -> Dict[str, Any]:
        if not self.n:
            return {"n": 0}
        return {"n": self.n, "crps_bps": self.crps / self.n, "z_rms": float(np.sqrt(self.z2 / self.n)),
                "coverage": {f"{c:.2f}": float(k / self.n) for c, k in zip(F.COVERAGE_LEVELS, self.cover)},
                "pit": [float(x / self.n) for x in self.pit]}


@dataclass
class DayScore:
    """One session's tallies: per horizon and variant, overall, per origin phase, and with a release ahead."""
    session_date: date
    all: Dict[tuple, Tally] = field(default_factory=dict)            # (variant, h)
    phase: Dict[tuple, Tally] = field(default_factory=dict)          # (variant, h, phase)
    release: Dict[tuple, Tally] = field(default_factory=dict)        # (variant, h) - a release in (t, t + h]

    def mean_crps(self, variant: str, h: int) -> Optional[float]:
        t = self.all.get((variant, h))
        return t.crps / t.n if t and t.n else None


def _release_ahead(day: Day, h: int) -> np.ndarray:
    """True at origin t when a known release minute falls in (t, t + h]."""
    marks = np.zeros(DAY_SLOTS + 1)
    for rel in (day.releases if day.releases_known else ()):
        if 0 <= rel.slot < DAY_SLOTS:
            marks[rel.slot + 1] += 1
    C = np.cumsum(marks)                       # C[s] = releases at slots < s
    t = np.arange(DAY_SLOTS)
    hi = np.minimum(DAY_SLOTS, t + h + 1)
    return (C[hi] - C[t + 1]) > 0


def score_day(model: FanModel, day: Day, horizons: Sequence[int] = F.SCORE_HORIZONS) -> DayScore:
    """Every origin of ``day`` under ``model`` (fitted on the sessions before it)."""
    lp = np.log(day.last_price)
    V = horizon_variances(model, day.returns, horizons)
    out = DayScore(day.session_date)
    t = np.arange(DAY_SLOTS)
    for i, h in enumerate(horizons):
        ok = np.isfinite(lp) & (t + h < day.end)
        ok[ok] &= np.isfinite(lp[(t + h)[ok]])
        for v in VARIANTS:
            ok &= np.isfinite(V[v][i]) & (np.nan_to_num(V[v][i]) > MIN_VARIANCE)
        if not ok.any():
            continue
        origins = t[ok]
        y = lp[origins + h] - lp[origins]
        ahead = _release_ahead(day, h)[origins]
        phases = PHASE_OF_SLOT[origins]
        for v in VARIANTS:
            sigma = np.sqrt(V[v][i][origins])
            c, z = crps_normal(y, sigma) * BPS, y / sigma
            out.all.setdefault((v, h), Tally()).add(c, z)
            if ahead.any():
                out.release.setdefault((v, h), Tally()).add(c[ahead], z[ahead])
            for p in np.unique(phases):
                m = phases == p
                out.phase.setdefault((v, h, PHASES[p][0]), Tally()).add(c[m], z[m])
    return out


def score_sessions(days: Sequence[Day], start: date, end: date,
                   horizons: Sequence[int] = F.SCORE_HORIZONS) -> Dict[str, Any]:
    """
    Walk-forward over the sessions of ``days`` from ``start`` to ``end``: each complete full session fitted on the
    loaded sessions before it, then scored. Returns ``{'scores': [DayScore], 'skipped': {reason: n},
    'last_model': FanModel | None}``.
    """
    ordered = sorted(days, key=lambda d: d.session_date)
    scores, skipped, last = [], {}, None
    for i, day in enumerate(ordered):
        if not (start <= day.session_date <= end):
            continue
        if day.schedule != "full":
            skipped["not_full_schedule"] = skipped.get("not_full_schedule", 0) + 1
            continue
        if not day.complete:
            skipped["incomplete"] = skipped.get("incomplete", 0) + 1
            continue
        try:
            model = fit(day, ordered[:i])
        except InsufficientHistory:
            skipped["insufficient_history"] = skipped.get("insufficient_history", 0) + 1
            continue
        scores.append(score_day(model, day, horizons))
        last = model
    return {"scores": scores, "skipped": skipped, "last_model": last}


def recent_accuracy(days: Sequence[Day], before: date, sessions: int,
                    horizons: Sequence[int] = F.SCORE_HORIZONS) -> Dict[str, Any]:
    """
    The fan's measured accuracy just before session ``before`` - what the chart's accuracy fade reads: the last
    ``sessions`` complete full sessions of ``days`` before it, scored walk-forward as ``score_sessions`` scores them.
    Per horizon: the full fan's CRPS skill against the flat reference (1 - CRPS full / CRPS flat: 0 is no better
    than a random walk with one variance for every minute), the coverage of its 50 and 90 % central intervals and
    its z RMS. ``{'sessions', 'first', 'last', 'horizons': [{'horizon', 'origins', 'skill', 'cover50', 'cover90',
    'z_rms'}]}``; no horizons without a session to score.
    """
    eligible = sorted((d for d in days if d.session_date < before and d.complete and d.schedule == "full"),
                      key=lambda d: d.session_date)[-sessions:]
    out: Dict[str, Any] = {"sessions": 0, "first": None, "last": None, "horizons": []}
    if not eligible:
        return out
    scores = score_sessions(days, eligible[0].session_date, eligible[-1].session_date, horizons)["scores"]
    if not scores:
        return out
    out.update(sessions=len(scores), first=scores[0].session_date.isoformat(),
               last=scores[-1].session_date.isoformat())
    for h in horizons:
        full, flat = Tally(), Tally()
        for s in scores:
            if ("full", h) in s.all:
                full.merge(s.all[("full", h)])
            if ("flat", h) in s.all:
                flat.merge(s.all[("flat", h)])
        f, ref = full.summary(), flat.summary()
        if not f["n"]:
            continue
        out["horizons"].append({
            "horizon": h, "origins": f["n"],
            "skill": 1 - f["crps_bps"] / ref["crps_bps"] if ref.get("crps_bps") else None,
            "cover50": f["coverage"]["0.50"], "cover90": f["coverage"]["0.90"], "z_rms": f["z_rms"]})
    return out


def summarise(scores: Sequence[DayScore], horizons: Sequence[int] = F.SCORE_HORIZONS) -> List[Dict[str, Any]]:
    """Per horizon: every variant's tallies, the paired differences with intervals, phases and release-ahead."""
    b = F.BOOTSTRAP
    out = []
    for h in horizons:
        entry: Dict[str, Any] = {"horizon": h, "variants": {}, "paired": {}, "phases": {}, "release_ahead": {}}
        for v in VARIANTS:
            total, rel = Tally(), Tally()
            for s in scores:
                if (v, h) in s.all:
                    total.merge(s.all[(v, h)])
                if (v, h) in s.release:
                    rel.merge(s.release[(v, h)])
            entry["variants"][v] = total.summary()
            entry["release_ahead"][v] = rel.summary()
        for a, ref in PAIRS:
            diffs = [x - y for x, y in ((s.mean_crps(a, h), s.mean_crps(ref, h)) for s in scores)
                     if x is not None and y is not None]
            ma, mr = entry["variants"][a].get("crps_bps"), entry["variants"][ref].get("crps_bps")
            entry["paired"][f"{a}-{ref}"] = {
                "sessions": len(diffs),
                "diff_bps": float(np.mean(diffs)) if diffs else None,
                "interval": block_bootstrap(diffs, b["block_sessions"], b["resamples"], b["seed"], b["interval"]),
                "skill": (1 - ma / mr) if ma is not None and mr else None,
            }
        for name, _, _ in PHASES:
            row = {}
            for v in ("flat", "full"):
                t = Tally()
                for s in scores:
                    if (v, h, name) in s.phase:
                        t.merge(s.phase[(v, h, name)])
                row[v] = t.summary()
            entry["phases"][name] = row
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _f(x, nd=3) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _ci(interval, nd=3) -> str:
    return "-" if not interval else f"[{_f(interval[0], nd)}, {_f(interval[1], nd)}]"


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> List[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def write_report(meta: Dict[str, Any], summary: List[Dict[str, Any]], last_model: Optional[FanModel],
                 directory: str) -> str:
    """``<version>_<symbol>_<start>_<end>.md`` and ``.csv`` in ``directory``; returns the markdown path."""
    os.makedirs(directory, exist_ok=True)
    stem = f"{meta['version']}_{meta['symbol']}_{meta['start']}_{meta['end']}"
    levels = [f"{c:.2f}" for c in F.COVERAGE_LEVELS]
    L: List[str] = [
        f"# Benchmark fan {meta['version']}: {meta['symbol']}, {meta['start']} to {meta['end']}", "",
        f"Scored by `forecaster/fan_scoring.py` (code {meta['code_revision']}); definition hash "
        f"`{meta['definition_hash'][:16]}`. Every origin minute of every complete full session, walk-forward: each "
        "session's fan is fitted on the sessions before it and its own bars up to the origin only.", "",
        f"- **Sessions:** {meta['sessions_scored']} scored"
        + "".join(f"; {n} skipped ({r.replace('_', ' ')})" for r, n in sorted(meta['skipped'].items())) + ".",
        "- **Variants:** flat (one variance for every trading minute), seasonal (the intraday pattern S), "
        "seasonal + events (S x E), full (the level l x S x E) - the fan as issued.",
        "- **Units:** CRPS of the log price in basis points (lower is better); z RMS is 1 for a calibrated "
        "spread (above 1: too narrow); coverage of central intervals should equal their level.",
        "- **Intervals:** paired session-mean CRPS differences, moving-block bootstrap "
        f"({F.BOOTSTRAP['block_sessions']}-session blocks, {int(F.BOOTSTRAP['interval'] * 100)}%); negative favours "
        "the first variant.", "",
        "## The fan as issued (full)", "",
    ]
    rows = []
    for e in summary:
        s = e["variants"]["full"]
        if not s.get("n"):
            continue
        p = e["paired"]["full-flat"]
        rows.append([e["horizon"], s["n"], _f(s["crps_bps"]), _f(s["z_rms"])]
                    + [_pct(s["coverage"][k]) for k in levels] + [_pct(p["skill"]), _ci(p["interval"])])
    L += _table(["h (min)", "origins", "CRPS bps", "z RMS"] + [f"cover {k}" for k in levels]
                + ["skill vs flat", "full-flat bps 95%"], rows)
    L += ["", "## What each part adds", "", "Mean CRPS (bps) per variant, and the paired step differences:", ""]
    rows = []
    for e in summary:
        if not e["variants"]["full"].get("n"):
            continue
        rows.append([e["horizon"]] + [_f(e["variants"][v].get("crps_bps")) for v in VARIANTS]
                    + [f"{_f(e['paired'][k]['diff_bps'])} {_ci(e['paired'][k]['interval'])}"
                       for k in ("seasonal-flat", "seasonal_events-seasonal", "full-seasonal_events")])
    L += _table(["h", "flat", "seasonal", "+events", "full", "pattern (S - flat)", "events (+E - S)",
                 "level (full - S x E)"], rows)
    L += ["", "## Origins with a release ahead", "",
          "Origins whose next h minutes hold a scheduled release (known calendar only):", ""]
    rows = []
    for e in summary:
        a, b = e["release_ahead"]["seasonal"], e["release_ahead"]["seasonal_events"]
        if not a.get("n"):
            continue
        rows.append([e["horizon"], a["n"], _f(a["crps_bps"]), _f(b["crps_bps"]), _f(a["z_rms"]), _f(b["z_rms"]),
                     _pct(a["coverage"]["0.90"]), _pct(b["coverage"]["0.90"])])
    L += _table(["h", "origins", "S CRPS", "S x E CRPS", "S z RMS", "S x E z RMS", "S cover 0.90",
                 "S x E cover 0.90"], rows) if rows else ["(none in range)"]
    L += ["", "## By origin phase (ET)", ""]
    rows = []
    for e in summary:
        if e["horizon"] not in (15, 60):
            continue
        for name, a, b in PHASES:
            fl, fu = e["phases"][name]["flat"], e["phases"][name]["full"]
            if not fu.get("n"):
                continue
            skill = (1 - fu["crps_bps"] / fl["crps_bps"]) if fl.get("crps_bps") else None
            rows.append([e["horizon"], f"{name} ({a}-{b})", fu["n"], _f(fu["crps_bps"]), _f(fl["crps_bps"]),
                         _pct(skill), _f(fu["z_rms"]), _pct(fu["coverage"]["0.90"])])
    L += _table(["h", "phase", "origins", "full CRPS", "flat CRPS", "skill", "z RMS", "cover 0.90"], rows)
    L += ["", "## Calibration (full): PIT deciles", "",
          "Share of origins whose realised price fell in each tenth of the issued distribution (10% each when "
          "calibrated; heavy outer deciles mean too narrow, heavy middle too wide):", ""]
    rows = [[e["horizon"]] + [f"{100 * x:.1f}" for x in e["variants"]["full"]["pit"]]
            for e in summary if e["variants"]["full"].get("n")]
    L += _table(["h"] + [f"{10 * i}-{10 * i + 10}" for i in range(10)], rows)
    if last_model is not None:
        L += ["", f"## The model on the last session ({last_model.session_date})", "",
              f"- Long level: {_f(last_model.level_long)}; intraday pattern from {len(last_model.seasonal_sessions)} "
              "sessions.", "- Release multipliers (earlier releases):", ""]
        rows = []
        for g, v in F.EVENT_GROUPS.items():
            source = "estimated (shrunk)" if last_model.occurrences[g] else "fallback"
            rows.append([g, last_model.occurrences[g], source]
                        + [f"{a}-{z}: {_f(m, 2)}" for (a, z), m in zip(v["buckets"], last_model.multipliers[g])])
        width = max(len(r) for r in rows)
        L += _table(["group", "releases", "source"] + [f"bucket {i + 1}" for i in range(width - 3)],
                    [r + [""] * (width - len(r)) for r in rows])
    path = os.path.join(directory, stem + ".md")
    with open(path, "w") as fh:
        fh.write("\n".join(L) + "\n")
    with open(os.path.join(directory, stem + ".csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["horizon", "variant", "scope", "origins", "crps_bps", "z_rms"] + [f"cover_{k}" for k in levels])
        for e in summary:
            for v in VARIANTS:
                scopes = [("all", e["variants"][v]), ("release_ahead", e["release_ahead"][v])]
                scopes += [(f"phase:{n}", e["phases"][n][v]) for n, _, _ in PHASES if v in e["phases"][n]]
                for scope, s in scopes:
                    if s.get("n"):
                        w.writerow([e["horizon"], v, scope, s["n"], f"{s['crps_bps']:.6f}", f"{s['z_rms']:.6f}"]
                                   + [f"{s['coverage'][k]:.6f}" for k in levels])
    return path
