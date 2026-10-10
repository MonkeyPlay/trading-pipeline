# forecaster/ml_bundle_data.py
"""
The seven-target bundles' dataset (contracts/nq_ml_bundle.py): one row per instrument and session,
each carrying its session date and instrument, its features (forecaster/ml_bundle_features.py), and
per target its label, the reason it has none, its eligibility at prediction time and - for the
direction targets - its move in threshold units.

  build(conn)          the research_0929 pool's NQ sessions (their stored snapshots and the latest
                       outcome revision of every target) and, for the pooled arm, ES's and RTY's sessions
                       of the same dates, each from its own snapshot (built, never stored) and its own
                       labels (forecaster/market_labels.py) - never NQ's copied over
  BundleData.mask(target, instrument=None, dates=None)
                       a target's training mask: rows with a classifiable label - a missing first-move
                       label never drops the row's close-direction label
  coverage()           per target and instrument: rows, labelled, and the count of every reason a row has
                       no label (ambiguous intrabar, uncovered, none_tested, shortened session, ...)
  eligible(payload)    per target, why it cannot be predicted from a snapshot (or None): the label
                       version's eligibility - a standard-session target on an early close, a missing
                       threshold, an unavailable first-level candidate
  inference_row(...)   the same features for one NQ session as of an issue time, for an arm: NQ's own
                       data only for N and P, the context instruments too for M
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from contracts import nq_ml as ml
from contracts import nq_ml_bundle as mb
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from forecaster import ml_bundle_features as bf
from forecaster import ml_features as mf

logger = logging.getLogger(__name__)
POOLED = ("NQ", "ES", "RTY")


def eligible(payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Per target, why it cannot be predicted from this snapshot - the label it would get - or None."""
    schedule = (payload.get("schedule") or {}).get("schedule")
    th = payload.get("thresholds") or {}
    frozen = ((payload.get("first_level_candidates") or {}).get("levels")) or {}
    out: Dict[str, Optional[str]] = {}
    for t in mb.TARGETS:
        why = None
        need = defs.TARGETS[t]["threshold"]
        if defs.TARGETS[t]["standard_session_only"] and schedule != "full":
            why = f"shortened_session: a standard-session target on a {schedule or 'unknown'} session"
        elif need and any(th.get(x) is None for x in need.split(",")):
            missing = [x for x in need.split(",") if th.get(x) is None]
            why = f"missing_threshold: {', '.join(missing)} not frozen at the cutoff"
        elif t == "first_level_tested":
            bad = [c for c in mb.CANDIDATES if (frozen.get(c) or {}).get("status") != "valid"]
            if bad:
                why = f"missing_reference: frozen candidate(s) unavailable at the cutoff: {', '.join(bad)}"
        out[t] = why
    return out


def move(target: str, measurements: Dict[str, Any]) -> float:
    """A direction target's realised move in threshold units, (close - O) / threshold, from the outcome's
    measurements; NaN when not measured."""
    spec = mb.DIRECTION[target]
    try:
        o, c, th = measurements.get("O"), measurements.get(spec["close"]), measurements.get(spec["threshold"])
        if o is None or c is None or th is None or Decimal(str(th)) == 0:
            return math.nan
        return float((Decimal(str(c)) - Decimal(str(o))) / Decimal(str(th)))
    except Exception:
        return math.nan


@dataclass
class BundleData:
    rows: pd.DataFrame                              # date, instrument, features, candidate columns
    labels: Dict[str, pd.Series]                    # target -> label (str) or None
    reasons: Dict[str, pd.Series]                   # target -> reason of a missing label
    eligibility: Dict[str, pd.Series]               # target -> why not predictable (None: eligible)
    moves: Dict[str, pd.Series]                     # direction target -> move in threshold units
    days: List[str]                                 # the NQ pool's session dates, oldest first
    snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)    # NQ day -> snapshot (pool order)
    sources: Dict[str, Any] = field(default_factory=dict)

    @property
    def dates(self) -> np.ndarray:
        return self.rows["date"].to_numpy()

    @property
    def instruments(self) -> np.ndarray:
        return self.rows["instrument"].to_numpy()

    def mask(self, target: str, instruments: Optional[Sequence[str]] = None,
             dates: Optional[Sequence[str]] = None) -> np.ndarray:
        """Rows with a classifiable label for ``target`` (optionally of some instruments and dates)."""
        m = np.array([isinstance(v, str) for v in self.labels[target]], dtype=bool)
        if instruments is not None:
            m &= np.isin(self.instruments, list(instruments))
        if dates is not None:
            m &= np.isin(self.dates, list(dates))
        return m

    def coverage(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for t in mb.TARGETS:
            out[t] = {}
            for s in sorted(set(self.instruments)):
                m = self.instruments == s
                labs = self.labels[t][m]
                reasons = self.reasons[t][m]
                out[t][s] = {"rows": int(m.sum()), "labelled": int(sum(isinstance(v, str) for v in labs)),
                             "classes": {c: int(sum(v == c for v in labs)) for c in mb.CLASSES[t]},
                             "without_label": {r: int(n) for r, n in pd.Series(
                                 [r for v, r in zip(labs, reasons) if not isinstance(v, str)]).value_counts().items()}}
        return out


def _payload_of(snapshot) -> Dict[str, Any]:
    return snapshot["payload"]


def _label_parts(labels: Dict[str, Any], target: str):
    d = labels.get(target) or {}
    return (d.get("label") if isinstance(d.get("label"), str) else None), d.get("reason")


def assemble(entries: List[Dict[str, Any]], days: List[str], snapshots: Dict[str, Dict[str, Any]],
             sources: Optional[Dict[str, Any]] = None) -> BundleData:
    """BundleData from per-row entries ``{'date', 'instrument', 'features', 'candidates', 'labels', 'measurements',
    'payload'}`` (pure: the database-free half of build, used by the tests too)."""
    feats = pd.DataFrame([{"date": e["date"], "instrument": e["instrument"], **e["features"], **e["candidates"]}
                          for e in entries])
    labels = {t: pd.Series([_label_parts(e["labels"], t)[0] for e in entries], dtype=object) for t in mb.TARGETS}
    reasons = {t: pd.Series([(None if isinstance(_label_parts(e["labels"], t)[0], str) else
                              (_label_parts(e["labels"], t)[1] or "no outcome")) for e in entries], dtype=object)
               for t in mb.TARGETS}
    elig = {t: pd.Series([(eligible(e["payload"])[t] if e["payload"] is not None else "no snapshot")
                          for e in entries], dtype=object) for t in mb.TARGETS}
    moves = {t: pd.Series([move(t, e["measurements"] or {}) for e in entries], dtype=float) for t in mb.DIRECTION}
    return BundleData(feats, labels, reasons, elig, moves, list(days), snapshots, sources or {})


def build(conn, profile: str = ml.PROFILE, until: Optional[str] = None, pooled: bool = True,
          progress=lambda *a: None) -> BundleData:
    """The dataset (see the module docstring): every NQ pool session up to ``until``; with ``pooled``, ES's and RTY's
    sessions of the same dates from their own snapshots and outcomes."""
    from forecaster import market_labels as mlab_f
    from forecaster.ml_train import pool
    snaps = [s for s in pool(conn, profile) if until is None or str(s["session_date"]) <= until]
    days = [str(s["session_date"]) for s in snaps]
    by_day = {str(s["session_date"]): s for s in snaps}
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    loaded = mf.load(conn, mf.ALL_SYMBOLS, (cal.session(days[0]).session_date - timedelta(days=200)).isoformat(),
                     days[-1])
    fs = mf.build(conn, days, by_day, loaded=loaded, profile=profile)
    progress(f"v1 features of {len(days)} sessions")
    entries = []
    for d in days:
        s = by_day[d]
        revs = history.get(s["snapshot_id"]) or []
        last = revs[-1] if revs else {"labels": {}, "measurements": {}}
        payload = _payload_of(s)
        entries.append({"date": d, "instrument": "NQ", "payload": payload,
                        "features": {**bf.row(fs, d, "NQ", payload, payload, context=True),
                                     # the development rows' M abstention (a required context instrument unusable)
                                     "ctx_required_missing": float(bool(mf.required_missing(fs, d)))},
                        "candidates": bf.candidate_row(payload), "labels": last["labels"],
                        "measurements": last["measurements"]})
    built = {"NQ": len(entries)}
    if pooled:
        for sym in POOLED[1:]:
            n = 0
            for d in days:
                snap = mlab_f.market_snapshot(conn, sym, d, profile)
                if snap is None:
                    continue
                out = mlab_f.market_outcome(conn, snap)
                payload = snap["payload"]
                entries.append({"date": d, "instrument": sym, "payload": payload,
                                "features": bf.row(fs, d, sym, payload, _payload_of(by_day[d]), context=False),
                                "candidates": bf.candidate_row(payload), "labels": out["labels"],
                                "measurements": out["measurements"]})
                n += 1
            built[sym] = n
            progress(f"{sym}: {n} sessions labelled from its own snapshots")
    order = sorted(range(len(entries)), key=lambda i: (entries[i]["date"], POOLED.index(entries[i]["instrument"])))
    entries = [entries[i] for i in order]                  # date order; a date's instruments together
    return assemble(entries, days, by_day, {"rows_built": built, "profile": profile, "until": until})


def inference_row(conn, snapshot: Dict[str, Any], arm: str, as_of: Optional[datetime] = None,
                  profile: str = ml.PROFILE) -> Dict[str, Any]:
    """``{'features', 'candidates', 'eligible', 'fs'}`` of one NQ session for ``arm`` as of ``as_of``: N and P read NQ's
    bars only, M the context instruments too (contracts/nq_ml_bundle.ARMS)."""
    day = str(snapshot["session_date"])
    symbols = mb.ARMS[arm]["symbols"]
    fs = mf.build(conn, [day], {day: snapshot}, as_of=as_of, symbols=symbols, profile=profile)
    payload = snapshot["payload"]
    return {"features": bf.row(fs, day, "NQ", payload, payload, context=(arm == "M")),
            "candidates": bf.candidate_row(payload), "eligible": eligible(payload), "fs": fs}
