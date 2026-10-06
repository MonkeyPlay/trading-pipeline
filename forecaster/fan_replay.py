# forecaster/fan_replay.py
"""
A live-style replay of the intermarket experiment's leading candidates
(docs/fan_experiment.md): for predefined check sessions and origins, everything a forecast
reads is rebuilt as the store would have served it at issuance - only bars that had closed
by then - and compared with the research path that built the checks.

  features     the panel loaded as of the issuance (fan_panel.load_panel(as_of=...)) over
               the sessions a forecast needs, the features built from it, against the
               research table at the same origin
  forming bar  the same issuance 30 seconds into the next minute: the bar still forming
               must not be read
  baseline     fan_rw_v2 fitted from fan_data.load_days(as_of=...) - the session in
               progress incomplete - its variance from the origin to every horizon, against
               the cached frame (v2's shape depends on earlier sessions only and is taken
               from the frame)
  multiplier   each candidate, trained on its check's training rows, on the replayed
               inputs against its multiplier in the research batch; and one origin
               predicted alone against the same origin inside the session's batch

What it cannot replay: a recent bar the collector stores as preliminary (is_completed = 0,
within 2 hours of collection) may be revised by a later run; history holds only the revised
value, so a live forecast can read a value the research never saw. The forward record
(chunk 8) logs the live inputs for that reason.

  pick_sessions(...)   the predefined sessions, from information known before each opened
  replay(...)          the comparisons and their largest differences
  write_report(...)    docs/reports/fan_replay_<experiment>_<target>.md
"""

from __future__ import annotations

import os
from datetime import date, time, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

from contracts import fan as F
from forecaster import fan_benchmark as fb
from forecaster import fan_features as ff
from forecaster import fan_harness as fh
from forecaster import fan_panel as fp
from forecaster import fan_v2
from forecaster.fan_benchmark import slot_instant, slot_of_time
from forecaster.fan_data import history_start, load_days
from forecaster.fan_scoring import PHASE_OF_SLOT

ORIGINS_ET = (time(19, 0), time(3, 0), time(8, 25), time(9, 28), time(10, 30), time(15, 30))
HISTORY_SESSIONS = 35            # the sessions before an origin's a replayed panel holds: 20 full ones for the usual
PER_KIND = 3
RTOL = 1e-5                      # features are float32: equal to a few units in the last place


def pick_sessions(sessions: Sequence[str], day_rv: Dict[str, float], release_days: Sequence[str]
                  ) -> Dict[str, List[str]]:
    """The predefined sessions among ``sessions`` (the checks'): PER_KIND release days (a high-tier or FOMC release,
    evenly spread), PER_KIND volatile (the largest previous-session variance over its usual - known before the
    session opens) and PER_KIND ordinary (no such release, the previous session's variance nearest its usual)."""
    rel = [d for d in sessions if d in set(release_days)]
    picked: Dict[str, List[str]] = {}
    picked["release"] = [rel[int(i)] for i in np.linspace(0, len(rel) - 1, PER_KIND)] if len(rel) >= PER_KIND else rel
    known = [d for d in sessions if np.isfinite(day_rv.get(d, np.nan)) and d not in picked["release"]]
    picked["volatile"] = sorted(known, key=lambda d: -day_rv[d])[:PER_KIND]
    quiet = [d for d in known if d not in set(rel) and d not in picked["volatile"]]
    picked["ordinary"] = sorted(quiet, key=lambda d: abs(day_rv[d]))[:PER_KIND]
    return picked


def _same(a: np.ndarray, b: np.ndarray, rtol: float = RTOL) -> np.ndarray:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    both_nan = np.isnan(a) & np.isnan(b)
    with np.errstate(invalid="ignore"):
        close = np.abs(a - b) <= rtol * np.maximum(1.0, np.maximum(np.abs(a), np.abs(b)))
    return both_nan | close


def _rel(a: np.ndarray, b: np.ndarray) -> float:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    ok = np.isfinite(a) & np.isfinite(b)
    if not ok.any():
        return 0.0
    return float(np.max(np.abs(a[ok] - b[ok]) / np.maximum(np.abs(b[ok]), 1e-300)))


def replay(conn, target: str, sessions: Dict[str, List[str]], table: ff.FeatureTable, frames: Dict[str, fh.Frame],
           symbols: Sequence[str], first_complete: Dict[str, Optional[str]],
           candidates: Dict[str, Dict[str, fh.Candidate]], check_of: Dict[str, int]) -> Dict[str, Any]:
    """Every comparison for the ``sessions`` (kind -> dates) at ORIGINS_ET; ``candidates``: name -> check -> a
    candidate fitted on that check's training rows; ``check_of``: session -> its check."""
    tally: Dict[str, Dict[str, Any]] = {k: {"compared": 0, "failed": 0, "max_rel": 0.0, "examples": []}
                                        for k in ("features", "forming bar", "baseline", "multiplier", "alone")}

    def note(kind: str, ok: np.ndarray, rel: float, where: str) -> None:
        t = tally[kind]
        t["compared"] += int(np.size(ok))
        bad = int(np.size(ok) - np.count_nonzero(ok))
        t["failed"] += bad
        t["max_rel"] = max(t["max_rel"], rel)
        if bad and len(t["examples"]) < 5:
            t["examples"].append(where)

    for kind, days in sessions.items():
        for s in days:
            i = table.sessions.index(s)
            window = table.sessions[max(0, i - HISTORY_SESSIONS):i + 1]
            fr = frames[s]
            research_rows = fh.frame_rows(fr)
            for at in ORIGINS_ET:
                t = slot_of_time(at)
                if t + 1 >= fr.end:
                    continue
                where = f"{s} {at:%H:%M}"
                as_of = slot_instant(date.fromisoformat(s), t + 1)          # the origin's bar has just closed
                live = ff.build(fp.load_panel(conn, window, symbols, as_of=as_of), target, first_complete)
                xl, xr = live.X[-1, t], table.X[i, t]
                note("features", _same(xl, xr), _rel(xl, xr), where)
                forming = ff.build(fp.load_panel(conn, window, symbols, as_of=as_of + timedelta(seconds=30)),
                                   target, first_complete)
                note("forming bar", _same(forming.X[-1, t], xl, rtol=0.0), _rel(forming.X[-1, t], xl), where)
                days_live = load_days(conn, target, history_start(date.fromisoformat(s), F.EVENT_SESSIONS),
                                      date.fromisoformat(s), as_of=as_of)
                day = days_live[-1]
                model = fan_v2.fit(day, days_live[:-1])
                V = fb.horizon_variances(model, day.returns, fh.FRAME_HORIZONS)["full"][:, t]
                note("baseline", _same(V, fr.var[:, t], rtol=1e-9), _rel(V, fr.var[:, t]), where)
                at_t = research_rows.take(research_rows.slot == t)
                if not len(at_t):
                    continue
                hidx = np.array([fh.FRAME_HORIZONS.index(int(h)) for h in at_t.horizon])
                slots = [slot for _, _, slot in fan_v2.placed(day)]
                ahead = np.array([fh._ahead(slots, int(h))[t] for h in at_t.horizon])
                live_rows = fh.Rows(at_t.session, at_t.slot, at_t.horizon, PHASE_OF_SLOT[at_t.slot], ahead, V[hidx],
                                    np.full(len(at_t), np.nan), at_t.q75)
                for name, by_check in candidates.items():
                    cand = by_check[check_of[s]]
                    batch = cand.predict(research_rows)[research_rows.slot == t]
                    alone = cand.predict(at_t)
                    note("alone", _same(alone, batch, rtol=1e-12), _rel(alone, batch), f"{name} {where}")
                    research_table = cand.table
                    cand.table = live
                    try:
                        replayed = cand.predict(live_rows)
                    finally:
                        cand.table = research_table
                    note("multiplier", _same(replayed, batch), _rel(replayed, batch), f"{name} {where}")
    return {"sessions": sessions, "origins_et": [f"{t:%H:%M}" for t in ORIGINS_ET], "checks": tally,
            "passed": all(v["failed"] == 0 for v in tally.values())}


def write_report(res: Dict[str, Any], meta: Dict[str, Any], report_dir: str) -> str:
    """docs/reports/fan_replay_<experiment>_<target>.md."""
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"fan_replay_{meta['experiment']}_{meta['target']}.md")
    what = {"features": "every feature from the panel as of the issuance, against the research table",
            "forming bar": "the same issuance 30 s into the next minute (its bar still forming), against the above",
            "baseline": "fan_rw_v2's variance to every horizon from load_days(as_of), against the cached frame",
            "multiplier": "each candidate on the replayed features and variance, against the research batch",
            "alone": "one origin predicted alone, against the same origin inside the session's batch"}
    L = [f"# Live-style replay: {meta['experiment']}, {meta['target']}", "",
         f"Code {meta['code_revision'][:12]} (source snapshot `{meta['source']['snapshot']}`); data "
         f"`{meta['data_fingerprint']}`; candidates "
         + ", ".join(f"`{c}`" for c in meta["candidates"]) + f" (each trained on its check's training rows).", "",
         "Sessions (predefined from what was known before each opened): "
         + "; ".join(f"{k} {', '.join(v)}" for k, v in res["sessions"].items())
         + f". Origins (ET): {', '.join(res['origins_et'])} - the forecast issued as the origin's bar closes.", "",
         f"**{'Passed' if res['passed'] else 'FAILED'}.**", "",
         "| Check | What | Compared | Failed | Largest relative difference |", "|---|---|---|---|---|"]
    for k, v in res["checks"].items():
        L.append(f"| {k} | {what[k]} | {v['compared']:,} | {v['failed']} | {v['max_rel']:.2e} |"
                 + (f" e.g. {', '.join(v['examples'])}" if v["examples"] else ""))
    L += ["", "Not replayable: a bar stored as preliminary (is_completed = 0, within 2 hours of collection) may be "
          "revised by a later collection; history holds only the revised value. The forward record logs the live "
          "inputs.", ""]
    with open(path, "w") as fh_:
        fh_.write("\n".join(L))
    return path
